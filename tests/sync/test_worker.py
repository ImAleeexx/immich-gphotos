from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Filters, RetryPolicy
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.gphotos.protocol import GPhotosError
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, AssetState, ErrorClass, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.throttle import TokenBucket
from immich_gphotos.sync.worker import MAX_INLINE_THROTTLE_WAIT_SECONDS, Worker

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


@pytest.fixture
def rig(tmp_path):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    immich = FakeImmichClient(contents={"a1": b"ABC"})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    worker = Worker(assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock)
    return worker, assets, immich, gphotos, clock, tmp_path


def claim(assets: AssetRepo, asset: Asset = ASSET):
    assets.upsert_pending(asset, Priority.WEBHOOK)
    return assets.claim_next(limit=1)[0]


def test_new_asset_is_uploaded_and_marked_synced(rig):
    worker, assets, immich, gphotos, _, _ = rig
    result = worker.process(claim(assets))
    assert result.state is AssetState.SYNCED
    assert result.outcome is Outcome.UPLOADED
    assert gphotos.uploads == [("sum-a", "IMG_1.JPG")]
    assert assets.get("a1").media_key == result.media_key


def test_asset_already_in_google_is_never_downloaded(rig):
    worker, assets, immich, gphotos, _, _ = rig
    gphotos.present["sum-a"] = "existing-key"
    result = worker.process(claim(assets))
    assert result.outcome is Outcome.ALREADY_PRESENT
    assert result.media_key == "existing-key"
    assert immich.downloads == []  # the whole point: no bytes moved
    assert gphotos.uploads == []


def test_duplicate_checksum_resolves_locally_without_asking_google(rig):
    worker, assets, immich, gphotos, _, _ = rig
    worker.process(claim(assets))
    twin = replace(ASSET, immich_id="a2")
    result = worker.process(claim(assets, twin))
    assert result.outcome is Outcome.ALREADY_PRESENT
    assert len(gphotos.uploads) == 1  # the twin did not upload
    assert immich.downloads == ["a1"]  # nor download


def test_hidden_assets_are_ineligible_and_touch_nothing(rig):
    worker, assets, immich, gphotos, _, _ = rig
    result = worker.process(claim(assets, replace(ASSET, immich_id="m1", visibility="hidden")))
    assert result.state is AssetState.INELIGIBLE
    assert result.reason == "hidden"
    assert immich.downloads == []
    assert gphotos.uploads == []
    assert assets.get("m1").ineligible_reason == "hidden"


def test_transient_failure_schedules_a_retry(rig):
    worker, assets, _, gphotos, clock, _ = rig
    gphotos.fail_on["sum-a"] = GPhotosError("network", ErrorClass.TRANSIENT)
    result = worker.process(claim(assets))
    stored = assets.get("a1")
    assert result.state is AssetState.PENDING
    assert stored.attempts == 1
    assert stored.next_attempt_at == (clock.now() + timedelta(seconds=30)).isoformat()


def test_quarantine_after_max_attempts(rig):
    worker, assets, _, gphotos, clock, _ = rig
    gphotos.fail_on["sum-a"] = GPhotosError("network", ErrorClass.TRANSIENT)
    policy = RetryPolicy(jitter=0.0, max_attempts=2)
    worker = Worker(worker._assets, gphotos, worker._resolver, Filters(), policy, clock)
    stored = claim(assets)
    worker.process(stored)
    clock.advance(timedelta(minutes=10))
    worker.process(assets.claim_next(limit=1)[0])
    assert assets.get("a1").state is AssetState.FAILED


def test_auth_failure_halts_without_burning_an_attempt(rig):
    worker, assets, _, gphotos, _, _ = rig
    gphotos.fail_on["sum-a"] = GPhotosError("401", ErrorClass.AUTH_INVALID)
    result = worker.process(claim(assets))
    assert result.halt is True
    assert result.error_class is ErrorClass.AUTH_INVALID
    stored = assets.get("a1")
    assert stored.state is AssetState.PENDING
    assert stored.attempts == 0  # not the asset's fault


def test_downloaded_scratch_file_is_removed_even_when_upload_fails(rig):
    worker, assets, _, gphotos, _, tmp_path = rig
    gphotos.fail_on["sum-a"] = GPhotosError("network", ErrorClass.TRANSIENT)
    worker.process(claim(assets))
    scratch = tmp_path / "scratch"
    assert list(scratch.glob("*")) == []


