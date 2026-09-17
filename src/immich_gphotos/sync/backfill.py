from dataclasses import dataclass

from immich_gphotos.config import Settings
from immich_gphotos.immich.protocol import ImmichClient
from immich_gphotos.models import Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.kv import CursorRepo

BACKFILL_CURSOR = "backfill"


@dataclass(frozen=True)
class BackfillProgress:
    page: int = 0
    scanned: int = 0
    enqueued: int = 0
    done: bool = True


class BackfillJob:
    """Walks the whole library once, at the lowest priority.

    Runs in slices so a library of any size never blocks the event path: a photo
    taken today is claimed before eighteen thousand from 2014.
    """

    def __init__(
        self,
        immich: ImmichClient,
        assets: AssetRepo,
        cursors: CursorRepo,
        settings: Settings,
    ) -> None:
        self._immich = immich
        self._assets = assets
        self._cursors = cursors
        self._settings = settings

    def is_running(self) -> bool:
        return self._cursors.get(BACKFILL_CURSOR) is not None

    def start(self) -> None:
        self._cursors.set(BACKFILL_CURSOR, "1")

    def reset(self) -> None:
        self._cursors.delete(BACKFILL_CURSOR)

    def run_slice(self, pages: int = 1) -> BackfillProgress:
        raw = self._cursors.get(BACKFILL_CURSOR)
        if not raw:
            return BackfillProgress()

        page = int(raw)
        scanned = enqueued = 0

        for _ in range(pages):
            result = self._immich.search_assets(
                updated_after=None, page=page, size=self._settings.reconcile_page_size
            )
            for asset in result.assets:
                scanned += 1
                if self._assets.upsert_pending(asset, Priority.BACKFILL):
                    enqueued += 1
            if result.next_page is None:
                self._cursors.delete(BACKFILL_CURSOR)
                return BackfillProgress(page=page, scanned=scanned, enqueued=enqueued, done=True)
            page = result.next_page

        self._cursors.set(BACKFILL_CURSOR, str(page))
        return BackfillProgress(page=page, scanned=scanned, enqueued=enqueued, done=False)
