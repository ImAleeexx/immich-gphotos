import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.kv import CursorRepo
from immich_gphotos.sync.backfill import BACKFILL_CURSOR, BackfillJob


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
    assert job.is_running() is False
    assert job.run_slice().scanned == 0


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
