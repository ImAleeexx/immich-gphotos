import threading
from datetime import datetime

from immich_gphotos.clock import Clock
from immich_gphotos.config import Window


def transfer_allowed(now: datetime, window: Window | None) -> bool:
    """Whether byte transfer may run at this moment.

    Only transfer is gated. Hash checks and metadata work continue around the
    clock: they are tiny, and they keep the already-present fast path clearing
    the queue for free.
    """
    if window is None:
        return True
    current = now.time()
    if window.start <= window.end:
        return window.start <= current <= window.end
    return current >= window.start or current <= window.end


class TokenBucket:
    """Rate limiter. Returns the wait a caller owes rather than sleeping itself,
    so tests stay instant and the caller decides how to wait."""

    def __init__(self, rate_bytes_per_second: int, clock: Clock) -> None:
        self._rate = max(1, rate_bytes_per_second)
        self._clock = clock
        self._tokens = float(self._rate)
        self._updated = clock.now()
        self._lock = threading.Lock()

    def take(self, n: int) -> float:
        with self._lock:
            now = self._clock.now()
            elapsed = (now - self._updated).total_seconds()
            self._updated = now
            self._tokens = min(float(self._rate), self._tokens + elapsed * self._rate)
            self._tokens -= n
            if self._tokens >= 0:
                return 0.0
            wait = -self._tokens / self._rate
            self._tokens = 0.0
            return wait
