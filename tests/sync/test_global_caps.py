"""Task 6: one bandwidth bucket and one upload gate shared by the whole
process, not one per account.

Before this task, every account's `build_runtime_graph` constructed its own
`TokenBucket`, so N accounts multiplied the configured bandwidth cap by N,
and each account's worker pool was sized independently, so `worker_threads`
became `worker_threads x N`. `Worker` now accepts a `gate` (any context
manager -- production passes a `threading.Semaphore`) that wraps the upload
call alone, and `build_runtime_graph` stops constructing its own
`TokenBucket`, taking one from its caller instead.
"""

import threading
from dataclasses import replace

from immich_gphotos.clock import FakeClock
from immich_gphotos.composition import build_runtime_graph
from immich_gphotos.config import Filters, RetryPolicy, Settings
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, Priority
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.throttle import TokenBucket
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


def claim(assets: AssetRepo, asset: Asset = ASSET):
    assets.upsert_pending(asset, Priority.WEBHOOK)
    return assets.claim_next(limit=1)[0]


def make_worker(tmp_path, name: str, *, gate, bandwidth=None, contents=b"ABC"):
    """One account's worker, built the same way tests/sync/test_worker.py and
    tests/sync/test_throttle.py already do (FakeClock, a fake gphotos client,
    a real ByteResolver over a fake Immich client) -- copied rather than
    inventing a new fixture shape, per the task brief."""
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / f"{name}.db"), clock)
    immich = FakeImmichClient(contents={"a1": contents})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / f"{name}-scratch")
    worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=bandwidth,
        gate=gate,
    )
    return worker, assets, clock


class ObservableGate:
    """A real mutual-exclusion gate (backed by `threading.Semaphore`) that
    also lets a test observe, deterministically, the instant a caller
    *starts* trying to acquire it.

    This exists so `test_the_upload_gate_bounds_concurrent_uploads` never has
    to guess how long to wait for the second worker's thread to "probably"
    have reached `gate.acquire()` -- a fixed timeout race that a slow CI box
    (or a correct-looking but actually gate-less implementation, which reaches
    the same point faster with nothing to block on) can pass for the wrong
    reason. Once `acquire_attempted` fires for a caller, that caller's thread
    is inside the real `Semaphore.acquire()` call; if the one slot is already
    held by someone else, a real `Semaphore.acquire()` *cannot* return until
    that holder releases -- so observing the event is equivalent to knowing
    the second caller is genuinely blocked, not "maybe about to be scheduled
    soon."""

    def __init__(self, value: int = 1) -> None:
        self._sem = threading.Semaphore(value)
        self.acquire_attempted = threading.Event()

    def __enter__(self) -> None:
        self.acquire_attempted.set()
        self._sem.acquire()

    def __exit__(self, *exc_info: object) -> bool:
        self._sem.release()
        return False


