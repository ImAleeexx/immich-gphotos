from dataclasses import dataclass

from immich_gphotos.clock import Clock
from immich_gphotos.config import Settings
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.events import EventRepo
from immich_gphotos.sync.throttle import transfer_allowed
from immich_gphotos.sync.worker import Worker


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
    """One bounded unit of work per call, so scheduling is testable without threads."""

    def __init__(
        self,
        assets: AssetRepo,
        worker: Worker,
        settings: Settings,
        clock: Clock,
        events: EventRepo,
    ) -> None:
        self._assets = assets
        self._worker = worker
        self._settings = settings
        self._clock = clock
        self._events = events
        self._paused_reason: str | None = None

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

        processed = 0
        deferred = 0
        claimed = self._assets.claim_next(limit=limit)
        for index, stored in enumerate(claimed):
            result = self._worker.process(stored, transfer_allowed=window_open)
            processed += 1
            if result.deferred:
                deferred += 1
            if result.halt:
                reason = result.error_class.value if result.error_class else "unknown"
                self.pause(reason)
                # The rest of this batch was already flipped to UPLOADING by
                # claim_next's single atomic UPDATE, but never handed to the
                # worker. Left alone they would be stranded there until some
                # separate crash-recovery sweep happens to run. A halt is
                # routine, not a crash, so tick() cleans up after itself:
                # return them to PENDING without counting an attempt against
                # them, since being queued behind a halted asset is not their
                # fault.
                for stranded in claimed[index + 1 :]:
                    self._assets.requeue(stranded.asset.immich_id, self._clock.now())
                return TickResult(processed=processed, halted=True)
        return TickResult(processed=processed, window_closed=not window_open, deferred=deferred)
