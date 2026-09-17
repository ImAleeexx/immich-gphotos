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
`BackgroundLoops` without restarting that thread. The database is left alone
-- cursors and queued assets are untouched -- with one deliberate exception:
a change to the bandwidth cap releases the assets that were deferred against
the *old* cap, whose deadlines nothing else would ever revisit. See the end
of `rebuild_runtime`.
"""

import threading
from pathlib import Path
from typing import Any

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


def _close_outgoing_immich_client(client: Any, pool: Any) -> None:
    """Close an outgoing `HttpImmichClient` once nothing already dispatched
    against it can still be running.

    `HttpImmichClient.close()` tears down its `httpx.Client`'s connection
    pool outright -- unlike `ThreadPoolExecutor.shutdown(wait=False)` (see
    `Runtime.close`), which only refuses *new* submissions and lets whatever
    is already running finish untouched, closing this out from under a
    request that is genuinely in flight would break that request. The
    outgoing Runtime's worker pool (if `worker_threads > 1` ever created one)
    is exactly where such an in-flight request would be: `Runtime.close()`,
    called just above this, asked that pool to stop accepting new
    submissions but deliberately did not wait for what it had already
    accepted -- those threads may still be calling into this same client
    (a download via the resolver, or an album-allowlist lookup) when we get
    here.

    So the close itself happens on a short-lived background thread that
    waits for that pool to fully drain first: `shutdown(wait=True)` after
    `close()`'s own `wait=False` shutdown does not shut down twice, it just
    blocks until the same drain completes. When there never was a pool
    (`worker_threads == 1`, the default -- every asset runs directly on the
    calling thread), the only possible racer is a tick already executing on
    the single long-lived background-loop thread at the exact moment of this
    swap. That window is small, bounded by one tick's own claim size, and any
    failure it causes surfaces as an ordinary classified, retried error --
    the same tolerance this project already extends to the analogous
    `executor.submit()` race in `Runtime._run_wave` -- so the client is
    closed immediately in that case rather than inventing a new lock for it.

    Not closing at all was the actual bug this exists to fix: every settings
    save and every wizard reconnect otherwise leaked one httpx connection
    pool's sockets for the life of the container.
    """
    close = getattr(client, "close", None)
    if close is None:
        return

    def _close() -> None:
        if pool is not None:
            pool.shutdown(wait=True)
        close()

    if pool is not None:
        threading.Thread(target=_close, name="igp-immich-client-close", daemon=True).start()
    else:
        _close()


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
    old_runtime = services.runtime
    old_immich = services.immich
    old_bandwidth = services.settings.bandwidth_bytes_per_second

    # GpmcClient bakes `quality` in at construction and `upload()` reads it
    # from `self`, not from `Settings` -- so carrying the *existing* gphotos
    # client forward (the common case: only settings changed) would silently
    # keep uploading at the old quality forever, no matter what a settings
    # save or the wizard's options step just set. Sync it here, in the one
    # place both paths funnel through, rather than in each caller.
    if hasattr(gphotos, "quality"):
        gphotos.quality = settings.quality

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

    # A fresh Runtime/BackgroundLoops otherwise starts unpaused, so any
    # settings save would silently clear an active AUTH_INVALID/
    # QUOTA_EXHAUSTED halt -- dropping the dashboard's banner and resuming a
    # transfer that still has bad credentials. Carry the halt (and the pause
    # timestamp that drives its retry cooldown) across the swap; a rebuild is
    # not itself a reason to resume. `_next_reconcile` is carried too so a
    # save doesn't also force an immediate full reconcile as a side effect.
    old_loops = services.loops_handle.current if services.loops_handle is not None else None
    paused_reason = getattr(old_runtime, "paused_reason", None)
    if paused_reason is not None:
        runtime._paused_reason = paused_reason
    if old_loops is not None:
        loops._paused_at = old_loops._paused_at
        loops._next_reconcile = old_loops._next_reconcile

    services.immich = immich
    services.gphotos = gphotos
    services.settings = settings
    services.runtime = runtime
    services.backfill = backfill
    if services.loops_handle is not None:
        services.loops_handle.replace(loops)

    # A real Runtime may own a worker-thread pool (see Runtime.close); shut
    # the outgoing one down so pool threads do not leak on every settings
    # save. getattr rather than a direct call: test doubles standing in for
    # `services.runtime` (StubRuntime and friends) carry no pool and no
    # close() to call.
    close = getattr(old_runtime, "close", None)
    if close is not None:
        close()

    # The one thing a rebuild has to change in the database. An upload whose
    # metered wait was too long to sleep out inline is parked on a deadline
    # computed from the cap that was in force when it was metered (see
    # `Worker._throttle_upload` and `AssetRepo.defer_for_bandwidth`), and
    # nothing else ever revisits that deadline -- not this rebuild, not
    # `upsert_pending`, not `requeue_stale_uploading`, not a restart, since the
    # deadline is persisted while the debt justifying it is in-memory. So a
    # user who raises or removes the cap would go on waiting out the old one,
    # up to weeks for a large backlog, with no way to undo it from the UI.
    # Release those rows (and only those: failure backoffs, halt retries and
    # window defers keep their deadlines) so the new cap re-meters them.
    #
    # Ordered after close() on purpose: this touches the database and can
    # raise (a lock that outlasts busy_timeout, a full disk). Running it
    # earlier meant such a failure propagated out of a rebuild that had
    # already swapped the graph in, leaking the outgoing pool's threads for
    # the life of the process. The swap is done either way; the pool is shut
    # down first, and only then do we risk raising.
    if old_bandwidth != settings.bandwidth_bytes_per_second:
        services.assets.release_bandwidth_deferrals()

    # Close the outgoing Immich client's connection pool too, but only when
    # it is genuinely being replaced (the common "only settings changed"
    # path carries the same client forward -- see the top of this function
    # -- and must never close a client that is still installed and in active
    # use). See `_close_outgoing_immich_client` for why this is deferred
    # rather than done inline.
    #
    # GpmcClient needs no equivalent: gpmc's `Api` opens a fresh
    # `requests.Session` per call, inside a `with` block that closes it again
    # before the call returns (see `gpmc/api.py`) -- it never holds a
    # long-lived session for this to leak. The only thing an outgoing
    # `GpmcClient` keeps around is a `gpmc.Client` per thread (auth-token
    # cache, no open socket), which is reclaimed by ordinary garbage
    # collection once nothing references the outgoing `GpmcClient` any more.
    if old_immich is not None and old_immich is not services.immich:
        _close_outgoing_immich_client(old_immich, getattr(old_runtime, "_pool", None))
