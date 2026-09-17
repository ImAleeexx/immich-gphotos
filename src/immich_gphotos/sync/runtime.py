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
    window_closed: bool = False


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
        if not transfer_allowed(self._clock.now(), self._settings.window):
            return TickResult(window_closed=True)

        processed = 0
        claimed = self._assets.claim_next(limit=limit)
        for index, stored in enumerate(claimed):
            result = self._worker.process(stored)
            processed += 1
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
        return TickResult(processed=processed)
