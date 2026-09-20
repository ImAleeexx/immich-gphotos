"""One account's runtime graph: the stores, clients and background loops for
a single `data_dir` (an account directory, or the legacy top-level data
directory before multi-account existed).

Split out of `main.build_services` so `AccountRegistry` can call it once per
account, sharing one `Clock` and one `Redactor` across all of them rather
than each account reinventing its own. `main.build_services` is kept as a
thin single-account wrapper around this for `tests/test_main.py` and anyone
still calling it directly.
"""

import os
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Any, get_args

from immich_gphotos.clock import Clock, SystemClock
from immich_gphotos.composition import build_runtime_graph
from immich_gphotos.config import (
    MAX_WORKER_THREADS,
    MIN_BANDWIDTH_BYTES_PER_SECOND,
    MIN_WORKER_THREADS,
    Quality,
    Settings,
)
from immich_gphotos.gphotos.client import GpmcClient
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.client import HttpImmichClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.logging import Redactor, configure_logging
from immich_gphotos.services import Services
from immich_gphotos.setup.wizard import Wizard
from immich_gphotos.storage_keys import (
    GLOBAL_SETTING_KEYS,
    GOOGLE_AUTH_KEY,
    IMMICH_KEY_KEY,
    IMMICH_URL_KEY,
    SECRET_KEY,
    SETTINGS_KEY,
    WORKFLOW_ID_KEY,
)
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo
from immich_gphotos.sync.loops import STALE_UPLOAD_AGE, LoopsHandle
from immich_gphotos.sync.throttle import TokenBucket

_QUALITIES = frozenset(get_args(Quality))

# Every key the "settings" row can carry, account-scoped or global. Shared by
# both halves of `_merged_settings` below so the two loops that walk `stored`
# and `stored_global` visit the same key set through the same validation --
# they just differ in which of the two dicts they read from and (for
# `stored_global`) that only the global subset is ever considered.
_SETTINGS_KEYS = (
    "quality",
    "albums_enabled",
    "deletions_enabled",
    "worker_threads",
    "bandwidth_bytes_per_second",
)


def _validated(key: str, value: object) -> object | None:
    """Validate one settings-row value against the same bounds `SettingsPatch`
    enforces in `api.routes` -- so a hand-edited database row, in either the
    account's copy of the settings row or the control database's global copy,
    can never apply a value the API itself would reject.

    Returns the value unchanged when it is valid for `key`, else `None`.
    Extracted out of `_merged_settings` so the identical check is applied to
    both the account row and the global row rather than copy-pasted between
    them (they used to be one dict; now they are two).
    """
    if key == "quality":
        return value if isinstance(value, str) and value in _QUALITIES else None
    if key in ("albums_enabled", "deletions_enabled"):
        return value if isinstance(value, bool) else None
    if key == "worker_threads":
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and MIN_WORKER_THREADS <= value <= MAX_WORKER_THREADS
        ):
            return value
        return None
    if key == "bandwidth_bytes_per_second":
        if isinstance(value, int) and not isinstance(value, bool) and value >= MIN_BANDWIDTH_BYTES_PER_SECOND:
            return value
        return None
    return None


def _merged_settings(immich_url: str, stored: object, stored_global: object = None) -> Settings:
    """Apply settings the API has persisted over the dataclass defaults.

    `stored` is the account's own copy of the "settings" row (`quality`,
    `albums_enabled`, `deletions_enabled`, and -- only on a database that
    predates the account/global split, see `accounts.migrate` -- a leftover
    copy of `worker_threads`/`bandwidth_bytes_per_second`). `stored_global` is
    the control database's copy of the row, which holds only the two keys in
    `storage_keys.GLOBAL_SETTING_KEYS`: the resources they govern (one
    uplink, one machine) are shared by every account, not owned by one.
    Nothing else builds a `Settings` from either row, so without this every
    change made in the UI is silently lost on the next restart.

    Precedence is per key, not per row: for each of the two global keys, a
    valid value found in `stored_global` wins over whatever `stored` has for
    that same key; a key that is missing from, or invalid in, `stored_global`
    falls back to `stored`'s copy instead of being left unset. That fallback
    is what a database not yet through the split relies on (`stored_global`
    is empty/absent there, so both global keys come from `stored`). In
    practice a *partial* global row -- one global key present, the other
    still only in `stored` -- never actually happens: `accounts.migrate.
    ensure_control_db` moves both global keys into the control database in
    one write, so a migrated database either has both there or (pre-split)
    neither. But that is a property of the migration, not of this function --
    a hand-edited or partially-written control row is still resolved
    correctly key by key, not row by row.

    Both rows are user-writable JSON, so this is defensive throughout: an
    unknown key or a value of the wrong type/range is ignored rather than
    raised, so a malformed settings row -- in either database -- can never
    make the container unstartable.
    """
    overrides: dict[str, object] = {}
    if isinstance(stored, dict):
        for key in _SETTINGS_KEYS:
            if key not in stored:
                continue
            validated = _validated(key, stored[key])
            if validated is not None:
                overrides[key] = validated

    if isinstance(stored_global, dict):
        for key in GLOBAL_SETTING_KEYS:
            if key not in stored_global:
                continue
            validated = _validated(key, stored_global[key])
            if validated is not None:
                overrides[key] = validated

    return replace(Settings(immich_url=immich_url), **overrides)