def test_the_upload_gate_bounds_concurrent_uploads(tmp_path):
    """Two workers sharing a 1-slot gate: the second cannot be inside
    upload() while the first is.

    Deterministic, not timing-based, on both ends:

    - The first worker's `upload()` is made to block (via an Event it waits
      on, not a sleep) until the test releases it, so the test can wait for
      "the first is genuinely inside upload(), holding the gate's only slot"
      before doing anything else.
    - The second worker's own attempt to acquire that same, already-held
      slot is observed via `ObservableGate.acquire_attempted` rather than
      inferred from a fixed pause. The moment that event fires, the second
      worker's thread is inside a real `Semaphore.acquire()` call with zero
      permits available, which cannot return until the first releases -- so
      checking, right then, that the second worker has not yet reached
      `upload()` is a logical guarantee, not a race. This is also what makes
      the test discriminate a *missing* gate: with `gate=None` (see the
      mutation test in the task report), `acquire_attempted` is never set at
      all, since `Worker.process` never touches `self._gate` in that branch,
      and the test fails on that wait instead of passing vacuously."""
    gate = ObservableGate(1)
    entered_upload_1 = threading.Event()
    entered_upload_2 = threading.Event()
    release_upload_1 = threading.Event()

    def make_gphotos(entered: threading.Event, release: threading.Event | None):
        class SlowGphotos(FakeGooglePhotosClient):
            def upload(self, path, *, checksum, filename):
                entered.set()
                if release is not None:
                    assert release.wait(timeout=5), "test never released the first upload"
                return super().upload(path, checksum=checksum, filename=filename)

        return SlowGphotos()

    def build(name: str, asset_id: str, entered: threading.Event, release: threading.Event | None):
        clock = FakeClock()
        assets = AssetRepo(connect(tmp_path / f"{name}.db"), clock)
        immich = FakeImmichClient(contents={asset_id: b"ABC"})
        gphotos = make_gphotos(entered, release)
        resolver = ByteResolver(immich, scratch=tmp_path / f"{name}-scratch")
        worker = Worker(assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, gate=gate)
        asset = replace(ASSET, immich_id=asset_id, checksum=f"sum-{asset_id}")
        return worker, claim(assets, asset)

    worker1, stored1 = build("acct1", "a1", entered_upload_1, release_upload_1)
    worker2, stored2 = build("acct2", "a2", entered_upload_2, None)

    results: dict[str, object] = {}

    def run(name, worker, stored):
        results[name] = worker.process(stored)

    t1 = threading.Thread(target=run, args=("t1", worker1, stored1))
    t1.start()
    assert entered_upload_1.wait(timeout=5), "first upload never started"
    # t1 is now inside upload(), holding the gate's only slot, blocked on
    # release_upload_1. Its own (already-satisfied) acquire also set
    # acquire_attempted; clear it so the next time it fires, it can only be
    # t2's.
    gate.acquire_attempted.clear()

    t2 = threading.Thread(target=run, args=("t2", worker2, stored2))
    t2.start()
    assert gate.acquire_attempted.wait(timeout=5), "second worker never attempted to acquire the gate"
    # With the slot already held by t1 and capacity 1, t2's acquire() cannot
    # have returned yet -- it is provably blocked, so it cannot be inside
    # upload() yet either. No sleep, no "give it a moment": this is true the
    # instant acquire_attempted fires, by construction of a real Semaphore.
    assert not entered_upload_2.is_set()

    release_upload_1.set()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert results["t1"].state.name == "SYNCED"
    assert results["t2"].state.name == "SYNCED"


def test_a_bandwidth_deferral_does_not_hold_the_gate(tmp_path):
    """A metered wait must not hold the gate while it is being served -- a
    second account's upload proceeds immediately rather than blocking behind
    it.

    This is the test that pins the gate to the upload call alone.
    `ByteResolver.resolve` runs *before* `_throttle_upload` (metering needs
    the resolved file's size), so if the gate's acquire moved up to wrap that
    wider resolve/throttle/upload region, an account whose upload is
    mid-wait -- inline-sleeping out a wait below
    `MAX_INLINE_THROTTLE_WAIT_SECONDS`, exactly like this test's capped
    worker -- would hold the gate's only slot for the whole sleep, and a
    second, otherwise-unrelated account's own upload would queue up behind a
    slot that was never needed for a transfer that has not even started
    resolving bytes yet. That is precisely the cross-account stalling
    per-account worker threads exist to remove.

    The wait here is driven by a fake `sleep` synchronized on `threading`
    primitives rather than a real clock sleep, so the test is deterministic:
    it blocks the capped worker's thread *inside* what would be the throttle
    sleep, deterministically proves the second account's upload completes
    while that is still true, and only then lets the first one finish.
    Confirmed by actually moving the gate's acquire up in `Worker.process`
    and re-running this test: it deadlocks (the second thread never returns
    within its join timeout) -- see the task report for that evidence."""
    gate = threading.Semaphore(1)
    sleeping = threading.Event()
    release_sleep = threading.Event()

    def fake_sleep(_seconds: float) -> None:
        sleeping.set()
        assert release_sleep.wait(timeout=5), "test never released the simulated throttle wait"

    clock = FakeClock()
    # 100 bytes/sec bucket starts full (100 tokens); 600 bytes costs
    # (600 - 100) / 100 == 5.0s -- comfortably inline (well under
    # MAX_INLINE_THROTTLE_WAIT_SECONDS), so `_throttle_upload` calls
    # `self._sleep(5.0)` rather than deferring.
    content = b"x" * 600
    assets = AssetRepo(connect(tmp_path / "capped.db"), clock)
    immich = FakeImmichClient(contents={"a1": content})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "capped-scratch")
    bucket = TokenBucket(rate_bytes_per_second=100, clock=clock)
    capped_worker = Worker(
        assets,
        gphotos,
        resolver,
        Filters(),
        RetryPolicy(jitter=0.0),
        clock,
        bandwidth=bucket,
        gate=gate,
        sleep=fake_sleep,
    )

    capped_result: dict[str, object] = {}

    def run_capped():
        capped_result["r"] = capped_worker.process(claim(assets, replace(ASSET, size_bytes=len(content))))

    t1 = threading.Thread(target=run_capped)
    t1.start()
    assert sleeping.wait(timeout=5), "the metered upload never reached its throttle wait"

    # While the first account's upload is mid-wait, a second, uncapped
    # account's own upload must complete on its own -- proven by actually
    # running it on its own thread and requiring it to finish inside a short
    # join timeout, rather than asserting on timing.
    other_worker, other_assets, _ = make_worker(tmp_path, "other", gate=gate)
    other_result: dict[str, object] = {}

    def run_other():
        other_result["r"] = other_worker.process(claim(other_assets))

    t2 = threading.Thread(target=run_other)
    t2.start()
    t2.join(timeout=2)

    assert not t2.is_alive(), "second account's upload did not proceed -- the gate is held across the wait"
    assert other_result["r"].state.name == "SYNCED"

    release_sleep.set()
    t1.join(timeout=5)
    assert capped_result["r"].state.name == "SYNCED"


