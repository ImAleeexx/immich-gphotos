"""Tests for the live-swap mechanism: rebuild_runtime + LoopsHandle.

These exercise the swap directly against Fakes -- no real network, no real
credentials -- proving the mechanism itself, independent of any particular
route that triggers it.
"""

from immich_gphotos.clock import FakeClock
from immich_gphotos.composition import build_runtime_graph, rebuild_runtime
from immich_gphotos.config import Settings
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.services import Services
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo
from immich_gphotos.sync.loops import LoopsHandle


def build(tmp_path, settings=None):
    conn = connect(tmp_path / "t.db")
    clock = FakeClock()
    assets = AssetRepo(conn, clock)
    albums = AlbumRepo(conn)
    cursors = CursorRepo(conn)
    events = EventRepo(conn, clock)
    settings = settings or Settings()
    immich = FakeImmichClient()
    gphotos = FakeGooglePhotosClient()
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
