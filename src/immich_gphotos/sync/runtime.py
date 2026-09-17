from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from immich_gphotos.clock import Clock
from immich_gphotos.config import Settings
from immich_gphotos.immich.protocol import ImmichClient
from immich_gphotos.models import StoredAsset
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.events import EventRepo
from immich_gphotos.sync.throttle import transfer_allowed
from immich_gphotos.sync.worker import Worker, WorkerResult


@dataclass(frozen=True)
class TickResult:
    processed: int = 0
    halted: bool = False
    # Whether the schedule window was closed during this tick. This no
    # longer means "nothing ran" -- claim_next, eligibility, the local
    # duplicate lookup and the remote hash check all proceed regardless of
    # the window. It only means that any asset which still needed bytes
    # moved after those checks was deferred rather than uploaded; see
    # `deferred`.
    window_closed: bool = False
    deferred: int = 0


class Runtime:
    """One bounded unit of work per call, so scheduling itself is testable
    without threads -- `tick()` is always invoked from a single caller, one
    at a time.

    Within a single tick, the claimed batch is optionally fanned out across
    `settings.worker_threads` -- see `_run_wave`. `worker_threads == 1` never
    creates a thread and processes the batch exactly as a plain sequential
    loop would.
    """

    def __init__(
        self,
        assets: AssetRepo,
        worker: Worker,
        settings: Settings,
        clock: Clock,
        events: EventRepo,
        *,
        immich: ImmichClient,
    ) -> None:
        self._assets = assets
        self._worker = worker
        self._settings = settings
        self._clock = clock
        self._events = events
        # Only used to resolve `settings.filters.album_allowlist` (Immich
        # album ids) to member asset ids, once per tick -- see
        # `_album_allowlist_ids`. Every other step in a tick is local/DB-only.
        self._immich = immich
        self._paused_reason: str | None = None
        # Lazy: only ever created if a wave with more than one asset actually
        # needs it (see _run_wave). worker_threads == 1 -- the common/default
        # case for anyone who hasn't opted in to a pool -- never creates this,
        # so it adds no thread and behaves byte-for-byte like the old
        # sequential loop. Persisted across ticks (not rebuilt each tick) so
        # each worker thread's lazily-constructed gpmc client (threading.local
        # in GpmcClient) is actually reused rather than rebuilt every tick.
        self._pool: ThreadPoolExecutor | None = None

    @property
    def paused_reason(self) -> str | None:
        return self._paused_reason

    def pause(self, reason: str) -> None:
        self._paused_reason = reason
        self._events.add("error", f"transfer paused: {reason}")

    def resume(self) -> None:
        if self._paused_reason:
            self._events.add("info", "transfer resumed")
        self._paused_reason = None

    def tick(self, limit: int = 8) -> TickResult:
        if self._paused_reason:
            return TickResult()

        # Schedule and bandwidth gate the byte transfer only -- claiming,
        # eligibility, the local duplicate lookup and the remote hash check
        # all run at any hour. Computed once per tick and handed to every
        # asset in the batch, so the whole batch sees a consistent window
        # state rather than possibly straddling the boundary mid-tick.
        window_open = transfer_allowed(self._clock.now(), self._settings.window)
        album_allowlist_ids = self._album_allowlist_ids()

        claimed = self._assets.claim_next(limit=limit)
        # Bounded to at most worker_threads at a time ("a wave"). Waves run
        # one after another; within a wave, every asset is dispatched before
        # any of that wave's results are known. worker_threads == 1 means
        # every wave has exactly one asset, so this degenerates to the
        # original sequential loop exactly (same order, same immediate halt
        # check after each single asset, no thread ever created).
        pool_size = max(1, self._settings.worker_threads)

        processed = 0
        deferred = 0
        index = 0
        while index < len(claimed):
            wave = claimed[index : index + pool_size]
            results = self._run_wave(wave, window_open, album_allowlist_ids)
            processed += len(wave)
            deferred += sum(1 for result in results if result.deferred)

            halted = next((result for result in results if result.halt), None)
            if halted is not None:
                # Only work that was never dispatched -- later waves -- is
                # "the rest of the batch" here. Everything in the halting
                # wave itself was already submitted (and, for a pool size >
                # 1, may have run concurrently with the asset that halted);
                # its own result already recorded whatever happened to it.
                # Those undispatched assets were flipped to UPLOADING by
                # claim_next's single atomic UPDATE but never handed to a
                # worker. Left alone they would be stranded there until some
                # separate crash-recovery sweep happens to run. A halt is
                # routine, not a crash, so tick() cleans up after itself:
                # return them to PENDING without counting an attempt against
                # them, since being queued behind a halted asset is not
                # their fault.
                for stranded in claimed[index + len(wave) :]:
                    self._assets.requeue(stranded.asset.immich_id, self._clock.now())
                reason = halted.error_class.value if halted.error_class else "unknown"
                self.pause(reason)
                return TickResult(processed=processed, halted=True)

            index += len(wave)

        return TickResult(processed=processed, window_closed=not window_open, deferred=deferred)

    def _run_wave(
        self,
        wave: list[StoredAsset],
        window_open: bool,
        album_allowlist_ids: frozenset[str] | None,
    ) -> list[WorkerResult]:
        """Process one wave of up to `worker_threads` assets.

        A wave of zero or one assets (covers every wave when worker_threads
        is 1, and the last, possibly-partial wave otherwise) is run directly
        on the calling thread -- no executor, no thread, identical to the
        original sequential path. A wave of more than one asset is fanned out
        across the shared, lazily-created pool and waited on in full before
        this returns, which is what keeps the halt check in `tick` accurate:
        every asset in the wave has actually finished, one way or another,
        before the caller decides whether to stop.
        """
        if len(wave) <= 1:
            return [
                self._worker.process(
                    stored, transfer_allowed=window_open, album_allowlist_ids=album_allowlist_ids
                )
                for stored in wave
            ]
        executor = self._executor()
        futures = [
            executor.submit(
                self._worker.process,
                stored,
                transfer_allowed=window_open,
                album_allowlist_ids=album_allowlist_ids,
            )
            for stored in wave
        ]
        return [future.result() for future in futures]

    def _executor(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=max(1, self._settings.worker_threads),
                thread_name_prefix="igp-worker",
            )
        return self._pool

    def close(self) -> None:
        """Shut down this runtime's worker pool, if one was ever created.

        `rebuild_runtime` calls this on the outgoing Runtime after a settings
        change swaps in a freshly built one, so pool threads do not leak on
        every settings save. `wait=False`: a tick already in flight on this
        (now-replaced) runtime keeps whatever it already submitted running to
        completion in the background -- shutdown only refuses *new*
        submissions from here on, which is safe since nothing will call
        tick() on this instance again.
        """
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=False)

    def _album_allowlist_ids(self) -> frozenset[str] | None:
        """Resolve `filters.album_allowlist` (Immich album ids) to the set of
        member asset ids, fresh for this tick.

        None means the filter is off (no allowlist configured) -- the common
        case -- and skips the Immich calls entirely. Recomputing this once
        per tick, the same way `window_open` is, rather than caching it for
        the runtime's lifetime, means an asset added to (or removed from) an
        allowed album shows up correctly the next time it is claimed, without
        needing a settings save to force a rebuild.
        """
        allowlist = self._settings.filters.album_allowlist
        if not allowlist:
            return None
        ids: set[str] = set()
        for album_id in allowlist:
            ids.update(self._immich.album_asset_ids(album_id))
        return frozenset(ids)
