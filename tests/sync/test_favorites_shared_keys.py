"""Favorites reconciled per Google item (PR #6 follow-up).

Several Immich assets can dedup onto one Google media key; stars must be
merged per item, never pushed per row, and an item whose star may be the
user's own must never be unstarred.
"""

from dataclasses import replace

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Filters, RetryPolicy
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.gphotos.protocol import GPhotosError
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, ErrorClass, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.favorites import FavoriteSyncer
from immich_gphotos.sync.worker import Worker

BASE = Asset(
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


def _repo(tmp_path):
    return AssetRepo(connect(tmp_path / "t.db"), FakeClock())


def _synced(assets, immich_id, media_key, *, fav, outcome=Outcome.UPLOADED):
    asset = replace(BASE, immich_id=immich_id, checksum=f"sum-{immich_id}", is_favorite=fav)
    assets.upsert_pending(asset, Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_synced(immich_id, media_key, outcome)
    return asset


def _set_fav(assets, asset, fav, **changes):
    assets.upsert_pending(replace(asset, is_favorite=fav, **changes), Priority.RECONCILE)


def test_two_assets_on_one_item_with_conflicting_stars_converge_without_flapping(tmp_path):
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)
    _synced(assets, "a1", "shared", fav=True)
    _synced(assets, "a2", "shared", fav=False)

    for _ in range(4):
        syncer.sync_once()

    assert gphotos.favorite_calls == [("shared", True)]
    assert gphotos.favorites == {"shared": True}


def test_the_item_is_unstarred_only_once_every_source_is_unstarred(tmp_path):
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)
    a1 = _synced(assets, "a1", "shared", fav=True)
    a2 = _synced(assets, "a2", "shared", fav=True)
    syncer.sync_once()

    _set_fav(assets, a1, False)
    syncer.sync_once()
    assert gphotos.favorites == {"shared": True}  # a2 still wants it

    _set_fav(assets, a2, False)
    syncer.sync_once()
    syncer.sync_once()
    assert gphotos.favorite_calls == [("shared", True), ("shared", False)]


def test_a_source_trashed_in_immich_never_triggers_an_unstar(tmp_path):
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)
    a1 = _synced(assets, "a1", "shared", fav=True)
    a2 = _synced(assets, "a2", "shared", fav=False)
    syncer.sync_once()

    # The starred source is trashed (temporarily, maybe) -- the remaining
    # unstarred source is not enough evidence to unstar.
    _set_fav(assets, a1, True, is_trashed=True)
    syncer.sync_once()
    assert gphotos.favorite_calls == [("shared", True)]

    # Every source trashed: nothing is live, nothing is pushed.
    _set_fav(assets, a2, False, is_trashed=True)
    syncer.sync_once()
    assert gphotos.favorite_calls == [("shared", True)]


def test_a_source_still_in_flight_holds_the_unstar(tmp_path):
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)
    a1 = _synced(assets, "a1", "shared", fav=True)
    syncer.sync_once()
    # A second Immich asset with the same bytes arrives and is mid-retry,
    # already pointing at the item (e.g. recorded by a previous attempt).
    late = replace(BASE, immich_id="late", checksum="sum-late", is_favorite=True)
    assets.upsert_pending(late, Priority.BACKFILL)
    assets.claim_next(limit=1)
    assets.mark_synced("late", "shared", Outcome.ALREADY_PRESENT)
    assets.mark_retry("late", ErrorClass.TRANSIENT, "x", FakeClock().now())
    _set_fav(assets, a1, False)

    result = syncer.sync_once()
    assert result.held == 1
    assert gphotos.favorite_calls == [("shared", True)]


def test_a_star_the_user_set_in_google_is_never_removed(tmp_path):
    """The item was already in Google (dedup hit). Its star could be the
    user's own, so an Immich unstar is never mirrored onto it."""
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)
    a1 = _synced(assets, "a1", "pre-existing", fav=True, outcome=Outcome.ALREADY_PRESENT)
    syncer.sync_once()
    _set_fav(assets, a1, False)

    result = syncer.sync_once()
    syncer.sync_once()

    assert result.protected == 1
    assert gphotos.favorite_calls == [("pre-existing", True)]