def test_one_bucket_is_shared_so_n_accounts_do_not_multiply_the_cap(tmp_path):
    """Two graphs built through `build_runtime_graph` with the same
    TokenBucket instance hand that same bucket to both Workers -- N accounts
    do not each get their own full-rate allowance."""
    clock = FakeClock()
    bucket = TokenBucket(rate_bytes_per_second=100, clock=clock)

    def build_graph(name: str):
        conn = connect(tmp_path / f"{name}.db")
        assets = AssetRepo(conn, clock)
        albums = AlbumRepo(conn)
        cursors = CursorRepo(conn)
        events = EventRepo(conn, clock)
        immich = FakeImmichClient()
        gphotos = FakeGooglePhotosClient()
        runtime, _backfill, _loops = build_runtime_graph(
            immich=immich,
            gphotos=gphotos,
            settings=Settings(),
            assets=assets,
            albums=albums,
            cursors=cursors,
            events=events,
            clock=clock,
            scratch=tmp_path / f"{name}-scratch",
            allow_direct=True,
            bandwidth=bucket,
        )
        return runtime

    runtime_a = build_graph("acct-a")
    runtime_b = build_graph("acct-b")

    assert runtime_a._worker._bandwidth is bucket
    assert runtime_b._worker._bandwidth is bucket
    assert runtime_a._worker._bandwidth is runtime_b._worker._bandwidth


def test_build_runtime_graph_no_longer_constructs_its_own_bucket(tmp_path):
    """`settings.bandwidth_bytes_per_second` alone must not be enough to get a
    bucket -- build_runtime_graph must take one from its caller (or none at
    all) rather than building its own from the setting, or a
    caller-supplied, genuinely shared bucket could never actually be shared:
    every call would silently get a fresh private one instead."""
    conn = connect(tmp_path / "t.db")
    clock = FakeClock()
    runtime, _backfill, _loops = build_runtime_graph(
        immich=FakeImmichClient(),
        gphotos=FakeGooglePhotosClient(),
        settings=Settings(bandwidth_bytes_per_second=1_000_000),
        assets=AssetRepo(conn, clock),
        albums=AlbumRepo(conn),
        cursors=CursorRepo(conn),
        events=EventRepo(conn, clock),
        clock=clock,
        scratch=tmp_path / "scratch",
        allow_direct=True,
        # bandwidth deliberately omitted
    )
    assert runtime._worker._bandwidth is None
