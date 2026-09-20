"""Tests for the live-swap mechanism: rebuild_runtime + LoopsHandle.

These exercise the swap directly against Fakes -- no real network, no real
credentials -- proving the mechanism itself, independent of any particular
route that triggers it.
"""

import threading
from dataclasses import replace
from datetime import timedelta

from immich_gphotos.clock import FakeClock
from immich_gphotos.composition import build_runtime_graph, rebuild_runtime
from immich_gphotos.config import Settings
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, ErrorClass, Priority
from immich_gphotos.services import Services
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo
from immich_gphotos.sync.loops import LoopsHandle
from immich_gphotos.sync.throttle import TokenBucket
from immich_gphotos.sync.worker import HALT_RETRY_DELAY


def build(tmp_path, settings=None, immich=None):
    conn = connect(tmp_path / "t.db")
    clock = FakeClock()
    assets = AssetRepo(conn, clock)
    albums = AlbumRepo(conn)
    cursors = CursorRepo(conn)
    events = EventRepo(conn, clock)
    settings = settings or Settings()
    immich = immich if immich is not None else FakeImmichClient()
    gphotos = FakeGooglePhotosClient()
    # build_runtime_graph no longer constructs its own TokenBucket from
    # settings.bandwidth_bytes_per_second (Task 6: that construction moved to
    # AccountRegistry, which shares one bucket across every account instead
    # of each graph getting a private one) -- so this single-account test
    # helper does what AccountRegistry now does, for the one graph it builds.
    bandwidth = (
        TokenBucket(settings.bandwidth_bytes_per_second, clock)
        if settings.bandwidth_bytes_per_second is not None
        else None
    )
    runtime, backfill, loops = build_runtime_graph(
        immich=immich,
        gphotos=gphotos,
        settings=settings,
        assets=assets,
        albums=albums,
        cursors=cursors,
        events=events,
        clock=clock,
        scratch=tmp_path / "scratch",
        allow_direct=True,
        bandwidth=bandwidth,
    )
    services = Services(
        assets=assets,
        albums=albums,
        cursors=cursors,
        settings_repo=SettingRepo(conn),
        events=events,
        runtime=runtime,
        settings=settings,
        webhook_secret="s3cret",
        backfill=backfill,
        immich=immich,
        gphotos=gphotos,
        clock=clock,
        bandwidth=bandwidth,
        scratch=tmp_path / "scratch",
        allow_direct=True,
        loops_handle=LoopsHandle(loops),
    )
    return services


def test_rebuild_swaps_the_immich_client_the_running_service_uses(tmp_path):
    services = build(tmp_path)
    original_runtime = services.runtime
    original_loops = services.loops_handle.current

    new_immich = FakeImmichClient(version=(3, 2, 2))
    rebuild_runtime(services, immich=new_immich)

    assert services.immich is new_immich
    # The rebuilt loop graph's components were constructed against the new
    # client, not the old one -- this is the assertion that matters: the
    # swap actually reached the background loop, not just the Services field.
    assert services.loops_handle.current is not original_loops
    assert services.loops_handle.current._reconciler._immich is new_immich
    assert services.loops_handle.current._backfill._immich is new_immich
    assert services.runtime is not original_runtime


def test_rebuild_swaps_the_gphotos_client(tmp_path):
    services = build(tmp_path)
    new_gphotos = FakeGooglePhotosClient()

    rebuild_runtime(services, gphotos=new_gphotos)

    assert services.gphotos is new_gphotos
    assert services.loops_handle.current._album_mirror._gphotos is new_gphotos
    assert services.loops_handle.current._deletion_sweeper._gphotos is new_gphotos


def test_rebuild_with_no_overrides_keeps_the_existing_clients_and_settings(tmp_path):
    services = build(tmp_path)
    immich, gphotos, settings = services.immich, services.gphotos, services.settings

    rebuild_runtime(services)

    assert services.immich is immich
    assert services.gphotos is gphotos
    assert services.settings is settings


def test_turning_deletions_off_takes_effect_on_the_next_loop_iteration_without_a_restart(tmp_path):
    """The scenario the spec calls out by name: a user flips deletions_enabled
    to False and the running loop must stop trashing Google items on its next
    pass, not only after a manual restart."""
    services = build(tmp_path, settings=Settings(deletions_enabled=True))
    assert services.loops_handle.current._deletion_sweeper._settings.deletions_enabled is True

    rebuild_runtime(services, settings=Settings(deletions_enabled=False))

    assert services.settings.deletions_enabled is False
    assert services.loops_handle.current._deletion_sweeper._settings.deletions_enabled is False