def build_account_services(
    account_dir: Path,
    *,
    clock: Clock | None = None,
    redactor: Redactor | None = None,
    env: Mapping[str, str] | None = None,
    global_settings: dict | None = None,
    bandwidth: TokenBucket | None = None,
    gate: AbstractContextManager[Any] | None = None,
) -> tuple[Services, LoopsHandle]:
    """One account's graph. Credentials come from that account's database, never from env.

    `global_settings` is the control database's copy of the settings row
    (`AccountRegistry.settings.get(SETTINGS_KEY)`) and is threaded straight
    into `_merged_settings`, which is where the global-vs-account precedence
    actually lives (Task 5). `bandwidth` and `gate` are the process-wide
    shared limiters from Task 6 -- `AccountRegistry` builds one `TokenBucket`
    and one upload semaphore from the global settings row and passes the
    *same* instances into every account's `build_account_services` call, so
    this account's `Worker` meters and gates against the one bucket/slot the
    whole process shares rather than a private copy of its own.
    """
    env = env if env is not None else os.environ
    clock = clock if clock is not None else SystemClock()
    conn = connect(Path(account_dir) / "immich-gphotos.db")

    settings_repo = SettingRepo(conn)

    secret = settings_repo.get(SECRET_KEY)
    if not secret:
        secret = Wizard.generate_secret()
        settings_repo.set(SECRET_KEY, secret)

    settings = _merged_settings(
        str(settings_repo.get(IMMICH_URL_KEY) or ""),
        settings_repo.get(SETTINGS_KEY),
        global_settings,
    )

    api_key = settings_repo.get(IMMICH_KEY_KEY)
    auth_data = settings_repo.get(GOOGLE_AUTH_KEY)

    # One Redactor instance backs the persisted stores (AssetRepo.last_error,
    # EventRepo.add) and the logging handler, so credential text is scrubbed
    # the same way wherever it might land -- and so a credential the wizard
    # persists *after* this boot (via Redactor.add_secret) reaches both at
    # once rather than only whichever copy a route happened to update. When
    # the registry hands us a shared Redactor (every account after the
    # first), we grow it in place instead of constructing our own, and skip
    # configure_logging entirely: the registry installs the log handler once,
    # for all accounts together, since they all write to the one log stream.
    if redactor is None:
        redactor = Redactor([secret, api_key, auth_data])
        configure_logging(env.get("IGP_LOG_LEVEL", "INFO"), redactor=redactor)
    else:
        redactor.add_secret(secret)
        redactor.add_secret(api_key)
        redactor.add_secret(auth_data)

    assets = AssetRepo(conn, clock, redactor=redactor)
    albums = AlbumRepo(conn)
    cursors = CursorRepo(conn)
    events = EventRepo(conn, clock, redactor=redactor)

    # Until the wizard has been completed the fakes stand in, so the service boots,
    # serves the wizard and never crash-loops on missing credentials.
    immich = (
        HttpImmichClient(settings.immich_url, str(api_key))
        if settings.immich_url and api_key
        else FakeImmichClient()
    )
    gphotos = GpmcClient(str(auth_data), quality=settings.quality) if auth_data else FakeGooglePhotosClient()

    scratch = Path(env.get("IGP_SCRATCH_DIR") or Path(account_dir) / "scratch")
    allow_direct = env.get("IGP_ALLOW_DIRECT_READS", "true").lower() != "false"

    runtime, backfill, loops = build_runtime_graph(
        immich=immich,
        gphotos=gphotos,
        settings=settings,
        assets=assets,
        albums=albums,
        cursors=cursors,
        events=events,
        clock=clock,
        scratch=scratch,
        allow_direct=allow_direct,
        bandwidth=bandwidth,
        gate=gate,
    )

    workflow_id = settings_repo.get(WORKFLOW_ID_KEY)
    services = Services(
        assets=assets,
        albums=albums,
        cursors=cursors,
        settings_repo=settings_repo,
        events=events,
        runtime=runtime,
        settings=settings,
        webhook_secret=str(secret),
        backfill=backfill,
        wizard=Wizard(),
        immich=immich,
        gphotos=gphotos,
        workflow_id=str(workflow_id) if workflow_id else None,
        clock=clock,
        redactor=redactor,
        scratch=scratch,
        allow_direct=allow_direct,
        bandwidth=bandwidth,
        gate=gate,
    )
    loops_handle = LoopsHandle(loops)
    services.loops_handle = loops_handle

    # Recover anything a crash left claimed mid-upload. BackgroundLoops repeats
    # this periodically; this is only the boot-time pass, for a crash that
    # happened before this process ever ran the loop.
    assets.requeue_stale_uploading(older_than=STALE_UPLOAD_AGE)
    return services, loops_handle
