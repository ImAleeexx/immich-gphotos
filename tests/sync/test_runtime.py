from datetime import UTC, datetime, time, timedelta

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
from immich_gphotos.sync.throttle import TokenBucket
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
    """A single claimed asset would make `len(wave) <= 1` short-circuit to
    the sequential path regardless of worker_threads, passing even at
    worker_threads=16 -- enqueue more than one so this actually exercises
    worker_threads=1 collapsing every wave to size 1."""
    assets, events, gphotos, worker, clock, immich = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    assets.upsert_pending(asset("b"), Priority.WEBHOOK)
    runtime = Runtime(assets, worker, Settings(worker_threads=1), clock, events, immich=immich)
    result = runtime.tick(limit=5)
    assert result.processed == 2
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
        # FakeClock never advances on its own, so without this every row
        # gets the same first_seen_at and "ORDER BY priority, first_seen_at"
        # ties -- the wave [a, b] this test depends on would then rest on
        # SQLite's unspecified tie-break instead of being deterministic.
        clock.advance(timedelta(seconds=1))

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


def test_close_mid_tick_falls_back_to_sequential_processing_instead_of_raising(rig):
    """I1: composition.rebuild_runtime calls close() on the outgoing Runtime
    from the settings-save request thread, with no coordination against an
    in-flight tick() on the background-loop thread. A tick already past its
    first wave when close() lands must not let executor.submit's
    RuntimeError ("cannot schedule new futures after shutdown") escape
    tick() and strand the rest of its claimed batch in UPLOADING until
    requeue_stale_uploading eventually notices -- it must finish the batch
    on the calling thread instead."""
    assets, events, gphotos, worker, clock, immich = rig
    for name in ("a", "b", "c", "d"):
        assets.upsert_pending(asset(name), Priority.WEBHOOK)
        clock.advance(timedelta(seconds=1))

    runtime = Runtime(assets, worker, Settings(worker_threads=2), clock, events, immich=immich)
    real_process = worker.process

    def process_and_race_a_settings_save(stored, **kwargs):
        result = real_process(stored, **kwargs)
        if stored.asset.immich_id == "b":
            # Simulates another thread's rebuild_runtime completing (and
            # calling close() on this now-outgoing Runtime) while this
            # tick's first wave, [a, b], is still running.
            runtime.close()
        return result

    worker.process = process_and_race_a_settings_save

    result = runtime.tick(limit=4)

    assert result.halted is False
    assert result.processed == 4
    for name in ("a", "b", "c", "d"):
        assert assets.get(name).state == AssetState.SYNCED


def test_run_wave_survives_close_racing_the_submit_call_itself(rig):
    """The tiny window between _run_wave's `self._closing.is_set()` check
    and its `executor.submit()` calls -- close() landing on another thread
    in exactly that gap -- is closed by catching the RuntimeError
    ThreadPoolExecutor raises for a submission after shutdown, not by that
    flag check alone. Whatever was already submitted in this wave before the
    race must still be awaited, and only the remainder run sequentially."""
    assets, events, gphotos, worker, clock, immich = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    clock.advance(timedelta(seconds=1))
    assets.upsert_pending(asset("b"), Priority.WEBHOOK)

    runtime = Runtime(assets, worker, Settings(worker_threads=2), clock, events, immich=immich)
    real_executor = runtime._executor()

    class RacingExecutor:
        """Stands in for the real pool: the second submit() call raises,
        exactly as the real pool would if shut down in between."""

        def __init__(self, real):
            self._real = real
            self.calls = 0

        def submit(self, fn, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("cannot schedule new futures after shutdown")
            return self._real.submit(fn, *args, **kwargs)

    runtime._pool = RacingExecutor(real_executor)

    result = runtime.tick(limit=2)

    assert result.halted is False
    assert result.processed == 2
    assert assets.get("a").state == AssetState.SYNCED
    assert assets.get("b").state == AssetState.SYNCED

    real_executor.shutdown(wait=True)


def test_album_allowlist_is_not_resolved_when_nothing_is_claimed(rig, tmp_path):
    """I2: resolving filters.album_allowlist costs one Immich call per
    configured album. Doing that unconditionally, before claim_next, cost
    ~1,800 calls/hour per allowed album at the default IDLE_SLEEP_SECONDS --
    almost all for ticks that claimed nothing. It must only be resolved once
    this tick has actually claimed something."""
    assets, events, gphotos, _, clock, immich = rig
    immich.albums["album-1"] = []
    filters = Filters(album_allowlist=frozenset({"album-1"}))
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    worker = Worker(assets, gphotos, resolver, filters, RetryPolicy(jitter=0.0), clock)
    settings = Settings(filters=filters)
    runtime = Runtime(assets, worker, settings, clock, events, immich=immich)

    result = runtime.tick(limit=5)  # nothing enqueued -- claims nothing

    assert result.processed == 0
    assert immich.album_asset_ids_calls == []


def test_a_failed_allowlist_resolution_requeues_the_claimed_batch(rig, tmp_path):
    """Resolving the allowlist only after claim_next (I2) means a resolution
    failure now happens after claim_next has already flipped those rows to
    UPLOADING. tick() must return them to PENDING rather than let the
    exception strand them there until requeue_stale_uploading eventually
    notices -- and must not mark any row ineligible against a partial or
    absent result (see I3)."""
    assets, events, gphotos, _, clock, immich = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)

    def boom(album_id):
        raise RuntimeError("immich unreachable")

    immich.album_asset_ids = boom

    filters = Filters(album_allowlist=frozenset({"album-1"}))
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    worker = Worker(assets, gphotos, resolver, filters, RetryPolicy(jitter=0.0), clock)
    settings = Settings(filters=filters)
    runtime = Runtime(assets, worker, settings, clock, events, immich=immich)

    with pytest.raises(RuntimeError, match="immich unreachable"):
        runtime.tick(limit=5)

    stranded = assets.get("a")
    assert stranded.state == AssetState.PENDING
    assert stranded.attempts == 0


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
    to member asset ids via ImmichClient for any tick that claims something,
    and the worker admits only members.

    Runs two ticks with membership changing in between -- a single tick can
    never exercise "per tick", and this doubles as the I3 regression guard:
    ALBUM_EXCLUDED_REASON must not be terminal, or an asset added to an
    allowed album after the fact would never be re-admitted.

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
    runtime = Runtime(assets, worker, settings, clock, events, immich=immich)

    first = runtime.tick(limit=5)

    assert first.processed == 2
    admitted = assets.get("in-album")
    assert admitted.state == AssetState.SYNCED

    excluded = assets.get("not-in-album")
    assert excluded.state == AssetState.INELIGIBLE
    assert excluded.ineligible_reason == "album_excluded"

    # Membership changes: "not-in-album" is added to the allowed album. A
    # webhook or reconciler pass touching this asset again -- upsert_pending
    # -- is what reopens a soft (album_excluded) ineligibility back to
    # PENDING; see store.assets.AssetRepo.upsert_pending.
    immich.albums["album-1"].append("not-in-album")
    assert assets.upsert_pending(asset("not-in-album"), Priority.WEBHOOK) is True

    second = runtime.tick(limit=5)

    assert second.processed == 1
    now_admitted = assets.get("not-in-album")
    assert now_admitted.state == AssetState.SYNCED


