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
    immich = FakeImmichClient(contents={})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    worker = Worker(assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock)
    return assets, events, gphotos, worker, clock, immich


def test_tick_processes_up_to_the_limit(rig):
    assets, events, gphotos, worker, clock, immich = rig
    for i in range(5):
        assets.upsert_pending(asset(str(i)), Priority.WEBHOOK)
    runtime = Runtime(assets, worker, Settings(), clock, events, immich=immich)
    assert runtime.tick(limit=3).processed == 3
    assert runtime.tick(limit=3).processed == 2


def test_outside_the_window_hash_checks_proceed_but_transfer_defers(rig):
    """The window gates byte transfer only. An asset the dedup checks clear
    for free (already present remotely) must still sync at any hour; only an
    asset that genuinely needs bytes moved waits for the window -- and doing
    so must not fail it, burn a retry attempt, or halt the rest of the batch."""
    assets, events, gphotos, worker, clock, immich = rig
    gphotos.present["sum-present"] = "existing-key"  # already in Google
    assets.upsert_pending(asset("present"), Priority.WEBHOOK)
    assets.upsert_pending(asset("needs-upload"), Priority.WEBHOOK)

    settings = Settings(window=Window(start=time(1, 0), end=time(6, 0)))  # it is 12:00
    result = Runtime(assets, worker, settings, clock, events, immich=immich).tick(limit=5)

    assert result.window_closed is True
    assert result.processed == 2
    assert result.deferred == 1
    assert gphotos.uploads == []  # no bytes moved outside the window

    already_present = assets.get("present")
    assert already_present.state == AssetState.SYNCED  # hash check cleared it for free

    deferred = assets.get("needs-upload")
    assert deferred.state == AssetState.PENDING
    assert deferred.attempts == 0  # not the asset's fault, no attempt burned


def test_an_auth_failure_pauses_the_runtime(rig):
    """worker_threads=1 (each wave holds exactly one asset) reproduces the
    original strictly-sequential guarantee: nothing claimed behind a halted
    asset is even started."""
    assets, events, gphotos, worker, clock, immich = rig
    gphotos.fail_on["sum-a"] = GPhotosError("401", ErrorClass.AUTH_INVALID)
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    assets.upsert_pending(asset("b"), Priority.WEBHOOK)

    runtime = Runtime(assets, worker, Settings(worker_threads=1), clock, events, immich=immich)
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


def test_a_pool_size_of_one_never_creates_a_thread_pool(rig):
    assets, events, gphotos, worker, clock, immich = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    runtime = Runtime(assets, worker, Settings(worker_threads=1), clock, events, immich=immich)
    runtime.tick(limit=5)
    assert runtime._pool is None


def test_pool_halts_the_batch_without_starting_undispatched_waves(rig):
    """With a real pool (worker_threads=2), a halting asset stops any *later*
    wave from ever being dispatched -- and that undispatched remainder is
    requeued with no attempt burned, exactly like the sequential path.
    Assets in the *same* wave as the halting one may legitimately have
    already run concurrently with it; that is not "unprocessed" and must not
    be requeued."""
    assets, events, gphotos, worker, clock, immich = rig
    gphotos.fail_on["sum-a"] = GPhotosError("401", ErrorClass.AUTH_INVALID)
    for name in ("a", "b", "c", "d"):
        assets.upsert_pending(asset(name), Priority.WEBHOOK)

    runtime = Runtime(assets, worker, Settings(worker_threads=2), clock, events, immich=immich)
    result = runtime.tick(limit=4)

    assert result.halted is True
    assert result.processed == 2  # only the first wave, [a, b], ever ran
    assert runtime.paused_reason is not None

    halted = assets.get("a")
    assert halted.state == AssetState.PENDING
    assert halted.attempts == 0  # not the asset's fault

    # "b" shared a's wave -- both were dispatched to the pool together, and
    # b had no configured failure, so it completed normally.
    completed = assets.get("b")
    assert completed.state == AssetState.SYNCED

    # "c" and "d" belonged to a wave that was never dispatched at all.
    for name in ("c", "d"):
        stranded = assets.get(name)
        assert stranded.state == AssetState.PENDING
        assert stranded.attempts == 0

    runtime.close()


def test_a_paused_runtime_does_no_work_until_resumed(rig):
    assets, events, gphotos, worker, clock, immich = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    runtime = Runtime(assets, worker, Settings(), clock, events, immich=immich)
    runtime.pause("credentials expired")
    assert runtime.tick().processed == 0
    runtime.resume()
    assert runtime.tick().processed == 1


def test_album_allowlist_is_resolved_and_enforced_per_tick(rig, tmp_path):
    """Filters.album_allowlist names Immich album ids; Runtime resolves them
    to member asset ids via ImmichClient once per tick and the worker admits
    only members.

    Built with its own Worker (rather than the `rig` one, which is fixed to
    plain `Filters()`) since the allowlist lives on `Filters`, which a real
    Worker closes over at construction -- exactly as `composition.py` builds
    a Worker and a Runtime from the same `Settings` in production."""
    assets, events, gphotos, _, clock, immich = rig
    immich.albums["album-1"] = ["in-album"]
    assets.upsert_pending(asset("in-album"), Priority.WEBHOOK)
    assets.upsert_pending(asset("not-in-album"), Priority.WEBHOOK)

    filters = Filters(album_allowlist=frozenset({"album-1"}))
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    worker = Worker(assets, gphotos, resolver, filters, RetryPolicy(jitter=0.0), clock)
    settings = Settings(filters=filters)
    result = Runtime(assets, worker, settings, clock, events, immich=immich).tick(limit=5)

    assert result.processed == 2
    admitted = assets.get("in-album")
    assert admitted.state == AssetState.SYNCED

    excluded = assets.get("not-in-album")
    assert excluded.state == AssetState.INELIGIBLE
    assert excluded.ineligible_reason == "album_excluded"


def test_events_are_recorded_and_bounded(rig):
    assets, events, _, worker, clock, immich = rig
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
