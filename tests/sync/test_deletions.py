from dataclasses import replace

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import DeletionPolicy, Settings
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.models import Asset, AssetState, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.deletions import DeletionSweeper, deletion_allowed


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
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    gphotos = FakeGooglePhotosClient()
    return assets, gphotos


def sync_asset(assets: AssetRepo, i: str, upload_sha1: str | None = None) -> None:
    assets.upsert_pending(asset(i), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_synced(i, f"key-{i}", Outcome.UPLOADED, upload_sha1=upload_sha1)


def trash_asset(assets: AssetRepo, i: str) -> None:
    assets.upsert_pending(replace(asset(i), is_trashed=True), Priority.RECONCILE)


def test_fraction_bound_blocks_a_mass_deletion():
    policy = DeletionPolicy(max_fraction=0.10, max_absolute=500)
    ok, reason = deletion_allowed(count=200, synced_total=1000, policy=policy)
    assert ok is False
    assert "fraction" in reason


def test_absolute_bound_blocks_even_a_small_fraction():
    policy = DeletionPolicy(max_fraction=0.90, max_absolute=500)
    ok, reason = deletion_allowed(count=600, synced_total=10_000, policy=policy)
    assert ok is False
    assert "absolute" in reason


def test_ordinary_deletion_is_allowed():
    ok, reason = deletion_allowed(count=3, synced_total=1000, policy=DeletionPolicy())
    assert (ok, reason) == (True, None)


def test_nothing_to_delete_is_allowed():
    assert deletion_allowed(count=0, synced_total=0, policy=DeletionPolicy())[0] is True


def test_zero_synced_total_with_nonzero_count_blocks_rather_than_dividing():
    """A library that reports zero synced assets is not a library the breaker
    can measure a fraction against, so it must block rather than allow."""
    ok, reason = deletion_allowed(count=5, synced_total=0, policy=DeletionPolicy())
    assert ok is False
    assert "fraction" in reason


def test_reason_names_both_bounds_when_both_are_breached():
    policy = DeletionPolicy(max_fraction=0.10, max_absolute=5)
    ok, reason = deletion_allowed(count=600, synced_total=1000, policy=policy)
    assert ok is False
    assert "absolute" in reason
    assert "fraction" in reason


def test_disabled_sweeper_plans_nothing(rig):
    assets, gphotos = rig
    sync_asset(assets, "a")
    trash_asset(assets, "a")
    sweeper = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=False))
    assert sweeper.plan().checksums == []


def test_enabled_sweeper_trashes_in_google_and_marks_the_row(rig):
    assets, gphotos = rig
    # A library large enough that trashing one asset stays comfortably under
    # the default 10% circuit-breaker bound (1/20 = 5%), so this exercises the
    # ordinary allowed-deletion path rather than tripping the breaker.
    sync_asset(assets, "a")
    for i in range(19):
        sync_asset(assets, f"b{i}")
    trash_asset(assets, "a")
    # A pre-hashing row (no upload_sha1): its checksum is only used once
    # Google confirms it maps to exactly this row's recorded item.
    gphotos.present["sum-a"] = "key-a"

    sweeper = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True))
    plan = sweeper.plan()
    assert plan.checksums == ["sum-a"]  # verified fallback to the checksum
    assert sweeper.execute(plan) == 1
    assert gphotos.trashed == ["sum-a"]
    stored = assets.get("a")
    assert stored.state is AssetState.INELIGIBLE
    assert stored.ineligible_reason == "deleted_from_immich"
    assert stored.media_key == "key-a"  # kept, so a re-add resolves instantly


def test_rows_with_an_upload_sha1_trash_by_the_content_hash(rig):
    """Google pairs dedup-key trash with the content SHA-1 it was given at
    upload. An External Library row's Immich checksum is sha1(path) -- no
    Google item answers to it -- so the sweeper must trash by the hash the
    worker recorded when the bytes went up."""
    assets, gphotos = rig
    sync_asset(assets, "a", upload_sha1="content-a")
    for i in range(19):
        sync_asset(assets, f"b{i}")
    trash_asset(assets, "a")

    sweeper = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True))
    plan = sweeper.plan()
    assert plan.checksums == ["content-a"]
    assert sweeper.execute(plan) == 1
    assert gphotos.trashed == ["content-a"]


def test_blocked_plan_refuses_to_execute(rig):
    assets, gphotos = rig
    for i in range(10):
        sync_asset(assets, str(i))
    for i in range(10):
        trash_asset(assets, str(i))

    sweeper = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True))
    plan = sweeper.plan()
    assert plan.blocked is True
    assert sweeper.execute(plan) == 0
    assert gphotos.trashed == []


def _big_library(assets):
    for i in range(30):
        sync_asset(assets, f"b{i}", upload_sha1=f"content-b{i}")


