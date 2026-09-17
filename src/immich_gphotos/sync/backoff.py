import random
from collections.abc import Callable
from datetime import timedelta

from immich_gphotos.config import RetryPolicy


def next_delay(
    attempts: int,
    policy: RetryPolicy,
    rand: Callable[[], float] = random.random,
) -> timedelta:
    """Delay before attempt number `attempts` + 1. `attempts` is 1-based."""
    raw = policy.base_seconds * (policy.factor ** max(0, attempts - 1))
    capped = min(raw, policy.max_seconds)
    if policy.jitter:
        spread = capped * policy.jitter
        capped = capped - spread + (2 * spread * rand())
    return timedelta(seconds=round(capped))


def should_quarantine(attempts: int, policy: RetryPolicy) -> bool:
    return attempts >= policy.max_attempts
