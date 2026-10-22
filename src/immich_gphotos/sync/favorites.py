import logging
import threading
from dataclasses import dataclass

from immich_gphotos.gphotos.protocol import GooglePhotosClient, GPhotosError
from immich_gphotos.models import AssetState, ErrorClass, StoredAsset
from immich_gphotos.store.assets import AssetRepo

logger = logging.getLogger(__name__)

_DELETED_REASON = "deleted_from_immich"


@dataclass(frozen=True)
class FavoriteSyncResult:
    synced: int = 0
    failed: int = 0
    # Items deliberately left alone this sweep: `held` because the set of
    # Immich rows behind the item was not complete enough to justify an
    # unstar, `protected` because the item's star may be the user's own
    # (it was not starred by this service on an item it uploaded itself).
    held: int = 0
    protected: int = 0


@dataclass(frozen=True)
class _Remote:
    pushed: bool | None
    managed: bool


class FavoriteSyncer:
    """Push Immich star state to Google Photos, one Google item at a time.

    The worker stars newly-uploaded assets immediately (so a favorited photo
    is favorited in Google the moment it lands), but a star added or removed
    in Immich *after* the bytes synced never re-enters the upload queue --
    SYNCED is terminal and bytes don't move twice. This sweeper closes that
    gap every reconcile tick.

    Reconciliation is per Google media key, not per Immich asset: several
    Immich assets (duplicates, the same file in two libraries) can dedup onto
    one Google item, and pushing each row's own star would flip that item
    back and forth every sweep. The target for an item is merged from every
    row that maps to it:

    - any live (SYNCED, not trashed) row starred -> star it;
    - every row live and unstarred -> unstar it, but only if this service
      provably put the star there (`managed`: it starred an item it uploaded
      itself). An item that was already in Google may carry a star the user
      set by hand, and nothing here can read that back authoritatively, so
      such items are only ever starred, never unstarred;
    - any row not settled (pending, failed, trashed in Immich, ...) and none
      starred -> hold: an incomplete or in-flux source set never unstars.

    Writes are idempotent and serialised per item; a row is only recorded as
    settled if its star did not change while the write was in flight. One
    failure never stops the sweep, except rate limiting / a halting error,
    which ends it early so the next tick retries. Favorites never halt
    transfer and never burn upload attempts -- the bytes are already safe.
    `set_favorite` returning is a *confirmed write*; Google offers no cheap
    per-item read-back through gpmc, so nothing here claims a verified one.
    """

    def __init__(
        self,
        gphotos: GooglePhotosClient,
        assets: AssetRepo,
        *,
        ledger=None,  # noqa: ANN001 - sync.favorite_ledger.SharedFavoriteLedger
        google_account: str | None = None,
        account_id: str | None = None,
    ) -> None:
        self._gphotos = gphotos
        self._assets = assets
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Cross-account agreement for service accounts that back up into the
        # same Google account (see sync.favorite_ledger). Inactive unless all
        # three are known.
        self._ledger = ledger if (ledger is not None and google_account and account_id) else None
        self._google = google_account
        self._account_id = account_id

    def _lock_for(self, media_key: str) -> threading.Lock:
        if self._ledger is not None:
            return self._ledger.lock_for(self._google, media_key)
        with self._locks_guard:
            lock = self._locks.get(media_key)
            if lock is None:
                lock = self._locks[media_key] = threading.Lock()
            return lock

    def _remote(self, media_key: str, rows: list[StoredAsset]) -> _Remote:
        row = self._assets.favorite_remote(media_key)
        if row is not None and (row["pushed"] is not None or row["managed"]):
            return _Remote(
                pushed=None if row["pushed"] is None else bool(row["pushed"]), managed=bool(row["managed"])
            )
        # Before the ledger existed, `favorite_synced` on a row was the last
        # value pushed for that row. Derive conservatively from it.
        pushed_values = {r.favorite_synced for r in rows if r.favorite_synced is not None}
        if True in pushed_values:
            return _Remote(pushed=True, managed=self._assets.media_key_uploaded_here(media_key))
        if pushed_values == {False}:
            return _Remote(pushed=False, managed=False)
        return _Remote(pushed=None, managed=False)

    def reconcile_key(self, media_key: str) -> str:
        """Decide and (if needed) push one item's star.

        Returns "synced", "noop", "held", "protected" or raises the
        underlying error from `set_favorite`. With a shared ledger, the
        decision also counts every other service account backing up into
        the same Google account: any of them wanting the star keeps it, any
        of them in flux holds an unstar, and a star put there by any of
        them counts as this service's own.
        """
        with self._lock_for(media_key):
            rows = self._assets.rows_for_media_key(media_key)
            relevant = [
                r
                for r in rows
                if not (r.state is AssetState.INELIGIBLE and r.ineligible_reason == _DELETED_REASON)
            ]
            live = [r for r in relevant if r.state is AssetState.SYNCED and not r.asset.is_trashed]
            remote = self._remote(media_key, rows)
            local_want = any(r.asset.is_favorite for r in live)
            local_held = not live or (not local_want and len(live) != len(relevant))
            others = []
            if self._ledger is not None:
                self._ledger.publish(
                    self._google,
                    media_key,
                    self._account_id,
                    intent=None if local_held else local_want,
                    managed=remote.managed,
                )
                others = self._ledger.others(self._google, media_key, self._account_id)
            if local_held:
                self._assets.touch_favorite_remote(media_key)
                return "held"
            want = local_want or any(o.intent is True for o in others)
            if not want and any(o.intent is None for o in others):
                self._assets.touch_favorite_remote(media_key)
                return "held"
            pushed = remote.pushed
            if self._ledger is not None:
                shared = self._ledger.pushed(self._google, media_key)
                if shared is not None:
                    pushed = shared
            managed_any = remote.managed or any(o.managed for o in others)
            outcome = "noop"
            managed = remote.managed
            if want and pushed is not True:
                self._gphotos.set_favorite(media_key, True)
                # Starting from a known-unstarred item this service created
                # itself, the star is provably ours.
                managed = remote.managed or self._assets.media_key_uploaded_here(media_key)
                pushed = True
                outcome = "synced"
            elif not want and pushed is True:
                if not managed_any:
                    outcome = "protected"
                else:
                    self._gphotos.set_favorite(media_key, False)
                    pushed = False
                    outcome = "synced"
            self._assets.record_favorite_remote(media_key, pushed=pushed, managed=managed, intent=local_want)
            if self._ledger is not None:
                self._ledger.record_pushed(self._google, media_key, pushed)
                self._ledger.publish(
                    self._google, media_key, self._account_id, intent=local_want, managed=managed
                )
            for r in live:
                self._assets.settle_favorite_row(r.asset.immich_id, r.asset.is_favorite)
            return outcome

    def sync_once(self, limit: int = 500) -> FavoriteSyncResult:
        """Reconcile up to `limit` remote writes' worth of drifted items."""
        synced = failed = held = protected = 0
        keys = self._assets.favorite_candidate_keys(limit=max(limit * 4, limit))
        for key in keys:
            if synced + failed >= limit:
                break
            try:
                outcome = self.reconcile_key(key)
            except GPhotosError as exc:
                failed += 1
                logger.debug("could not sync favorite for %s: %s", key, exc)
                if exc.error_class is ErrorClass.RATE_LIMITED or exc.error_class.halts_transfer():
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - one star must not kill the sweep
                failed += 1
                logger.debug("could not sync favorite for %s: %s", key, exc)
                continue
            if outcome == "synced":
                synced += 1
            elif outcome == "held":
                held += 1
            elif outcome == "protected":
                protected += 1
        return FavoriteSyncResult(synced=synced, failed=failed, held=held, protected=protected)