def test_a_legacy_row_without_proof_of_its_google_item_is_never_trashed(rig):
    """External Library rows carry a virtual sha1(path) checksum. With no
    recorded upload hash and no Google confirmation that the checksum names
    this row's item, deletion fails closed instead of guessing."""
    assets, gphotos = rig
    _big_library(assets)
    sync_asset(assets, "ext")  # upload_sha1 NULL, checksum unknown to Google
    trash_asset(assets, "ext")
    gphotos.present["sum-ext"] = "some-other-item"

    sweeper = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True))
    plan = sweeper.plan()
    assert plan.checksums == []
    assert plan.unverified_ids == ["ext"]
    sweeper.execute(plan)
    assert gphotos.trashed == []
    assert assets.get("ext").state is AssetState.SYNCED  # left for a human to check


def test_an_item_another_live_asset_still_maps_to_is_never_trashed(rig):
    assets, gphotos = rig
    _big_library(assets)
    for i in ("dup1", "dup2"):
        assets.upsert_pending(asset(i), Priority.WEBHOOK)
        assets.claim_next(limit=1)
        assets.mark_synced(i, "shared-key", Outcome.ALREADY_PRESENT, upload_sha1="content-shared")
    trash_asset(assets, "dup1")

    sweeper = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True))
    plan = sweeper.plan()
    assert plan.checksums == []
    assert plan.retained_ids == ["dup1"]
    sweeper.execute(plan)
    assert gphotos.trashed == []
    assert assets.get("dup1").ineligible_reason == "deleted_from_immich"

    # Once the last asset mapping to it is trashed too, the item goes.
    trash_asset(assets, "dup2")
    plan = sweeper.plan()
    assert plan.checksums == ["content-shared"]
    sweeper.execute(plan)
    assert gphotos.trashed == ["content-shared"]


def test_a_derived_motion_photo_added_by_reprocess_is_trashed_with_its_still(rig):
    assets, gphotos = rig
    _big_library(assets)
    sync_asset(assets, "still", upload_sha1="content-still")
    assets.record_live_photo(
        "still",
        "mov",
        mode="derived_motion",
        status="complete",
        derived_sha1="content-derived",
        derived_media_key="key-derived",
    )
    trash_asset(assets, "still")
    plan = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True)).plan()
    assert plan.checksums == ["content-still", "content-derived"]


def _original_pair(assets, *, mov_hash="content-mov", trash_mov=False):
    sync_asset(assets, "still", upload_sha1="content-still")
    sync_asset(assets, "mov", upload_sha1=mov_hash)  # the hidden clip, its own Google item
    assets.record_live_photo(
        "still",
        "mov",
        mode="original_pair",
        status="complete",
        still_sha1="content-still",
        video_sha1=mov_hash,
        video_media_key="key-mov",
    )
    trash_asset(assets, "still")
    if trash_mov:
        trash_asset(assets, "mov")


def test_original_pair_mov_is_trashed_with_its_still_even_if_immich_never_reports_it(rig):
    assets, gphotos = rig
    _big_library(assets)
    _original_pair(assets)  # Immich reported the still trashed, never the hidden .MOV
    sweeper = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True))
    plan = sweeper.plan()
    assert plan.checksums == ["content-still", "content-mov"]
    assert plan.asset_ids == ["still", "mov"]
    sweeper.execute(plan)
    assert gphotos.trashed == ["content-still", "content-mov"]
    assert assets.get("mov").ineligible_reason == "deleted_from_immich"
    # Nothing left to do on the next tick: no double trash.
    assert sweeper.plan().checksums == []


def test_original_pair_mov_reported_trashed_too_is_trashed_once(rig):
    assets, gphotos = rig
    _big_library(assets)
    _original_pair(assets, trash_mov=True)
    plan = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True)).plan()
    assert sorted(plan.checksums) == ["content-mov", "content-still"]
    assert sorted(plan.asset_ids) == ["mov", "still"]


def test_original_pair_mov_still_backing_up_another_asset_is_kept(rig):
    assets, gphotos = rig
    _big_library(assets)
    _original_pair(assets)
    assets.upsert_pending(asset("other-copy"), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_synced("other-copy", "key-mov", Outcome.ALREADY_PRESENT, upload_sha1="content-mov")
    plan = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True)).plan()
    assert plan.checksums == ["content-still"]
    assert plan.asset_ids == ["still"]


def test_original_pair_mov_without_a_provable_hash_fails_closed(rig):
    assets, gphotos = rig
    _big_library(assets)
    sync_asset(assets, "still", upload_sha1="content-still")
    sync_asset(assets, "mov")  # legacy: no upload hash, Google does not confirm its checksum
    assets.record_live_photo(
        "still", "mov", mode="original_pair", status="complete", video_media_key="key-mov"
    )
    trash_asset(assets, "still")
    plan = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True)).plan()
    assert plan.checksums == ["content-still"]
    assert plan.unverified_ids == ["mov"]

    gphotos.present["sum-mov"] = "key-mov"  # now Google confirms the mapping
    plan = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True)).plan()
    assert plan.checksums == ["content-still", "sum-mov"]
