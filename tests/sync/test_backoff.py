from datetime import timedelta

from immich_gphotos.config import RetryPolicy
from immich_gphotos.sync.backoff import next_delay, should_quarantine

NO_JITTER = RetryPolicy(jitter=0.0)


def test_delay_grows_exponentially():
    assert next_delay(1, NO_JITTER) == timedelta(seconds=30)
    assert next_delay(2, NO_JITTER) == timedelta(seconds=60)
    assert next_delay(3, NO_JITTER) == timedelta(seconds=120)


def test_delay_is_capped():
    assert next_delay(99, NO_JITTER) == timedelta(seconds=3600)


def test_jitter_stays_within_bounds():
    policy = RetryPolicy(jitter=0.2)
    low = next_delay(3, policy, rand=lambda: 0.0)
    high = next_delay(3, policy, rand=lambda: 1.0)
    assert low == timedelta(seconds=96)  # 120 * 0.8
    assert high == timedelta(seconds=144)  # 120 * 1.2


def test_quarantine_after_max_attempts():
    policy = RetryPolicy(max_attempts=8)
    assert should_quarantine(7, policy) is False
    assert should_quarantine(8, policy) is True
