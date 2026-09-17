from datetime import UTC, datetime, time, timedelta

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Window
from immich_gphotos.sync.throttle import TokenBucket, transfer_allowed


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 17, hour, minute, tzinfo=UTC)


def test_no_window_always_allows():
    assert transfer_allowed(at(3), None) is True


def test_simple_window():
    window = Window(start=time(9, 0), end=time(17, 0))
    assert transfer_allowed(at(12), window) is True
    assert transfer_allowed(at(8, 59), window) is False
    assert transfer_allowed(at(17, 1), window) is False


def test_window_wrapping_midnight():
    window = Window(start=time(23, 0), end=time(6, 0))
    assert transfer_allowed(at(23, 30), window) is True
    assert transfer_allowed(at(2), window) is True
    assert transfer_allowed(at(12), window) is False


def test_window_boundaries_are_inclusive():
    window = Window(start=time(1, 0), end=time(6, 0))
    assert transfer_allowed(at(1, 0), window) is True
    assert transfer_allowed(at(6, 0), window) is True


def test_bucket_allows_a_burst_up_to_capacity():
    bucket = TokenBucket(rate_bytes_per_second=1000, clock=FakeClock())
    assert bucket.take(1000) == 0.0


def test_bucket_makes_the_caller_wait_when_drained():
    clock = FakeClock()
    bucket = TokenBucket(rate_bytes_per_second=1000, clock=clock)
    bucket.take(1000)
    assert bucket.take(500) == 0.5


def test_bucket_refills_over_time():
    clock = FakeClock()
    bucket = TokenBucket(rate_bytes_per_second=1000, clock=clock)
    bucket.take(1000)
    clock.advance(timedelta(seconds=2))
    assert bucket.take(1000) == 0.0


def test_a_zero_rate_is_rejected_rather_than_clamped_to_the_most_extreme_throttle():
    """C2: TokenBucket used to silently clamp a nonsensical rate (0, or
    negative) to 1 byte/second via max(1, rate) -- the most extreme possible
    throttle, and the wrong failure direction: a 5 MB upload at 1 byte/second
    produces a ~58-day sleep on the single background-loop thread. Reject
    instead; the only valid way to mean "unlimited" is not constructing a
    TokenBucket at all."""
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(rate_bytes_per_second=0, clock=FakeClock())


def test_a_negative_rate_is_also_rejected():
    with pytest.raises(ValueError, match="positive"):
        TokenBucket(rate_bytes_per_second=-5, clock=FakeClock())


def test_takes_at_the_same_instant_queue_up_behind_one_another_with_cumulative_waits():
    """C1 regression guard. `take` used to zero the bucket after computing a
    wait, forgiving the debt it had just handed the caller. That was harmless
    while the only caller slept the wait out inline -- the sleep *was* the
    enforcement -- but `Worker._throttle_upload` now defers an asset for a
    long wait instead of sleeping, so nothing else enforces it: every asset
    metered in the same instant got the *same* wait, came due at the same
    moment, and then uploaded unmetered. The debt must persist, so each
    successive take queues behind the ones before it.

    100 bytes/second, 10,000-byte assets: the bucket starts with one second
    of credit, so the first owes 99s and each further one owes 100s more."""
    bucket = TokenBucket(rate_bytes_per_second=100, clock=FakeClock())
    assert [bucket.take(10_000) for _ in range(5)] == [99.0, 199.0, 299.0, 399.0, 499.0]


def test_an_idle_bucket_accrues_at_most_one_seconds_worth_of_credit():
    """The other side of the same line: carrying debt forward must not also
    let an idle bucket bank unlimited credit and then burst through the cap.
    Capacity stays one second of tokens however long the bucket sat unused."""
    clock = FakeClock()
    bucket = TokenBucket(rate_bytes_per_second=100, clock=clock)
    clock.advance(timedelta(hours=1))
    assert bucket.take(300) == 2.0  # 100 tokens of credit, not an hour's worth