def test_never_starred_items_are_never_touched(tmp_path):
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)
    for i in range(3):
        _synced(assets, f"p{i}", f"k{i}", fav=False, outcome=Outcome.ALREADY_PRESENT)
    syncer.sync_once()
    assert gphotos.favorite_calls == []


def test_the_ledger_survives_a_restart(tmp_path):
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    _synced(assets, "a1", "shared", fav=True)
    _synced(assets, "a2", "shared", fav=False)
    FavoriteSyncer(gphotos, assets).sync_once()

    reopened = AssetRepo(connect(tmp_path / "t.db"), FakeClock())
    FavoriteSyncer(gphotos, reopened).sync_once()
    assert gphotos.favorite_calls == [("shared", True)]


def test_a_star_flipped_while_the_write_was_in_flight_is_not_lost(tmp_path):
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)
    a1 = _synced(assets, "a1", "key", fav=True)
    real = gphotos.set_favorite
    flipped = {"done": False}

    def racing_set_favorite(media_key, is_favorite):
        real(media_key, is_favorite)
        if not flipped["done"]:
            flipped["done"] = True
            _set_fav(assets, a1, False)  # webhook lands mid-write

    gphotos.set_favorite = racing_set_favorite
    syncer.sync_once()
    assert assets.get("a1").favorite_synced is None  # not recorded as settled
    syncer.sync_once()
    assert gphotos.favorite_calls == [("key", True), ("key", False)]
    assert assets.get("a1").favorite_synced is False


def test_rate_limiting_ends_the_sweep_and_the_next_one_recovers(tmp_path):
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    syncer = FavoriteSyncer(gphotos, assets)
    for i in range(3):
        _synced(assets, f"a{i}", f"k{i}", fav=True)
    gphotos.fail_methods["set_favorite"] = GPhotosError("slow down", ErrorClass.RATE_LIMITED)

    first = syncer.sync_once()
    assert first.failed == 1  # stopped at the first rate limit

    del gphotos.fail_methods["set_favorite"]
    second = syncer.sync_once()
    assert second.synced == 3
    assert gphotos.favorites == {"k0": True, "k1": True, "k2": True}


def test_rows_pushed_before_the_ledger_existed_are_honoured(tmp_path):
    assets = _repo(tmp_path)
    gphotos = FakeGooglePhotosClient()
    a1 = _synced(assets, "a1", "legacy", fav=True)
    assets.mark_favorite_synced("a1", True)  # what the earlier PR revision wrote
    syncer = FavoriteSyncer(gphotos, assets)
    syncer.sync_once()
    assert gphotos.favorite_calls == []  # already starred, nothing to push

    _set_fav(assets, a1, False)
    syncer.sync_once()
    assert gphotos.favorite_calls == [("legacy", False)]  # we uploaded and starred it


def test_worker_and_sweeper_share_one_ledger_for_a_dedup_hit(tmp_path):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    gphotos = FakeGooglePhotosClient(present={"sum-a": "existing"})
    syncer = FavoriteSyncer(gphotos, assets)
    resolver = ByteResolver(
        FakeImmichClient(contents={"a1": b"ABC"}), scratch=tmp_path / "s", direct_roots=[]
    )
    worker = Worker(assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, favorites=syncer)
    assets.upsert_pending(replace(BASE, is_favorite=True), Priority.WEBHOOK)
    worker.process(assets.claim_next()[0])
    syncer.sync_once()
    assert gphotos.favorite_calls == [("existing", True)]

    _set_fav(assets, replace(BASE), False)
    assert syncer.sync_once().protected == 1
    assert gphotos.favorite_calls == [("existing", True)]


def test_toggling_favorites_off_and_on_resumes_from_the_ledger(tmp_path):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(
        FakeImmichClient(contents={"a1": b"ABC"}), scratch=tmp_path / "s", direct_roots=[]
    )
    off = Worker(
        assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, favorites_enabled=False
    )
    assets.upsert_pending(replace(BASE, is_favorite=True), Priority.WEBHOOK)
    off.process(assets.claim_next()[0])
    assert gphotos.favorite_calls == []

    FavoriteSyncer(gphotos, assets).sync_once()  # switched back on
    assert gphotos.favorite_calls == [(assets.get("a1").media_key, True)]
