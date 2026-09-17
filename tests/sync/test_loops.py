import threading
from datetime import timedelta

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.sync.loops import BackgroundLoops


class Spy:
    def __init__(self, **returns):
        self.calls = []
        self._returns = returns

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append(name)
            return self._returns.get(name)

        return record


class StubRuntime(Spy):
    paused_reason = None


class StubBackfill(Spy):
    def __init__(self, running: bool):
        super().__init__()
        self._running = running

    def is_running(self):
        return self._running

    def run_slice(self, pages=1):
        self.calls.append("run_slice")
        return None


class StubAssets(Spy):
    def requeue_stale_uploading(self, older_than):
        self.calls.append("requeue_stale_uploading")
        return 0


@pytest.fixture
def rig(tmp_path):
    clock = FakeClock()
    events = EventRepo(connect(tmp_path / "t.db"), clock)
    return clock, events


def loops(clock, events, settings, backfill_running=False, sweeper=None, assets=None):
    return BackgroundLoops(
        runtime=StubRuntime(),
        reconciler=Spy(),
        backfill=StubBackfill(backfill_running),
        album_mirror=Spy(),
        deletion_sweeper=sweeper or Spy(),
        settings=settings,
        clock=clock,
        events=events,
        assets=assets or StubAssets(),
    )


def test_first_iteration_reconciles_and_ticks(rig):
    clock, events = rig
    loop = loops(clock, events, Settings())
    ran = loop.iterate()
    assert ran["reconciled"] is True
    assert "tick" in loop._runtime.calls


def test_reconcile_waits_for_the_interval(rig):
    clock, events = rig
    loop = loops(clock, events, Settings(reconcile_interval=timedelta(minutes=15)))
    loop.iterate()
    clock.advance(timedelta(minutes=5))
    assert loop.iterate()["reconciled"] is False
    clock.advance(timedelta(minutes=11))
    assert loop.iterate()["reconciled"] is True


def test_backfill_slices_only_while_running(rig):
    clock, events = rig
    idle = loops(clock, events, Settings(), backfill_running=False)
    idle.iterate()
    assert "run_slice" not in idle._backfill.calls

    busy = loops(clock, events, Settings(), backfill_running=True)
    busy.iterate()
    assert "run_slice" in busy._backfill.calls


def test_albums_are_mirrored_only_when_enabled(rig):
    clock, events = rig
    off = loops(clock, events, Settings(albums_enabled=False))
    off.iterate()
    assert off._album_mirror.calls == []

    on = loops(clock, events, Settings(albums_enabled=True))
    on.iterate()
    assert "sync_once" in on._album_mirror.calls


def test_deletion_sweep_runs_only_when_enabled(rig):
    clock, events = rig
    off = loops(clock, events, Settings(deletions_enabled=False))
    off.iterate()
    assert off._deletion_sweeper.calls == []


def test_a_blocked_deletion_plan_is_reported_as_an_event(rig):
    clock, events = rig

    class BlockedSweeper:
        def plan(self):
            from immich_gphotos.sync.deletions import DeletionPlan

            return DeletionPlan(
                checksums=["x"], asset_ids=["a"], blocked=True, reason="absolute limit exceeded: 900 > 500"
            )

        def execute(self, plan):
            raise AssertionError("must not execute a blocked plan")

    loop = loops(clock, events, Settings(deletions_enabled=True), sweeper=BlockedSweeper())
    loop.iterate()
    assert any("absolute limit" in e["message"] for e in events.recent(10))


