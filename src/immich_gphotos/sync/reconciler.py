from dataclasses import dataclass
from datetime import datetime

from immich_gphotos.clock import Clock
from immich_gphotos.config import Settings
from immich_gphotos.immich.protocol import ImmichClient
from immich_gphotos.models import Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.kv import CursorRepo

RECONCILE_CURSOR = "reconcile"


@dataclass(frozen=True)
class ReconcileResult:
    scanned: int = 0
    enqueued: int = 0
    pages: int = 0


class Reconciler:
    """The safety net.

    Immich's webhook action is fire-and-forget with no retry, so nothing may be
    known only by webhook. This pass re-reads everything Immich changed since the
    last complete run, and the cursor advances only when a run finishes — a crash
    mid-pass re-reads rather than skips.
    """

    def __init__(
        self,
        immich: ImmichClient,
        assets: AssetRepo,
        cursors: CursorRepo,
        settings: Settings,
        clock: Clock,
    ) -> None:
        self._immich = immich
        self._assets = assets
        self._cursors = cursors
        self._settings = settings
        self._clock = clock

    def run_once(self) -> ReconcileResult:
        started = self._clock.now()
        previous = self._cursors.get(RECONCILE_CURSOR)

        if previous is None:
            # Nothing to reconcile against yet. The backfill job owns history.
            self._cursors.set(RECONCILE_CURSOR, started.isoformat())
            return ReconcileResult()

        updated_after = datetime.fromisoformat(previous) - self._settings.reconcile_overlap
        scanned = enqueued = pages = 0
        page: int | None = 1

        while page is not None:
            result = self._immich.search_assets(
                updated_after=updated_after,
                page=page,
                size=self._settings.reconcile_page_size,
                with_deleted=self._settings.deletions_enabled,
            )
            pages += 1
            for asset in result.assets:
                scanned += 1
                if self._assets.upsert_pending(asset, Priority.RECONCILE):
                    enqueued += 1
            page = result.next_page

        self._cursors.set(RECONCILE_CURSOR, started.isoformat())
        return ReconcileResult(scanned=scanned, enqueued=enqueued, pages=pages)
