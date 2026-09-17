from datetime import timedelta

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.models import Asset, AssetState, ErrorClass, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect

CHECKSUM = "qvTGHdzF6KLavt4PO0gs2a6pQ00="


def make_asset(asset_id: str = "a1", checksum: str = CHECKSUM) -> Asset:
    return Asset(
        immich_id=asset_id,
        checksum=checksum,
        filename=f"{asset_id}.jpg",
        type="IMAGE",
        size_bytes=1024,
        immich_updated_at="2026-09-17T10:00:00Z",
        original_path=f"/data/upload/{asset_id}.jpg",
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
    )


@pytest.fixture
def repo(tmp_path):
    clock = FakeClock()
    conn = connect(tmp_path / "test.db")
    return AssetRepo(conn, clock), clock


def test_upsert_then_claim_returns_the_asset(repo):
    r, _ = repo
    assert r.upsert_pending(make_asset(), Priority.WEBHOOK) is True
    claimed = r.claim_next(limit=5)
    assert [s.asset.immich_id for s in claimed] == ["a1"]
    assert claimed[0].state is AssetState.UPLOADING


def test_claiming_twice_yields_nothing_the_second_time(repo):
    r, _ = repo
    r.upsert_pending(make_asset(), Priority.WEBHOOK)
    assert len(r.claim_next(limit=5)) == 1
    assert r.claim_next(limit=5) == []


def test_lower_priority_number_is_claimed_first(repo):
    r, _ = repo
    r.upsert_pending(make_asset("old"), Priority.BACKFILL)
    r.upsert_pending(make_asset("new"), Priority.WEBHOOK)
    assert [s.asset.immich_id for s in r.claim_next(limit=1)] == ["new"]


def test_upsert_does_not_downgrade_priority(repo):
    r, _ = repo
    r.upsert_pending(make_asset(), Priority.WEBHOOK)
    r.upsert_pending(make_asset(), Priority.BACKFILL)
    assert r.get("a1").priority is Priority.WEBHOOK


def test_upsert_skips_assets_already_terminal(repo):
    r, _ = repo
    r.upsert_pending(make_asset(), Priority.WEBHOOK)
    r.claim_next(limit=1)
    r.mark_synced("a1", "mediakey1", Outcome.UPLOADED)
    assert r.upsert_pending(make_asset(), Priority.RECONCILE) is False
    assert r.get("a1").state is AssetState.SYNCED


def test_media_key_is_reused_across_duplicate_checksums(repo):
    r, _ = repo
    r.upsert_pending(make_asset("a1"), Priority.WEBHOOK)
    r.claim_next(limit=1)
    r.mark_synced("a1", "mediakey1", Outcome.UPLOADED)
    assert r.media_key_for_checksum(CHECKSUM) == "mediakey1"
    assert r.media_key_for_checksum("other") is None


def test_retry_sets_next_attempt_and_returns_to_pending(repo):
    r, clock = repo
    r.upsert_pending(make_asset(), Priority.WEBHOOK)
    r.claim_next(limit=1)
    r.mark_retry("a1", ErrorClass.TRANSIENT, "boom", clock.now() + timedelta(minutes=5))
    stored = r.get("a1")
    assert stored.state is AssetState.PENDING
    assert stored.attempts == 1
    assert r.claim_next(limit=5) == []  # not due yet
    clock.advance(timedelta(minutes=6))
    assert len(r.claim_next(limit=5)) == 1


def test_failed_assets_are_quarantined_not_reclaimed(repo):
    r, clock = repo
    r.upsert_pending(make_asset(), Priority.WEBHOOK)
    r.claim_next(limit=1)
    r.mark_failed("a1", ErrorClass.UNKNOWN, "gave up")
    clock.advance(timedelta(days=1))
    assert r.claim_next(limit=5) == []
    assert r.get("a1").state is AssetState.FAILED


def test_ineligible_records_reason(repo):
    r, _ = repo
    r.upsert_pending(make_asset(), Priority.WEBHOOK)
    r.claim_next(limit=1)
    r.mark_ineligible("a1", "hidden")
    assert r.get("a1").ineligible_reason == "hidden"


def test_counts_by_state(repo):
    r, _ = repo
    r.upsert_pending(make_asset("a1"), Priority.WEBHOOK)
    r.upsert_pending(make_asset("a2", "b" * 27 + "="), Priority.WEBHOOK)
    r.claim_next(limit=1)
    r.mark_synced("a1", "k", Outcome.ALREADY_PRESENT)
    assert r.counts_by_state() == {"synced": 1, "pending": 1}


def test_terminal_rows_still_track_being_trashed(repo):
    """The deletion sweeper depends on this: a synced asset must record that it
    was later trashed in Immich, even though it is no longer enqueued."""
    from dataclasses import replace

    r, _ = repo
    r.upsert_pending(make_asset(), Priority.WEBHOOK)
    r.claim_next(limit=1)
    r.mark_synced("a1", "mediakey1", Outcome.UPLOADED)

    assert r.upsert_pending(replace(make_asset(), is_trashed=True), Priority.RECONCILE) is False
    stored = r.get("a1")
    assert stored.asset.is_trashed is True
    assert stored.state is AssetState.SYNCED


def test_stale_uploading_rows_are_requeued_after_a_crash(repo):
    r, clock = repo
    r.upsert_pending(make_asset(), Priority.WEBHOOK)
    r.claim_next(limit=1)
    clock.advance(timedelta(hours=2))
    assert r.requeue_stale_uploading(timedelta(hours=1)) == 1
    assert r.get("a1").state is AssetState.PENDING
