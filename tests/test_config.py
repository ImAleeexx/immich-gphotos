from datetime import time, timedelta

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import DeletionPolicy, Filters, RetryPolicy, Settings, Window


def test_settings_defaults_are_safe():
    s = Settings()
    assert s.deletions_enabled is False
    assert s.reconcile_interval == timedelta(minutes=15)
    assert s.reconcile_page_size == 1000
    assert s.reconcile_overlap == timedelta(minutes=5)
    assert s.quality == "original"
    assert s.worker_threads == 2


def test_deletion_policy_thresholds_cannot_be_disabled():
    p = DeletionPolicy(max_fraction=0.0, max_absolute=0)
    assert p.max_fraction > 0.0
    assert p.max_absolute > 0


def test_window_is_none_by_default_and_parses_a_range():
    assert Settings().window is None
    w = Window(start=time(1, 0), end=time(6, 0))
    assert w.start == time(1, 0)


def test_filters_default_to_allowing_everything():
    f = Filters()
    assert f.max_size_bytes is None
    assert f.allowed_types == frozenset({"IMAGE", "VIDEO"})
    assert f.include_archived is True


def test_retry_policy_defaults():
    r = RetryPolicy()
    assert r.max_attempts == 8
    assert r.base_seconds == 30


def test_fake_clock_advances_only_when_told():
    c = FakeClock()
    first = c.now()
    c.advance(timedelta(seconds=90))
    assert c.now() - first == timedelta(seconds=90)