def test_a_paused_runtime_is_retried_after_the_cooldown(rig):
    """Without this the service would stay halted forever after one auth failure."""
    clock, events = rig

    class PausedRuntime(Spy):
        paused_reason = "auth_invalid"

        def resume(self):
            self.calls.append("resume")
            type(self).paused_reason = None

    loop = BackgroundLoops(
        runtime=PausedRuntime(),
        reconciler=Spy(),
        backfill=StubBackfill(False),
        album_mirror=Spy(),
        deletion_sweeper=Spy(),
        settings=Settings(),
        clock=clock,
        events=events,
        assets=StubAssets(),
    )
    assert loop.iterate()["resumed"] is False  # cooldown starts now
    clock.advance(timedelta(minutes=5))
    assert loop.iterate()["resumed"] is False
    clock.advance(timedelta(minutes=6))
    assert loop.iterate()["resumed"] is True
    PausedRuntime.paused_reason = "auth_invalid"  # restore for other tests


def test_a_tick_failure_does_not_abort_the_rest_of_the_iteration(rig):
    """I4: every other step in iterate() goes through _safely, but tick()
    did not -- a raise from it (I1's RuntimeError from a settings save
    racing an in-flight tick, an Immich failure inside the album allowlist
    resolution, or a sqlite error from mark_ineligible/
    media_key_for_checksum, both of which sit outside Worker.process's own
    try block) used to abort this whole iterate() call, skipping reconcile,
    stale-upload recovery, album sync, deletions and backfill for that
    pass."""
    clock, events = rig

    class FailingRuntime(Spy):
        paused_reason = None

        def tick(self):
            raise RuntimeError("boom")

    assets = StubAssets()
    loop = BackgroundLoops(
        runtime=FailingRuntime(),
        reconciler=Spy(),
        backfill=StubBackfill(False),
        album_mirror=Spy(),
        deletion_sweeper=Spy(),
        settings=Settings(),
        clock=clock,
        events=events,
        assets=assets,
    )

    ran = loop.iterate()

    assert ran["reconciled"] is True  # the rest of the pass still ran
    assert "requeue_stale_uploading" in assets.calls
    assert any("tick failed" in e["message"] for e in events.recent(10))


def test_run_forever_stops_when_the_event_is_set(rig):
    clock, events = rig
    loop = loops(clock, events, Settings())
    stop = threading.Event()
    calls = []

    def fake_sleep(_seconds):
        calls.append(1)
        if len(calls) >= 3:
            stop.set()

    loop.run_forever(stop, sleep=fake_sleep)
    assert len(calls) == 3


def test_stale_uploads_are_requeued_periodically(rig):
    """Correction 2: requeue_stale_uploading must run on the reconcile cadence too,
    not only once at startup in build_services -- otherwise a crash mid-upload
    strands assets in UPLOADING until someone notices and restarts the container."""
    clock, events = rig
    assets = StubAssets()
    loop = loops(clock, events, Settings(reconcile_interval=timedelta(minutes=15)), assets=assets)
    loop.iterate()
    assert "requeue_stale_uploading" in assets.calls

    assets.calls.clear()
    clock.advance(timedelta(minutes=5))
    loop.iterate()
    assert "requeue_stale_uploading" not in assets.calls  # only on the reconcile cadence

    clock.advance(timedelta(minutes=11))
    loop.iterate()
    assert "requeue_stale_uploading" in assets.calls


def test_run_forever_survives_a_bad_iteration(rig, caplog):
    """Correction 3: one exception from iterate() must not kill the background
    thread -- otherwise backups silently stop while the rest of the service
    looks healthy. The failure must be logged (through the redacting logger)
    so it is observable, without a supervisor being built."""
    clock, events = rig
    loop = loops(clock, events, Settings())

    attempts = {"n": 0}

    def bad_iterate():
        attempts["n"] += 1
        raise RuntimeError("boom")

    loop.iterate = bad_iterate

    calls = []
    stop = threading.Event()

    def fake_sleep(_seconds):
        calls.append(1)
        if len(calls) >= 3:
            stop.set()

    with caplog.at_level("ERROR"):
        loop.run_forever(stop, sleep=fake_sleep)

    assert attempts["n"] == 3
    assert any(record.exc_info and "boom" in str(record.exc_info[1]) for record in caplog.records)