def test_rebuild_without_a_loops_handle_still_updates_services(tmp_path):
    """rebuild_runtime must not assume a loops_handle is wired (e.g. a test
    Services built without one) -- it should update the Services fields and
    simply skip the loop swap."""
    services = build(tmp_path)
    services.loops_handle = None

    rebuild_runtime(services, settings=Settings(quality="saver"))

    assert services.settings.quality == "saver"


def test_rebuild_syncs_a_changed_quality_onto_the_carried_forward_gphotos_client(tmp_path):
    """C1: rebuild_runtime carries the *existing* gphotos client forward
    unchanged when only settings changed. Anything exposing a settable
    `quality` (GpmcClient in production) must be synced to the new settings'
    quality, or a quality change has no effect on the live uploader."""

    class _QualityAwareFake(FakeGooglePhotosClient):
        def __init__(self) -> None:
            super().__init__()
            self.quality = "original"

    services = build(tmp_path)
    services.gphotos = _QualityAwareFake()

    rebuild_runtime(services, settings=Settings(quality="quota"))

    assert services.gphotos.quality == "quota"


def test_rebuild_preserves_an_active_halt_and_its_retry_cooldown(tmp_path):
    """I6: a fresh Runtime starts with _paused_reason=None and a fresh
    BackgroundLoops starts with _paused_at=None, so a settings save used to
    silently clear an active AUTH_INVALID/QUOTA_EXHAUSTED halt -- dropping
    the dashboard's banner and letting the loop resume hammering still-bad
    credentials. A rebuild must carry the halt (and the pause timestamp that
    drives PAUSE_RETRY_AFTER) across the swap."""
    services = build(tmp_path)
    services.runtime.pause("AUTH_INVALID")
    services.loops_handle.current.iterate()  # sets _paused_at, as a real loop pass would
    paused_at = services.loops_handle.current._paused_at
    next_reconcile = services.loops_handle.current._next_reconcile
    assert paused_at is not None

    rebuild_runtime(services, settings=Settings(worker_threads=3))

    assert services.runtime.paused_reason == "AUTH_INVALID"
    assert services.loops_handle.current._paused_at == paused_at
    # Not strictly required by the spec, but noted as clean to also carry:
    # a rebuild should not force an unrelated full reconcile as a side effect.
    assert services.loops_handle.current._next_reconcile == next_reconcile


def test_rebuild_does_not_carry_a_pause_forward_when_there_was_none(tmp_path):
    services = build(tmp_path)
    assert services.runtime.paused_reason is None

    rebuild_runtime(services, settings=Settings(worker_threads=3))

    assert services.runtime.paused_reason is None
    assert services.loops_handle.current._paused_at is None


def test_rebuild_closes_the_outgoing_immich_client_when_it_is_replaced(tmp_path):
    """The small finding this guards: `HttpImmichClient.close()` existed but
    nothing ever called it on a rebuild, leaking one httpx connection pool's
    sockets on every wizard reconnect. With no worker pool ever created
    (`worker_threads` defaults to 1 in this fixture's Settings()), there is no
    background pool to drain first, so the close happens synchronously."""

    class _ClosableFake(FakeImmichClient):
        def __init__(self, *a, **k) -> None:
            super().__init__(*a, **k)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    services = build(tmp_path)
    old_immich = _ClosableFake()
    services.immich = old_immich
    services.loops_handle.current._reconciler._immich = old_immich

    new_immich = _ClosableFake(version=(3, 2, 2))
    rebuild_runtime(services, immich=new_immich)

    assert old_immich.closed is True
    assert new_immich.closed is False


def test_rebuild_does_not_close_the_immich_client_it_is_carrying_forward(tmp_path):
    """The common case -- only settings changed -- must never close the
    client still installed and in active use on the running service."""

    class _ClosableFake(FakeImmichClient):
        def __init__(self, *a, **k) -> None:
            super().__init__(*a, **k)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    services = build(tmp_path)
    carried = _ClosableFake()
    services.immich = carried
    services.loops_handle.current._reconciler._immich = carried

    rebuild_runtime(services, settings=Settings(quality="saver"))

    assert carried.closed is False
    assert services.immich is carried


