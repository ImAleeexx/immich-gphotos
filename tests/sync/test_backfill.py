import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.immich.protocol import AssetPage
from immich_gphotos.models import Asset, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.kv import CursorRepo
from immich_gphotos.sync.backfill import BACKFILL_CURSOR, BackfillJob, StalledPaginationError


def asset(i: str) -> Asset:
    return Asset(
        immich_id=i,
        checksum=f"sum-{i}",
        filename=f"{i}.jpg",
        type="IMAGE",
        size_bytes=1,
        immich_updated_at="2026-09-17T10:00:00Z",
        original_path=None,
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
    )


@pytest.fixture
def rig(tmp_path):
    clock = FakeClock()
    conn = connect(tmp_path / "t.db")
    immich = FakeImmichClient(assets=[asset(str(i)) for i in range(5)])
    job = BackfillJob(immich, AssetRepo(conn, clock), CursorRepo(conn), Settings(reconcile_page_size=2))
    return job, AssetRepo(conn, clock), CursorRepo(conn)


def test_backfill_does_nothing_until_started(rig):
    job, _, _ = rig
    immich = job._immich
    assert job.is_running() is False
    assert job.run_slice().scanned == 0
    assert immich.searches == []


def test_a_slice_walks_one_page_and_remembers_the_next(rig):
    job, assets, cursors = rig
    job.start()
    progress = job.run_slice(pages=1)
    assert (progress.scanned, progress.done) == (2, False)
    assert cursors.get(BACKFILL_CURSOR) == "2"
    assert assets.get("0").priority is Priority.BACKFILL


def test_running_to_completion_marks_done_and_clears_the_cursor(rig):
    job, assets, cursors = rig
    job.start()
    while not job.run_slice(pages=1).done:
        pass
    assert job.is_running() is False
    assert cursors.get(BACKFILL_CURSOR) is None
    assert len(assets.claim_next(limit=99)) == 5


def test_backfill_enqueues_at_the_lowest_priority(rig):
    job, assets, _ = rig
    job.start()
    job.run_slice(pages=99)
    assert assets.get("3").priority is Priority.BACKFILL


def test_reset_clears_progress(rig):
    job, _, cursors = rig
    job.start()
    job.run_slice(pages=1)
    job.reset()
    assert cursors.get(BACKFILL_CURSOR) is None
    assert job.is_running() is False


def test_enqueued_excludes_assets_already_synced(rig):
    """The reconciler suite pins `enqueued` in four places; the backfill's own
    enqueue counting — driven by `upsert_pending`'s boolean return — needs the
    same coverage, or a regression that recounts terminal rows goes unnoticed."""
    job, assets, _ = rig
    for i in range(5):
        assets.upsert_pending(asset(str(i)), Priority.WEBHOOK)
    assets.claim_next(limit=5)
    for i in range(5):
        assets.mark_synced(str(i), f"key-{i}", Outcome.UPLOADED)

    job.start()
    progress = job.run_slice(pages=99)
    assert progress.scanned == 5
    assert progress.enqueued < progress.scanned
    assert progress.enqueued == 0


def test_pagination_that_does_not_advance_raises_instead_of_looping_forever(tmp_path):
    """A server bug or retried response repeating a page number must not spin
    a slice forever — it must fail loudly rather than re-walking one page."""
    clock = FakeClock()
    conn = connect(tmp_path / "t.db")

    class Stuck(FakeImmichClient):
        def search_assets(self, **kwargs):
            page = kwargs.get("page", 1)
            return AssetPage(assets=[asset("0")], next_page=page)

    immich = Stuck(assets=[asset("0")])
    cursors = CursorRepo(conn)
    job = BackfillJob(immich, AssetRepo(conn, clock), cursors, Settings(reconcile_page_size=2))
    job.start()

    with pytest.raises(StalledPaginationError):
        job.run_slice(pages=1)
    assert cursors.get(BACKFILL_CURSOR) == "1"
