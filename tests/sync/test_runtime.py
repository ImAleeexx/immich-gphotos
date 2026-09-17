from datetime import UTC, datetime, time

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Filters, RetryPolicy, Settings, Window
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.gphotos.protocol import GPhotosError
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, AssetState, ErrorClass, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.runtime import Runtime
from immich_gphotos.sync.worker import Worker


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
    clock = FakeClock(datetime(2026, 9, 17, 12, 0, tzinfo=UTC))
    conn = connect(tmp_path / "t.db")
    assets = AssetRepo(conn, clock)
    events = EventRepo(conn, clock)
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(FakeImmichClient(contents={}), scratch=tmp_path / "scratch")
    worker = Worker(assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock)
    return assets, events, gphotos, worker, clock


def test_tick_processes_up_to_the_limit(rig):
    assets, events, gphotos, worker, clock = rig
    for i in range(5):
        assets.upsert_pending(asset(str(i)), Priority.WEBHOOK)
    runtime = Runtime(assets, worker, Settings(), clock, events)
    assert runtime.tick(limit=3).processed == 3
    assert runtime.tick(limit=3).processed == 2


def test_tick_does_nothing_outside_the_window(rig):
    assets, events, gphotos, worker, clock = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    settings = Settings(window=Window(start=time(1, 0), end=time(6, 0)))  # it is 12:00
    result = Runtime(assets, worker, settings, clock, events).tick()
    assert result.window_closed is True
    assert result.processed == 0
    assert gphotos.uploads == []


def test_an_auth_failure_pauses_the_runtime(rig):
    assets, events, gphotos, worker, clock = rig
    gphotos.fail_on["sum-a"] = GPhotosError("401", ErrorClass.AUTH_INVALID)
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    assets.upsert_pending(asset("b"), Priority.WEBHOOK)

    runtime = Runtime(assets, worker, Settings(), clock, events)
    result = runtime.tick(limit=5)

    assert result.halted is True
    assert runtime.paused_reason is not None
    assert gphotos.uploads == []  # "b" was not attempted after the halt

    # "b" was claimed (flipped to UPLOADING) behind the halted "a" but never
    # handed to the worker. It must be back in PENDING with no attempt
    # counted against it, not stranded in UPLOADING until a restart.
    stranded = assets.get("b")
    assert stranded.state == AssetState.PENDING
    assert stranded.attempts == 0


def test_a_paused_runtime_does_no_work_until_resumed(rig):
    assets, events, gphotos, worker, clock = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    runtime = Runtime(assets, worker, Settings(), clock, events)
    runtime.pause("credentials expired")
    assert runtime.tick().processed == 0
    runtime.resume()
    assert runtime.tick().processed == 1


def test_events_are_recorded_and_bounded(rig):
    assets, events, _, worker, clock = rig
    for i in range(5):
        events.add("info", f"message {i}")
    assert len(events.recent(3)) == 3
    assert events.recent(1)[0]["message"] == "message 4"


def test_event_ring_discards_the_oldest(tmp_path):
    clock = FakeClock()
    events = EventRepo(connect(tmp_path / "t.db"), clock, limit=3)
    for i in range(6):
        events.add("info", f"m{i}")
    messages = [e["message"] for e in events.recent(10)]
    assert messages == ["m5", "m4", "m3"]