def test_no_bandwidth_cap_configured_adds_no_delay_and_no_bucket(tmp_path):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    immich = FakeImmichClient(contents={"a1": b"ABC"})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    sleeps: list[float] = []
    worker = Worker(assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, sleep=sleeps.append)
    assert worker._bandwidth is None  # unset means no bucket is ever constructed

    result = worker.process(claim(assets))

    assert result.state is AssetState.SYNCED
    assert sleeps == []


def test_bandwidth_cap_delays_a_large_upload(tmp_path):
    """Driven entirely by a FakeClock and a recording sleep -- no real sleep."""
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    content = b"x" * 1000
    immich = FakeImmichClient(contents={"a1": content})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    bucket = TokenBucket(rate_bytes_per_second=100, clock=clock)
    sleeps: list[float] = []
    worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=bucket,
        sleep=sleeps.append,
    )

    result = worker.process(claim(assets, replace(ASSET, size_bytes=len(content))))

    assert result.state is AssetState.SYNCED
    # 1000 bytes at 100 bytes/sec, with a bucket that starts full at capacity
    # (== the rate): the first 100 bytes are free, the remaining 900 cost
    # 9.0 seconds -- and the caller (not the bucket) is what waits.
    assert sleeps == [9.0]


def test_bandwidth_cap_shared_across_calls_drains_the_same_bucket(tmp_path):
    """One bucket instance must be shared across every upload, or a pool of
    workers would each get their own full-rate allowance -- wrong by a
    factor of the pool size. Verified here via two sequential uploads
    against the same Worker/bucket."""
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    content = b"x" * 60
    immich = FakeImmichClient(contents={"a1": content, "a2": content})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    bucket = TokenBucket(rate_bytes_per_second=100, clock=clock)
    sleeps: list[float] = []
    worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=bucket,
        sleep=sleeps.append,
    )

    worker.process(claim(assets, replace(ASSET, size_bytes=len(content))))
    assert sleeps == []  # 60 of 100 tokens spent, still within capacity

    second = replace(ASSET, immich_id="a2", checksum="sum-a2", size_bytes=len(content))
    worker.process(claim(assets, second))
    # The second call drains the *same* bucket: 60 + 60 = 120 against a
    # 100-token capacity that has not had time to refill (FakeClock never
    # advanced), so it must wait for the 20-token shortfall.
    assert sleeps == [0.2]


def test_a_large_upload_under_a_tiny_cap_defers_instead_of_freezing_the_loop(tmp_path):
    """A wait at or above MAX_INLINE_THROTTLE_WAIT_SECONDS must never be slept
    out on the calling thread -- for a background-loop tick, that thread is
    the only one driving reconcile, backfill, album sync, the deletion sweep,
    requeue_stale_uploading and the pause-retry, so sleeping here would freeze
    all of them for the wait's whole duration (hours, at the documented
    bandwidth minimum, for a large file).

    Uncapped, 5,000,000 bytes at 1 byte/second is a ~58-day wait -- proving
    the defer path is taken, independent of the API-level guard
    (MIN_BANDWIDTH_BYTES_PER_SECOND) that keeps a rate this low from being
    configurable through the API in the first place; TokenBucket itself is
    still constructed directly with it here."""
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    content = b"x" * 5_000_000
    immich = FakeImmichClient(contents={"a1": content})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    bucket = TokenBucket(rate_bytes_per_second=1, clock=clock)
    expected_wait = bucket.take(len(content))
    assert expected_wait > 1_000_000  # the raw, uncapped wait is enormous
    bucket = TokenBucket(rate_bytes_per_second=1, clock=clock)  # fresh, undrained bucket
    sleeps: list[float] = []
    worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=bucket,
        sleep=sleeps.append,
    )

    result = worker.process(claim(assets, replace(ASSET, size_bytes=len(content))))

    assert result.state is AssetState.PENDING
    assert result.deferred is True
    assert sleeps == []  # never slept on the calling thread
    assert gphotos.uploads == []  # not uploaded yet
    stored = assets.get("a1")
    assert stored.attempts == 0  # not the asset's fault, no attempt burned
    assert stored.next_attempt_at == (clock.now() + timedelta(seconds=expected_wait)).isoformat()


