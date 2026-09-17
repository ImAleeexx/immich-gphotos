from datetime import timedelta

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.immich.protocol import AssetPage, ImmichError
from immich_gphotos.models import Asset, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.kv import CursorRepo
from immich_gphotos.sync.reconciler import RECONCILE_CURSOR, Reconciler, StalledPaginationError


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
    return AssetRepo(conn, clock), CursorRepo(conn), clock


def test_first_run_only_plants_the_cursor(rig):
    """History belongs to the backfill job, not to the incremental pass."""
    assets, cursors, clock = rig
    immich = FakeImmichClient(assets=[asset("a"), asset("b")])
    result = Reconciler(immich, assets, cursors, Settings(), clock).run_once()
    assert result.enqueued == 0
    assert immich.searches == []
    assert cursors.get(RECONCILE_CURSOR) == clock.now().isoformat()


def test_subsequent_run_enqueues_with_the_overlap_applied(rig):
    assets, cursors, clock = rig
    immich = FakeImmichClient(assets=[asset("a")])
    settings = Settings()
    reconciler = Reconciler(immich, assets, cursors, settings, clock)
    reconciler.run_once()
    planted = clock.now()

    clock.advance(timedelta(minutes=20))
    result = reconciler.run_once()

    assert result.enqueued == 1
    assert assets.get("a").priority is Priority.RECONCILE
    sent = immich.searches[0]["updated_after"]
    assert sent == planted - settings.reconcile_overlap


def test_all_pages_are_walked(rig):
    assets, cursors, clock = rig
    immich = FakeImmichClient(assets=[asset(str(i)) for i in range(5)])
    settings = Settings(reconcile_page_size=2)
    reconciler = Reconciler(immich, assets, cursors, settings, clock)
    reconciler.run_once()
    clock.advance(timedelta(minutes=20))
    result = reconciler.run_once()
    assert result.pages == 3
    assert result.enqueued == 5


def test_terminal_assets_are_not_re_enqueued(rig):
    assets, cursors, clock = rig
    immich = FakeImmichClient(assets=[asset("a")])
    reconciler = Reconciler(immich, assets, cursors, Settings(), clock)
    reconciler.run_once()
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_synced("a", "key", Outcome.UPLOADED)

    clock.advance(timedelta(minutes=20))
    result = reconciler.run_once()
    assert result.enqueued == 0


def test_cursor_does_not_advance_when_a_page_fails(rig):
    assets, cursors, clock = rig

    class Failing(FakeImmichClient):
        def search_assets(self, **kwargs):
            raise ImmichError("boom", 500)

    immich = Failing(assets=[asset("a")])
    reconciler = Reconciler(immich, assets, cursors, Settings(), clock)
    reconciler.run_once()
    planted = cursors.get(RECONCILE_CURSOR)

    clock.advance(timedelta(minutes=20))
    with pytest.raises(ImmichError):
        reconciler.run_once()
    assert cursors.get(RECONCILE_CURSOR) == planted


def test_pagination_that_does_not_advance_raises_instead_of_looping_forever(rig):
    """A server bug or retried response repeating a page number must not spin
    run_once forever — it must fail loudly so the cursor never advances."""
    assets, cursors, clock = rig

    class Stuck(FakeImmichClient):
        def search_assets(self, **kwargs):
            page = kwargs.get("page", 1)
            return AssetPage(assets=[asset("a")], next_page=page)

    immich = Stuck(assets=[asset("a")])
    reconciler = Reconciler(immich, assets, cursors, Settings(), clock)
    reconciler.run_once()
    planted = cursors.get(RECONCILE_CURSOR)

    clock.advance(timedelta(minutes=20))
    with pytest.raises(StalledPaginationError):
        reconciler.run_once()
    assert cursors.get(RECONCILE_CURSOR) == planted


def test_cursor_does_not_advance_when_enqueue_fails_mid_page(rig):
    """The failure path matters as much when it happens during enqueue as when
    it happens during fetch: either way the cursor must not move."""
    assets, cursors, clock = rig
    immich = FakeImmichClient(assets=[asset("a"), asset("b")])
    reconciler = Reconciler(immich, assets, cursors, Settings(), clock)
    reconciler.run_once()
    planted = cursors.get(RECONCILE_CURSOR)

    class FlakyAssets:
        def __init__(self) -> None:
            self.calls = 0

        def upsert_pending(self, asset, priority):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("boom")
            return True

    clock.advance(timedelta(minutes=20))
    broken = Reconciler(immich, FlakyAssets(), cursors, Settings(), clock)
    with pytest.raises(RuntimeError):
        broken.run_once()
    assert cursors.get(RECONCILE_CURSOR) == planted


def test_deletions_disabled_means_trashed_assets_are_not_requested(rig):
    assets, cursors, clock = rig
    immich = FakeImmichClient(assets=[asset("a")])
    reconciler = Reconciler(immich, assets, cursors, Settings(deletions_enabled=False), clock)
    reconciler.run_once()
    clock.advance(timedelta(minutes=20))
    reconciler.run_once()
    assert immich.searches[0]["with_deleted"] is False


def test_deletions_enabled_requests_trashed_assets_too(rig):
    assets, cursors, clock = rig
    immich = FakeImmichClient(assets=[asset("a")])
    reconciler = Reconciler(immich, assets, cursors, Settings(deletions_enabled=True), clock)
    reconciler.run_once()
    clock.advance(timedelta(minutes=20))
    reconciler.run_once()
    assert immich.searches[0]["with_deleted"] is True
