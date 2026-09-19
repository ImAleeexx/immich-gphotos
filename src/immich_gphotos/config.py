from dataclasses import dataclass, field
from datetime import time, timedelta
from typing import Literal

Quality = Literal["original", "saver", "quota"]

# Bounds on `Settings.worker_threads` and `Settings.bandwidth_bytes_per_second`,
# shared between the API's `SettingsPatch` validation (`api.routes`) and
# `accounts.build._merged_settings`, which validates a stored settings row
# against these same bounds so a hand-edited database row can never apply a
# value the API itself would reject. Defined here rather than in `api.routes`
# because this is a true leaf module (no internal imports) that already owns
# `Settings`, so both `api.routes` and `accounts.build` -- which must never
# import `api` -- can read them without a dependency cycle. `api.routes`
# re-exports all three for its existing importers.
MIN_WORKER_THREADS = 1
MAX_WORKER_THREADS = 16

# 0 passes `ge=0` and looks like a reasonable way to type "no cap" (the UI's
# own "blank = unlimited" hint invites exactly that), but TokenBucket must
# reject or ignore a non-positive rate rather than run with one -- so it can
# never be a valid *cap* in the first place. blank/omitted (None) is still
# how "unlimited" is actually spelled.
MIN_BANDWIDTH_BYTES_PER_SECOND = 65536


@dataclass(frozen=True)
class Window:
    """Hours during which byte transfer is permitted. May wrap midnight."""

    start: time
    end: time


@dataclass(frozen=True)
class Filters:
    max_size_bytes: int | None = None
    allowed_types: frozenset[str] = frozenset({"IMAGE", "VIDEO"})
    skip_raw: bool = False
    include_archived: bool = True
    excluded_tags: frozenset[str] = frozenset()
    album_allowlist: frozenset[str] | None = None


@dataclass(frozen=True)
class RetryPolicy:
    base_seconds: int = 30
    factor: float = 2.0
    max_seconds: int = 3600
    max_attempts: int = 8
    jitter: float = 0.2


@dataclass(frozen=True)
class DeletionPolicy:
    """Guard rails on propagating deletions. Neither bound can be switched off."""

    max_fraction: float = 0.10
    max_absolute: int = 500

    def __post_init__(self) -> None:
        if self.max_fraction <= 0.0:
            object.__setattr__(self, "max_fraction", 0.10)
        if self.max_absolute <= 0:
            object.__setattr__(self, "max_absolute", 500)


@dataclass(frozen=True)
class Settings:
    immich_url: str = ""
    quality: Quality = "original"
    worker_threads: int = 2
    reconcile_interval: timedelta = timedelta(minutes=15)
    reconcile_page_size: int = 1000
    reconcile_overlap: timedelta = timedelta(minutes=5)
    albums_enabled: bool = True
    deletions_enabled: bool = False
    window: Window | None = None
    bandwidth_bytes_per_second: int | None = None
    filters: Filters = field(default_factory=Filters)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    deletion_policy: DeletionPolicy = field(default_factory=DeletionPolicy)
