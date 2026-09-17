"""Builds the credential/settings-dependent half of the runtime graph, and
rebuilds it in place on the already-running service.

`Runtime`, `BackfillJob`, `Reconciler`, `AlbumMirror`, `DeletionSweeper` and
the `Worker`/`ByteResolver` behind it all close over the Immich client, the
Google Photos client and/or `Settings` at construction time. None of them
poll for change. So a configuration change -- the wizard persisting real
credentials in place of the boot-time fakes, or a settings PUT flipping
`deletions_enabled` -- has nowhere to take effect unless something rebuilds
that half of the graph and swaps it onto the running service.

That is what `rebuild_runtime` does. It is the live-swap mechanism described
in the setup-wizard design: `Services` is deliberately not frozen, so its
`runtime`/`backfill`/`immich`/`gphotos`/`settings` fields can be reassigned
in place, and `LoopsHandle.replace` swaps the background thread onto a fresh
`BackgroundLoops` without restarting that thread. Nothing in the database
changes here -- cursors and queued assets are untouched -- only the
in-memory object graph.
"""

from pathlib import Path

from immich_gphotos.clock import Clock
from immich_gphotos.config import Settings
from immich_gphotos.gphotos.protocol import GooglePhotosClient
from immich_gphotos.immich.protocol import ImmichClient
from immich_gphotos.services import Services
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo
from immich_gphotos.sync.albums import AlbumMirror
from immich_gphotos.sync.backfill import BackfillJob
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.deletions import DeletionSweeper
from immich_gphotos.sync.loops import BackgroundLoops
from immich_gphotos.sync.reconciler import Reconciler
from immich_gphotos.sync.runtime import Runtime
from immich_gphotos.sync.throttle import TokenBucket
from immich_gphotos.sync.worker import Worker


def build_runtime_graph(
    *,
    immich: ImmichClient,
    gphotos: GooglePhotosClient,
    settings: Settings,
    assets: AssetRepo,
    albums: AlbumRepo,
    cursors: CursorRepo,
    events: EventRepo,
    clock: Clock,
    scratch: Path,
    allow_direct: bool,
) -> tuple[Runtime, BackfillJob, BackgroundLoops]:
    """Construct one fresh copy of everything that closes over clients/settings."""
    resolver = ByteResolver(immich, scratch=scratch, allow_direct=allow_direct)
    # One bucket for the whole graph, shared by every worker thread that
    # calls this Worker's process() -- a bucket per thread would let the pool
    # size multiply the configured cap. None (the default, unset) means no
    # cap and is never constructed, so it adds no overhead.
    bandwidth = (
        TokenBucket(settings.bandwidth_bytes_per_second, clock)
        if settings.bandwidth_bytes_per_second is not None
        else None
    )
    worker = Worker(assets, gphotos, resolver, settings.filters, settings.retry, clock, bandwidth=bandwidth)
    runtime = Runtime(assets, worker, settings, clock, events, immich=immich)
    backfill = BackfillJob(immich, assets, cursors, settings)
    loops = BackgroundLoops(
        runtime=runtime,
        reconciler=Reconciler(immich, assets, cursors, settings, clock),
        backfill=backfill,
        album_mirror=AlbumMirror(immich, gphotos, albums, assets),
        deletion_sweeper=DeletionSweeper(gphotos, assets, settings),
        settings=settings,
        clock=clock,
        events=events,
        assets=assets,
    )
    return runtime, backfill, loops


def rebuild_runtime(
    services: Services,
    *,
    immich: ImmichClient | None = None,
    gphotos: GooglePhotosClient | None = None,
    settings: Settings | None = None,
) -> None:
    """Rebuild the runtime graph and swap it onto `services` in place.

    Any of `immich`/`gphotos`/`settings` left as `None` keeps whatever is
    already installed on `services` -- callers pass only what actually
    changed (e.g. the settings route passes only `settings`; the wizard's
    Immich step passes only `immich` and the updated `settings.immich_url`).
    """
    immich = immich if immich is not None else services.immich
    gphotos = gphotos if gphotos is not None else services.gphotos
    settings = settings if settings is not None else services.settings

    runtime, backfill, loops = build_runtime_graph(
        immich=immich,
        gphotos=gphotos,
        settings=settings,
        assets=services.assets,
        albums=services.albums,
        cursors=services.cursors,
        events=services.events,
        clock=services.clock,
        scratch=services.scratch or Path("scratch"),
        allow_direct=services.allow_direct,
    )

    services.immich = immich
    services.gphotos = gphotos
    services.settings = settings
    services.runtime = runtime
    services.backfill = backfill
    if services.loops_handle is not None:
        services.loops_handle.replace(loops)
