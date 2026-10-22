from dataclasses import replace

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Filters, RetryPolicy
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.gphotos.protocol import GPhotosError
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, AssetState, ErrorClass, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.favorites import FavoriteSyncer
from immich_gphotos.sync.worker import Worker

ASSET = Asset(
    immich_id="a1",
    checksum="sum-a",
    filename="IMG_1.JPG",
    type="IMAGE",
    size_bytes=3,
    immich_updated_at="2026-09-17T10:00:00Z",
    original_path=None,
    visibility="timeline",
    is_offline=False,
    is_trashed=False,
)


def _rig(tmp_path, **worker_kwargs):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    immich = FakeImmichClient(contents={"a1": b"ABC"})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    worker = Worker(assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, **worker_kwargs)
    return worker, assets, immich, gphotos, clock


def _claim(assets: AssetRepo, asset: Asset = ASSET):
    assets.upsert_pending(asset, Priority.WEBHOOK)
    return assets.claim_next(limit=1)[0]


def _sync(assets: AssetRepo, asset: Asset, media_key: str = "key-1"):
    """Put a row straight to SYNCED the way the worker would leave it."""
    assets.upsert_pending(asset, Priority.WEBHOOK)
    stored = assets.claim_next(limit=1)[0]
    assets.mark_synced(stored.asset.immich_id, media_key, Outcome.UPLOADED)
    return assets.get(stored.asset.immich_id)


def test_worker_stars_a_favorited_upload_immediately(tmp_path):
    worker, assets, _, gphotos, _ = _rig(tmp_path)
    result = worker.process(_claim(assets, replace(ASSET, is_favorite=True)))

    assert result.state is AssetState.SYNCED
    assert gphotos.favorite_calls == [(result.media_key, True)]
    assert assets.get("a1").favorite_synced is True


def test_worker_does_not_star_an_unfavorited_upload(tmp_path):
    worker, assets, _, gphotos, _ = _rig(tmp_path)
    worker.process(_claim(assets, replace(ASSET, is_favorite=False)))

    assert gphotos.favorite_calls == []
    assert assets.get("a1").favorite_synced is None


def test_worker_still_syncs_bytes_when_starring_fails(tmp_path):
    worker, assets, _, gphotos, _ = _rig(tmp_path)
    gphotos.fail_methods["set_favorite"] = GPhotosError("no", ErrorClass.TRANSIENT)
    result = worker.process(_claim(assets, replace(ASSET, is_favorite=True)))

    assert result.state is AssetState.SYNCED
    assert result.outcome is Outcome.UPLOADED
    # Left drifted for the sweeper rather than failing the backup.
    assert assets.get("a1").favorite_synced is None


def test_worker_respects_favorites_enabled_off(tmp_path):
    worker, assets, _, gphotos, _ = _rig(tmp_path, favorites_enabled=False)
    worker.process(_claim(assets, replace(ASSET, is_favorite=True)))

    assert gphotos.favorite_calls == []
    assert assets.get("a1").favorite_synced is None


def test_worker_stars_a_favorited_dedup_hit_without_uploading(tmp_path):
    worker, assets, _, gphotos, _ = _rig(tmp_path)
    gphotos.present["sum-a"] = "existing-key"
    result = worker.process(_claim(assets, replace(ASSET, is_favorite=True)))

    assert result.outcome is Outcome.ALREADY_PRESENT
    assert gphotos.uploads == []
    assert gphotos.favorite_calls == [("existing-key", True)]


def test_sweeper_pushes_star_and_unstar_drift(tmp_path):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)

    _sync(assets, replace(ASSET, immich_id="a1", checksum="s1", is_favorite=True), "key-1")
    _sync(assets, replace(ASSET, immich_id="a2", checksum="s2", is_favorite=False), "key-2")
    # a2 was previously starred in Google, then unstarred in Immich.
    assets.mark_favorite_synced("a2", True)

    result = syncer.sync_once()

    assert result.synced == 2
    assert gphotos.favorites == {"key-1": True, "key-2": False}
    assert assets.get("a1").favorite_synced is True
    assert assets.get("a2").favorite_synced is False


def test_sweeper_skips_rows_with_no_media_key_and_up_to_date_rows(tmp_path):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)

    _sync(assets, replace(ASSET, immich_id="a1", checksum="s1", is_favorite=True), "key-1")
    assets.mark_favorite_synced("a1", True)

    result = syncer.sync_once()

    assert result.synced == 0
    assert gphotos.favorite_calls == []


