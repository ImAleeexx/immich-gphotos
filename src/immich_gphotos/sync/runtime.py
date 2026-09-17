import threading
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
        # needs it (see _run_wave). worker_threads == 1 never creates this --
        # but that is not the default: Settings.worker_threads defaults to 2
        # and the wizard ships the same value, so a fresh install runs the
        # pool from the first tick that claims more than one asset. Only a
        # settings change (or a hand-edited database row) that lowers
        # worker_threads to 1 avoids creating this. Persisted across ticks
        # (not rebuilt each tick) so
        # each worker thread's lazily-constructed gpmc client (threading.local
        # in GpmcClient) is actually reused rather than rebuilt every tick.
        self._pool: ThreadPoolExecutor | None = None
        # Set by close() *before* the pool is shut down. A tick already in
        # flight on this (now-outgoing) instance is not stopped by a
        # settings-triggered rebuild -- see close()'s docstring -- so
        # _run_wave checks this before every submission and falls back to
        # running the rest of that wave on the calling thread instead of
        # racing the pool's own shutdown.
        self._closing = threading.Event()

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

        claimed = self._assets.claim_next(limit=limit)

        # Resolved only when this tick actually claimed something, and only
        # after the claim -- not unconditionally up front. Every idle tick
        # (the common case: IDLE_SLEEP_SECONDS keeps this running roughly
        # every 2 seconds) previously paid one Immich call per allowed album
        # for nothing. See `_album_allowlist_ids`.
        album_allowlist_ids: frozenset[str] | None = None
        if claimed:
            try:
                album_allowlist_ids = self._album_allowlist_ids()
            except Exception:
                # claim_next already flipped these to UPLOADING via its one
                # atomic UPDATE. A resolution failure here (an Immich call
                # inside _album_allowlist_ids raising) must not strand them
                # there until requeue_stale_uploading eventually notices --
                # return them to PENDING first, exactly like the halt path
                # below, then let the caller (BackgroundLoops.iterate, via
                # _safely) record the failure. Re-raising rather than
                # swallowing this also means no row is ever marked
                # album_excluded against a partial or absent result.
                for stranded in claimed:
                    self._assets.requeue(stranded.asset.immich_id, self._clock.now())
                raise

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
                # window_closed/deferred are populated the same way as the
                # normal return below -- BackgroundLoops.iterate discards the
                # TickResult today (via _safely), so nothing currently reads
                # these on the halt path, but that is not a reason to hand
                # back a value that quietly claims the window was open and
                # nothing was deferred when a halt cut the tick short.
                return TickResult(
                    processed=processed,
                    halted=True,
                    window_closed=not window_open,
                    deferred=deferred,
                )

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

        Also falls back to the sequential path -- for whatever of `wave` is
        not yet dispatched -- the moment `close()` has been (or is
        concurrently being) called on this Runtime. See `close()`: a settings
        save can rebuild and swap in a fresh Runtime while this one's tick()
        is still mid-wave, and its pool may be shutting down underneath this
        call. `self._closing` is checked first so the common case (not
        closing) never even attempts a submission; the `try`/`except`
        around the actual `submit()` calls closes the remaining race window
        where `close()` lands in between that check and this wave's
        dispatch. Either way, whatever was already submitted before that
        point is still awaited -- never abandoned or double-processed --
        and only the not-yet-submitted remainder of the wave runs here
        instead.
        """
        if len(wave) <= 1 or self._closing.is_set():
            return [
                self._worker.process(
                    stored, transfer_allowed=window_open, album_allowlist_ids=album_allowlist_ids
                )
                for stored in wave
            ]
        executor = self._executor()
        futures: list = []
        for i, stored in enumerate(wave):
            try:
                futures.append(
                    executor.submit(
                        self._worker.process,
                        stored,
                        transfer_allowed=window_open,
                        album_allowlist_ids=album_allowlist_ids,
                    )
                )
            except RuntimeError:
                # Raced close(): the pool was shut down between this wave
                # starting and this asset's turn to be dispatched. Assets
                # already submitted (futures[:i]) keep running in the pool
                # and are awaited normally; this one and the rest of the
                # wave run sequentially instead of being lost or raising out
                # of tick().
                results = [future.result() for future in futures]
                results.extend(
                    self._worker.process(
                        remaining,
                        transfer_allowed=window_open,
                        album_allowlist_ids=album_allowlist_ids,
                    )
                    for remaining in wave[i:]
                )
                return results
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
        every settings save. This is *not* safe to treat as "nothing will
        call tick() on this instance again": `rebuild_runtime` calls it from
        the settings-save request thread, with no coordination against the
        background-loop thread, which may still be mid-tick on this exact
        (now-outgoing) instance. Without `self._closing`, that in-flight
        tick's next wave would call `executor.submit` on an already-shutting-
        down pool and raise `RuntimeError`, stranding whatever it had
        claimed in UPLOADING until the next `requeue_stale_uploading` sweep.

        `self._closing` is set *before* `shutdown()` so `_run_wave` can
        notice and fall back to running sequentially on the calling thread
        instead -- see there for the remaining race window and how it is
        closed. `wait=False`: whatever this pool was already asked to do
        keeps running to completion in the background; shutdown only
        refuses new submissions from here on.
        """
        self._closing.set()
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=False)

    def _album_allowlist_ids(self) -> frozenset[str] | None:
        """Resolve `filters.album_allowlist` (Immich album ids) to the set of
        member asset ids, fresh for whichever tick calls this.

        None means the filter is off (no allowlist configured) -- the common
        case -- and skips the Immich calls entirely. `tick` only calls this
        at all when that tick actually claimed something (claim_next
        returned a non-empty batch): with IDLE_SLEEP_SECONDS driving a tick
        roughly every 2 seconds, calling this unconditionally cost one
        Immich request per configured album for nearly every tick, almost
        all of which claimed nothing and threw the result away unused.

        Recomputing this fresh rather than caching it for the runtime's
        lifetime is necessary but not sufficient on its own for "an asset
        added to (or removed from) an allowed album shows up correctly the
        next time it is claimed": a non-member is marked ineligible with
        `ALBUM_EXCLUDED_REASON` via `check_eligibility`, and `claim_next`
        only ever claims PENDING rows. Recomputing this every time does
        nothing for a row stuck in INELIGIBLE. What actually makes re-
        evaluation happen is that `store.assets.AssetRepo.upsert_pending`
        treats `ALBUM_EXCLUDED_REASON` as non-terminal and reopens such a
        row back to PENDING -- so the next webhook or reconciler pass that
        touches it puts it back in front of a `claim_next` call, which is
        what this fresh resolution then actually gets to act on.
        """
        allowlist = self._settings.filters.album_allowlist
        if not allowlist:
            return None
        ids: set[str] = set()
        for album_id in allowlist:
            ids.update(self._immich.album_asset_ids(album_id))
        return frozenset(ids)
