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


def sync_asset(assets: AssetRepo, i: str) -> None:
    assets.upsert_pending(asset(i), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_synced(i, f"key-{i}", Outcome.UPLOADED)


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

    sweeper = DeletionSweeper(gphotos, assets, Settings(deletions_enabled=True))
    plan = sweeper.plan()
    assert plan.checksums == ["sum-a"]
    assert sweeper.execute(plan) == 1
    assert gphotos.trashed == ["sum-a"]
    stored = assets.get("a")
    assert stored.state is AssetState.INELIGIBLE
    assert stored.ineligible_reason == "deleted_from_immich"
    assert stored.media_key == "key-a"  # kept, so a re-add resolves instantly


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
