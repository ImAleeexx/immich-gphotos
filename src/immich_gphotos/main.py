import os
import threading
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import get_args

from immich_gphotos.api.app import create_app
from immich_gphotos.api.routes import (
    MAX_WORKER_THREADS,
    MIN_BANDWIDTH_BYTES_PER_SECOND,
    MIN_WORKER_THREADS,
    SETTING_KEY,
)
from immich_gphotos.clock import SystemClock
from immich_gphotos.composition import build_runtime_graph
from immich_gphotos.config import Quality, Settings
from immich_gphotos.gphotos.client import GpmcClient
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.client import HttpImmichClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.logging import Redactor, configure_logging
from immich_gphotos.services import Services
from immich_gphotos.setup.wizard import Wizard
from immich_gphotos.storage_keys import (
    GOOGLE_AUTH_KEY,
    IMMICH_KEY_KEY,
    IMMICH_URL_KEY,
    SECRET_KEY,
    WORKFLOW_ID_KEY,
)
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo
from immich_gphotos.sync.loops import STALE_UPLOAD_AGE, LoopsHandle

_QUALITIES = frozenset(get_args(Quality))


def _merged_settings(immich_url: str, stored: object) -> Settings:
    """Apply settings the API has persisted (under the "settings" key) over the
    dataclass defaults.

    The API writes `quality`, `albums_enabled`, `deletions_enabled`,
    `worker_threads` and `bandwidth_bytes_per_second` into that row; nothing
    else builds a `Settings` from it, so without this every change made in the
    UI is silently lost on the next restart.

    The row is user-writable JSON, so this is defensive: an unknown key or a
    value of the wrong type/range is ignored rather than raised, so a
    malformed settings row can never make the container unstartable.
    """
    overrides: dict[str, object] = {}
    if isinstance(stored, dict):
        quality = stored.get("quality")
        if isinstance(quality, str) and quality in _QUALITIES:
            overrides["quality"] = quality

        for key in ("albums_enabled", "deletions_enabled"):
            value = stored.get(key)
            if isinstance(value, bool):
                overrides[key] = value

        worker_threads = stored.get("worker_threads")
        if (
            isinstance(worker_threads, int)
            and not isinstance(worker_threads, bool)
            and MIN_WORKER_THREADS <= worker_threads <= MAX_WORKER_THREADS
        ):
            overrides["worker_threads"] = worker_threads

        bandwidth = stored.get("bandwidth_bytes_per_second")
        if (
            isinstance(bandwidth, int)
            and not isinstance(bandwidth, bool)
            and bandwidth >= MIN_BANDWIDTH_BYTES_PER_SECOND
        ):
            overrides["bandwidth_bytes_per_second"] = bandwidth

    return replace(Settings(immich_url=immich_url), **overrides)


def build_services(data_dir: Path, env: Mapping[str, str] | None = None) -> tuple[Services, LoopsHandle]:
    """Compose everything. Credentials come from the database, never from env."""
    env = env if env is not None else os.environ
    clock = SystemClock()
    conn = connect(Path(data_dir) / "immich-gphotos.db")

    settings_repo = SettingRepo(conn)

    secret = settings_repo.get(SECRET_KEY)
    if not secret:
        secret = Wizard.generate_secret()
        settings_repo.set(SECRET_KEY, secret)

    settings = _merged_settings(str(settings_repo.get(IMMICH_URL_KEY) or ""), settings_repo.get(SETTING_KEY))

    api_key = settings_repo.get(IMMICH_KEY_KEY)
    auth_data = settings_repo.get(GOOGLE_AUTH_KEY)

    # One Redactor instance backs the persisted stores (AssetRepo.last_error,
    # EventRepo.add) and the logging handler below, so credential text is
    # scrubbed the same way wherever it might land -- and so a credential the
    # wizard persists *after* this boot (via Redactor.add_secret) reaches both
    # at once rather than only whichever copy a route happened to update.
    redactor = Redactor([secret, api_key, auth_data])
    configure_logging(env.get("IGP_LOG_LEVEL", "INFO"), redactor=redactor)

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

    scratch = Path(env.get("IGP_SCRATCH_DIR") or Path(data_dir) / "scratch")
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
    )
    loops_handle = LoopsHandle(loops)
    services.loops_handle = loops_handle

    # Recover anything a crash left claimed mid-upload. BackgroundLoops repeats
    # this periodically; this is only the boot-time pass, for a crash that
    # happened before this process ever ran the loop.
    assets.requeue_stale_uploading(older_than=STALE_UPLOAD_AGE)
    return services, loops_handle


def main() -> None:
    import uvicorn

    data_dir = Path(os.environ.get("IGP_DATA_DIR", "/data"))
    services, loops = build_services(data_dir)

    stop = threading.Event()
    thread = threading.Thread(target=loops.run_forever, args=(stop,), daemon=True)
    thread.start()

    try:
        uvicorn.run(
            create_app(services),
            host=os.environ.get("IGP_HOST", "0.0.0.0"),  # noqa: S104 - it is a container
            port=int(os.environ.get("IGP_PORT", "8080")),
            log_config=None,
        )
    finally:
        stop.set()