def test_a_deferred_upload_is_not_metered_twice_on_retry(tmp_path):
    """The bucket is charged exactly once for a given transfer -- at the call
    that decided to defer, not again when the asset is reclaimed. Re-metering
    on retry would double-charge the same bytes, and since a TokenBucket's
    capacity never exceeds one second's worth of tokens, that second charge
    would recreate almost the same enormous wait on every subsequent retry --
    the asset would never actually upload."""
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    content = b"x" * 5_000_000
    immich = FakeImmichClient(contents={"a1": content})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    bucket = TokenBucket(rate_bytes_per_second=1, clock=clock)
    sleeps: list[float] = []
    worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=bucket,
        sleep=sleeps.append,
    )

    first = worker.process(claim(assets, replace(ASSET, size_bytes=len(content))))
    assert first.deferred is True
    stored = assets.get("a1")
    wait_seconds = (datetime.fromisoformat(stored.next_attempt_at) - clock.now()).total_seconds()
    clock.advance(timedelta(seconds=wait_seconds))

    reclaimed = assets.claim_next(limit=1)[0]
    result = worker.process(reclaimed)

    assert result.state is AssetState.SYNCED
    assert sleeps == []  # the wait already elapsed via the requeue, not a sleep
    assert gphotos.uploads == [("sum-a", "IMG_1.JPG")]


def test_wait_just_under_the_bound_still_sleeps_inline(tmp_path):
    """A wait comfortably below MAX_INLINE_THROTTLE_WAIT_SECONDS is still
    slept out inline, in a single call, rather than deferred -- deferring
    every capped upload, however small the wait, would mean a modest cap
    never actually uploads anything on the first pass."""
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    rate = 100
    # 100 bytes/sec bucket starts full (100 tokens): the first 100 bytes are
    # free, so 2900 bytes costs (2900 - 100) / 100 == 28.0s -- comfortably
    # under the 30s bound.
    content = b"x" * 2900
    immich = FakeImmichClient(contents={"a1": content})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    bucket = TokenBucket(rate_bytes_per_second=rate, clock=clock)
    sleeps: list[float] = []
    worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=bucket,
        sleep=sleeps.append,
    )

    result = worker.process(claim(assets, replace(ASSET, size_bytes=len(content))))

    assert result.state is AssetState.SYNCED
    assert sleeps == [28.0]
    assert sleeps[0] < MAX_INLINE_THROTTLE_WAIT_SECONDS


def test_several_assets_deferred_in_one_pass_get_staggered_deadlines(tmp_path):
    """C1 regression guard at the worker level. Deferring is only a real
    throttle if the deadlines it hands out queue behind one another: while
    `TokenBucket.take` forgave the debt it had just charged, every asset
    metered in the same instant was told to wait the same number of seconds,
    so a whole backlog came due at the same moment and then uploaded
    unmetered (each retry short-circuits on `_throttle_prepaid`). At 100
    bytes/second, three 10,000-byte assets must come due 100 seconds apart."""
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    content = b"x" * 10_000
    ids = ["a1", "a2", "a3"]
    immich = FakeImmichClient(contents=dict.fromkeys(ids, content))
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    sleeps: list[float] = []
    worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=TokenBucket(rate_bytes_per_second=100, clock=clock),
        sleep=sleeps.append,
    )

    for immich_id in ids:
        stored = claim(
            assets,
            replace(ASSET, immich_id=immich_id, checksum=f"sum-{immich_id}", size_bytes=len(content)),
        )
        assert worker.process(stored).deferred is True

    start = clock.now()
    deadlines = [(datetime.fromisoformat(assets.get(i).next_attempt_at) - start).total_seconds() for i in ids]
    assert deadlines == [99.0, 199.0, 299.0]
    assert sleeps == []
    assert gphotos.uploads == []


def test_a_deferred_asset_that_then_becomes_ineligible_drops_its_prepaid_entry(tmp_path):
    """M1. `_throttle_prepaid` is only discarded inside `_throttle_upload`,
    which every earlier return in `process()` skips. An asset that defers and
    is then trashed in Immich returns `ineligible` before the throttle runs,
    and its id used to sit in the set for the life of the process -- the row
    is terminal now, so nothing will ever come back to clear it."""
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    content = b"x" * 10_000
    immich = FakeImmichClient(contents={"a1": content})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=TokenBucket(rate_bytes_per_second=100, clock=clock),
        sleep=[].append,
    )
    big = replace(ASSET, size_bytes=len(content))
    assert worker.process(claim(assets, big)).deferred is True
    assert worker._throttle_prepaid == {"a1"}  # charged, waiting to be reclaimed

    # The user trashes it while it waits; the reconciler's upsert_pending
    # records that, and the retry never reaches the throttle.
    clock.advance(timedelta(seconds=99))
    assets.upsert_pending(replace(big, is_trashed=True), Priority.WEBHOOK)
    result = worker.process(assets.claim_next(limit=1)[0])

    assert result.state is AssetState.INELIGIBLE
    assert result.reason == "trashed"
    assert worker._throttle_prepaid == set()