def test_events_are_recorded_and_bounded(rig):
    assets, events, _, worker, clock, immich = rig
    for i in range(5):
        events.add("info", f"message {i}")
    assert len(events.recent(3)) == 3
    assert events.recent(1)[0]["message"] == "message 4"


def test_a_deferred_backlog_drains_at_the_configured_bandwidth_cap(tmp_path):
    """C1 regression guard over the whole loop. Every asset here is far too
    large for its cap to be slept out inline, so all of them take the defer
    path -- which is the only path that matters in practice, since at the
    minimum configurable cap (MIN_BANDWIDTH_BYTES_PER_SECOND) anything above
    a couple of megabytes defers. Driving `tick` with a FakeClock that jumps
    to each successive deadline, the backlog must take about as long as the
    configured rate says it should: 20 assets x 10,000 bytes at 100 bytes/
    second is ~2,000 seconds of transfer, and no wall-clock time at all,
    since nothing is ever slept out on the loop thread."""
    clock = FakeClock(datetime(2026, 9, 17, 12, 0, tzinfo=UTC))
    conn = connect(tmp_path / "t.db")
    assets = AssetRepo(conn, clock)
    events = EventRepo(conn, clock)
    gphotos = FakeGooglePhotosClient()
    rate = 100
    content = b"x" * 10_000
    ids = [f"a{i:02d}" for i in range(20)]
    immich = FakeImmichClient(contents=dict.fromkeys(ids, content))
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    sleeps: list[float] = []
    worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=TokenBucket(rate_bytes_per_second=rate, clock=clock),
        sleep=sleeps.append,
    )
    runtime = Runtime(assets, worker, Settings(worker_threads=1), clock, events, immich=immich)
    for i in ids:
        assets.upsert_pending(asset(i), Priority.BACKFILL)

    start = clock.now()
    for _ in range(len(ids) * 4):  # generous bound, so a stall fails instead of hanging
        runtime.tick(limit=8)
        if len(gphotos.uploads) == len(ids):
            break
        # Nothing is due right now: jump to whenever the next deferred asset
        # says it may move its bytes. This is the simulated time the cap costs.
        due = conn.execute(
            "SELECT MIN(next_attempt_at) FROM asset WHERE state = ?",
            (AssetState.PENDING.value,),
        ).fetchone()[0]
        assert due is not None, "backlog stalled with nothing pending"
        clock.advance(max(timedelta(0), datetime.fromisoformat(due) - clock.now()))

    elapsed = (clock.now() - start).total_seconds()
    ideal = len(ids) * len(content) / rate  # 2,000 seconds
    assert len(gphotos.uploads) == len(ids)
    assert sleeps == []  # the loop thread was never blocked
    assert ideal * 0.95 <= elapsed <= ideal * 1.05


def test_event_ring_discards_the_oldest(tmp_path):
    clock = FakeClock()
    events = EventRepo(connect(tmp_path / "t.db"), clock, limit=3)
    for i in range(6):
        events.add("info", f"m{i}")
    messages = [e["message"] for e in events.recent(10)]
    assert messages == ["m5", "m4", "m3"]