def test_never_starred_never_pushed_rows_are_not_drift(tmp_path):
    """Regression: after the favorites upgrade, every historical SYNCED row
    carries `favorite_synced IS NULL` -- but 99% were never starred, and
    Google's default is unstarred, so pushing `False` for all of them would
    be tens of thousands of no-op API calls (and a rate-limit incident).
    Only starred-but-never-pushed rows count as drift."""
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)

    for i in range(5):
        _sync(assets, replace(ASSET, immich_id=f"plain-{i}", checksum=f"p{i}", is_favorite=False), f"k{i}")
    _sync(assets, replace(ASSET, immich_id="starred", checksum="s", is_favorite=True), "ks")

    assert [d.asset.immich_id for d in assets.favorite_drift()] == ["starred"]
    result = syncer.sync_once()

    assert result.synced == 1
    assert gphotos.favorite_calls == [("ks", True)]
    assert assets.get("plain-0").favorite_synced is None


def test_sweeper_continues_past_a_single_failure(tmp_path):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)

    _sync(assets, replace(ASSET, immich_id="a1", checksum="s1", is_favorite=True), "bad-key")
    _sync(assets, replace(ASSET, immich_id="a2", checksum="s2", is_favorite=True), "good-key")
    gphotos.fail_methods["set_favorite"] = GPhotosError("no", ErrorClass.TRANSIENT)

    # Fail-all makes both fail; remove the blanket failure and fail only the
    # first by key is not supported, so assert the sweep is all-or-nothing
    # safe: nothing recorded, everything retried next tick.
    result = syncer.sync_once()

    assert result.synced == 0
    assert result.failed == 2
    assert assets.get("a1").favorite_synced is None
    assert assets.get("a2").favorite_synced is None


def test_upsert_updates_the_star_without_clearing_what_google_was_told(tmp_path):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    assets.upsert_pending(replace(ASSET, is_favorite=False), Priority.WEBHOOK)
    assets.claim_next(limit=1)[0]
    assets.mark_synced("a1", "key-1", Outcome.UPLOADED)
    assets.mark_favorite_synced("a1", False)

    assets.upsert_pending(replace(ASSET, is_favorite=True), Priority.RECONCILE)

    drifted = assets.get("a1")
    assert drifted.asset.is_favorite is True
    assert drifted.favorite_synced is False
    assert [d.asset.immich_id for d in assets.favorite_drift()] == ["a1"]


def test_favorite_sync_runs_on_the_reconcile_cadence_only_when_enabled(tmp_path):
    from immich_gphotos.config import Settings
    from immich_gphotos.store.events import EventRepo
    from immich_gphotos.sync.loops import BackgroundLoops

    class Spy:
        def __init__(self):
            self.calls = 0

        def sync_once(self):
            self.calls += 1
            from immich_gphotos.sync.favorites import FavoriteSyncResult

            return FavoriteSyncResult()

    class Stub:
        paused_reason = None

        def tick(self):
            return None

    clock = FakeClock()
    events = EventRepo(connect(tmp_path / "e.db"), clock)

    class Noop:
        def run_once(self):
            from immich_gphotos.sync.reconciler import ReconcileResult

            return ReconcileResult()

        def run_slice(self, pages=1):
            from immich_gphotos.sync.backfill import BackfillProgress

            return BackfillProgress()

        def is_running(self):
            return False

        def sync_once(self):
            from immich_gphotos.sync.albums import AlbumSyncResult

            return AlbumSyncResult()

        def plan(self):
            from immich_gphotos.sync.deletions import DeletionPlan

            return DeletionPlan()

    spy = Spy()
    off = BackgroundLoops(
        runtime=Stub(),
        reconciler=Noop(),
        backfill=Noop(),
        album_mirror=Noop(),
        deletion_sweeper=Noop(),
        settings=Settings(favorites_enabled=False),
        clock=clock,
        events=events,
        assets=None,
        favorite_syncer=spy,
    )
    off.iterate()
    assert spy.calls == 0

    on = BackgroundLoops(
        runtime=Stub(),
        reconciler=Noop(),
        backfill=Noop(),
        album_mirror=Noop(),
        deletion_sweeper=Noop(),
        settings=Settings(favorites_enabled=True),
        clock=clock,
        events=events,
        assets=None,
        favorite_syncer=spy,
    )
    on.iterate()
    assert spy.calls == 1
