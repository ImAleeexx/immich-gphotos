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