def test_rebuild_closes_the_outgoing_immich_client_after_its_pool_drains(tmp_path):
    """When the outgoing Runtime did create a worker pool (worker_threads >
    1), the Immich client close is deferred to a background thread that
    waits for that pool to finish draining first, rather than closing out
    from under whatever the pool's threads might still be doing."""
    import time

    class _ClosableFake(FakeImmichClient):
        def __init__(self, *a, **k) -> None:
            super().__init__(*a, **k)
            self.closed = False

        def close(self) -> None:
            self.closed = True

    services = build(tmp_path, settings=Settings(worker_threads=2))
    old_immich = _ClosableFake()
    services.immich = old_immich
    services.loops_handle.current._reconciler._immich = old_immich
    services.runtime._executor()  # force the pool into existence, as a real tick would

    new_immich = _ClosableFake(version=(3, 2, 2))
    rebuild_runtime(services, immich=new_immich)

    deadline = time.monotonic() + 2.0
    while not old_immich.closed and time.monotonic() < deadline:
        time.sleep(0.01)
    assert old_immich.closed is True


def test_rebuild_closes_the_outgoing_runtimes_worker_pool(tmp_path):
    """A Runtime with worker_threads > 1 may own a lazily-created thread
    pool. Swapping in a new Runtime on a settings change must not leak that
    pool's threads -- rebuild_runtime closes the outgoing one."""
    services = build(tmp_path, settings=Settings(worker_threads=2))
    old_runtime = services.runtime
    old_runtime._executor()  # force the pool into existence, as a real tick would
    assert old_runtime._pool is not None

    closed = []
    original_close = old_runtime.close

    def spy_close():
        closed.append(True)
        original_close()

    old_runtime.close = spy_close

    rebuild_runtime(services, settings=Settings(worker_threads=3))

    assert closed == [True]
    assert services.runtime is not old_runtime


# --- I1: a bandwidth-cap change releases the work deferred under the old cap.

THROTTLED_CONTENT = b"x" * 100


def throttled_asset(asset_id: str) -> Asset:
    return Asset(
        immich_id=asset_id,
        checksum=f"sum-{asset_id}",
        filename=f"{asset_id}.jpg",
        type="IMAGE",
        size_bytes=len(THROTTLED_CONTENT),
        immich_updated_at="2026-09-17T10:00:00Z",
        original_path=None,
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
    )


def with_assets_deferred_under_a_cap(tmp_path, ids=("a1", "a2")):
    """A running service whose queue holds `ids`, each parked on a deadline the
    *current* cap produced -- via the real Worker the built graph wired up, not
    a hand-written row. 100 bytes at 1 byte/second is a 99-second wait, well
    over MAX_INLINE_THROTTLE_WAIT_SECONDS, so each one defers rather than
    sleeping on the calling thread."""
    immich = FakeImmichClient(contents=dict.fromkeys(ids, THROTTLED_CONTENT))
    services = build(tmp_path, settings=Settings(bandwidth_bytes_per_second=1), immich=immich)
    for asset_id in ids:
        services.assets.upsert_pending(throttled_asset(asset_id), Priority.WEBHOOK)
    for stored in services.assets.claim_next(limit=len(ids)):
        assert services.runtime._worker.process(stored).deferred is True
    assert services.assets.claim_next(limit=10) == []  # all parked on future deadlines
    return services


def test_clearing_the_bandwidth_cap_makes_assets_deferred_under_it_claimable_again(tmp_path):
    """I1. A deferred upload's deadline is arithmetic against the cap that was
    in force when it was metered, and nothing else ever revisits it -- not the
    rebuild, not upsert_pending, not requeue_stale_uploading, not a restart
    (the deadline is persisted; the debt justifying it is in memory). So
    clearing the cap used to leave the whole deferred backlog serving out the
    old cap's sentence, invisibly and with no way to undo it from the UI."""
    services = with_assets_deferred_under_a_cap(tmp_path)

    rebuild_runtime(services, settings=replace(services.settings, bandwidth_bytes_per_second=None))

    assert [s.asset.immich_id for s in services.assets.claim_next(limit=10)] == ["a1", "a2"]


def test_changing_the_bandwidth_cap_to_another_value_also_releases_assets_deferred_under_the_old_one(
    tmp_path,
):
    """Not only clearing it: a cap that is raised (or lowered) is a different
    cap, so the old cap's arithmetic is equally stale."""
    services = with_assets_deferred_under_a_cap(tmp_path)

    rebuild_runtime(services, settings=replace(services.settings, bandwidth_bytes_per_second=500_000))

    assert [s.asset.immich_id for s in services.assets.claim_next(limit=10)] == ["a1", "a2"]


