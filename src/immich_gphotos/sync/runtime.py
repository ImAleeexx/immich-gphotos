from dataclasses import dataclass

from immich_gphotos.clock import Clock
from immich_gphotos.config import Settings
from immich_gphotos.immich.protocol import ImmichClient
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.events import EventRepo
from immich_gphotos.sync.throttle import transfer_allowed
from immich_gphotos.sync.worker import Worker


@dataclass(frozen=True)
class TickResult:
    processed: int = 0
    halted: bool = False
    # Whether the schedule window was closed during this tick. This no
    # longer means "nothing ran" -- claim_next, eligibility, the local
    # duplicate lookup and the remote hash check all proceed regardless of
    # the window. It only means that any asset which still needed bytes
    # moved after those checks was deferred rather than uploaded; see
    # `deferred`.
    window_closed: bool = False
    deferred: int = 0


class Runtime:
    """One bounded unit of work per call, so scheduling is testable without threads."""

    def __init__(
        self,
        assets: AssetRepo,
        worker: Worker,
        settings: Settings,
        clock: Clock,
        events: EventRepo,
        *,
        immich: ImmichClient,
    ) -> None:
        self._assets = assets
        self._worker = worker
        self._settings = settings
        self._clock = clock
        self._events = events
        # Only used to resolve `settings.filters.album_allowlist` (Immich
        # album ids) to member asset ids, once per tick -- see
        # `_album_allowlist_ids`. Every other step in a tick is local/DB-only.
        self._immich = immich
        self._paused_reason: str | None = None

    @property
    def paused_reason(self) -> str | None:
        return self._paused_reason

    def pause(self, reason: str) -> None:
        self._paused_reason = reason
        self._events.add("error", f"transfer paused: {reason}")

    def resume(self) -> None:
        if self._paused_reason:
            self._events.add("info", "transfer resumed")
        self._paused_reason = None

    def tick(self, limit: int = 8) -> TickResult:
        if self._paused_reason:
            return TickResult()

        # Schedule and bandwidth gate the byte transfer only -- claiming,
        # eligibility, the local duplicate lookup and the remote hash check
        # all run at any hour. Computed once per tick and handed to every
        # asset in the batch, so the whole batch sees a consistent window
        # state rather than possibly straddling the boundary mid-tick.
        window_open = transfer_allowed(self._clock.now(), self._settings.window)
        album_allowlist_ids = self._album_allowlist_ids()

        processed = 0
        deferred = 0
        claimed = self._assets.claim_next(limit=limit)
        for index, stored in enumerate(claimed):
            result = self._worker.process(
                stored, transfer_allowed=window_open, album_allowlist_ids=album_allowlist_ids
            )
            processed += 1
            if result.deferred:
                deferred += 1
            if result.halt:
                reason = result.error_class.value if result.error_class else "unknown"
                self.pause(reason)
                # The rest of this batch was already flipped to UPLOADING by
                # claim_next's single atomic UPDATE, but never handed to the
                # worker. Left alone they would be stranded there until some
                # separate crash-recovery sweep happens to run. A halt is
                # routine, not a crash, so tick() cleans up after itself:
                # return them to PENDING without counting an attempt against
                # them, since being queued behind a halted asset is not their
                # fault.
                for stranded in claimed[index + 1 :]:
                    self._assets.requeue(stranded.asset.immich_id, self._clock.now())
                return TickResult(processed=processed, halted=True)
        return TickResult(processed=processed, window_closed=not window_open, deferred=deferred)

    def _album_allowlist_ids(self) -> frozenset[str] | None:
        """Resolve `filters.album_allowlist` (Immich album ids) to the set of
        member asset ids, fresh for this tick.

        None means the filter is off (no allowlist configured) -- the common
        case -- and skips the Immich calls entirely. Recomputing this once
        per tick, the same way `window_open` is, rather than caching it for
        the runtime's lifetime, means an asset added to (or removed from) an
        allowed album shows up correctly the next time it is claimed, without
        needing a settings save to force a rebuild.
        """
        allowlist = self._settings.filters.album_allowlist
        if not allowlist:
            return None
        ids: set[str] = set()
        for album_id in allowlist:
            ids.update(self._immich.album_asset_ids(album_id))
        return frozenset(ids)
