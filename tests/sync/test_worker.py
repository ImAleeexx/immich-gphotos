from dataclasses import replace
from datetime import timedelta

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
from immich_gphotos.sync.worker import THROTTLE_SLEEP_CHUNK_SECONDS, Worker

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


def test_a_large_upload_under_a_tiny_cap_sleeps_the_full_wait_in_bounded_chunks(tmp_path):
    """The throttle cap must be honoured, not silently violated: an earlier
    version of this fix bounded any single sleep to 30s by truncating the
    wait outright, which meant TokenBucket still deducted the full token
    cost while the caller waited only a fraction of it -- so a large upload
    under a low cap finished, and the next one started, faster than the
    configured rate actually allows.

    _throttle_upload now sleeps the *entire* wait `TokenBucket.take` hands
    back, just broken into chunks of at most THROTTLE_SLEEP_CHUNK_SECONDS so
    no single `time.sleep` call is asked for a pathological duration.
    Uncapped, 5,000,000 bytes at 1 byte/second is a ~58-day wait -- proving
    the chunking here, independent of the API-level guard
    (MIN_BANDWIDTH_BYTES_PER_SECOND) that keeps a rate this low from being
    configurable through the API in the first place; TokenBucket itself is
    still constructed directly with it below."""
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

    assert result.state is AssetState.SYNCED
    assert len(sleeps) > 1  # chunked, not one giant call
    assert all(chunk <= THROTTLE_SLEEP_CHUNK_SECONDS for chunk in sleeps)
    assert sum(sleeps) == pytest.approx(expected_wait)  # the full wait is honoured