def test_a_rebuild_that_does_not_touch_the_cap_leaves_deferred_assets_on_their_deadlines(tmp_path):
    """The release is keyed to the cap actually changing. A settings save that
    only changes the quality (or a wizard step that swaps a client) must not
    dump the whole deferred backlog back into the queue at once -- that would
    undo the throttle the user still has configured."""
    services = with_assets_deferred_under_a_cap(tmp_path)
    deadlines = {i: services.assets.get(i).next_attempt_at for i in ("a1", "a2")}

    rebuild_runtime(services, settings=replace(services.settings, quality="saver"))

    assert {i: services.assets.get(i).next_attempt_at for i in deadlines} == deadlines
    assert services.assets.claim_next(limit=10) == []


def test_a_cap_change_leaves_failure_backoff_halt_retry_and_window_deferrals_on_their_deadlines(tmp_path):
    """I1's hard constraint, end to end. `next_attempt_at` is shared by four
    defer reasons; only the bandwidth one may be released. The window defer is
    driven through the real Worker (`transfer_allowed=False`); the halt retry
    and the failure backoff are written with the exact repo calls
    `Worker._handle_failure` makes for them."""
    services = with_assets_deferred_under_a_cap(tmp_path, ids=("capped",))
    assets, clock = services.assets, services.clock
    for asset_id in ("backoff", "halted", "windowed"):
        assets.upsert_pending(throttled_asset(asset_id), Priority.WEBHOOK)
    claimed = {s.asset.immich_id: s for s in assets.claim_next(limit=3)}
    assert services.runtime._worker.process(claimed["windowed"], transfer_allowed=False).deferred is True
    assets.requeue("halted", clock.now() + HALT_RETRY_DELAY)
    assets.mark_retry("backoff", ErrorClass.TRANSIENT, "boom", clock.now() + timedelta(minutes=5))
    untouched = {i: assets.get(i).next_attempt_at for i in ("backoff", "halted", "windowed")}

    rebuild_runtime(services, settings=replace(services.settings, bandwidth_bytes_per_second=None))

    assert assets.get("capped").next_attempt_at is None
    assert {i: assets.get(i).next_attempt_at for i in untouched} == untouched
    assert assets.get("backoff").attempts == 1  # the backoff's own bookkeeping is intact
    assert [s.asset.immich_id for s in assets.claim_next(limit=10)] == ["capped"]


# --- RULING R6: rebuild_runtime must forward the shared bandwidth bucket and
# upload gate already installed on `services`, or the very first settings
# save after boot silently hands every account back its own private,
# uncapped upload path -- invisible until someone measures their uplink.


def test_a_settings_save_does_not_drop_the_shared_bandwidth_bucket_and_gate(tmp_path):
    """R6. `Services.bandwidth`/`Services.gate` stand in here for what
    `AccountRegistry` actually installs -- one `TokenBucket` and one
    `threading.Semaphore` shared by every account. A settings save that has
    nothing to do with either (here, `quality`) must still carry them across
    the swap unchanged: `build_runtime_graph` no longer builds its own
    bucket, so if `rebuild_runtime` failed to forward these, the rebuilt
    Worker would silently end up with `bandwidth=None, gate=None` --
    uncapped and ungated -- with nothing in the API or the UI to show it."""
    services = build(tmp_path, settings=Settings(bandwidth_bytes_per_second=1_000_000))
    shared_bandwidth = services.bandwidth
    assert shared_bandwidth is not None  # build() only sets this when the setting is set
    assert services.runtime._worker._bandwidth is shared_bandwidth
    # build()'s own graph never wires a gate in (it predates Task 6 and has
    # no reason to grow one just for this test); set one directly on
    # `services` here to stand in for what `AccountRegistry` would have
    # installed there in production, and prove the *rebuild* forwards it.
    shared_gate = threading.Semaphore(3)
    services.gate = shared_gate

    rebuild_runtime(services, settings=replace(services.settings, quality="saver"))

    assert services.bandwidth is shared_bandwidth
    assert services.gate is shared_gate
    assert services.runtime._worker._bandwidth is shared_bandwidth
    assert services.runtime._worker._gate is shared_gate
