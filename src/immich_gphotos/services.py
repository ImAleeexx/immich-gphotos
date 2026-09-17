from dataclasses import dataclass, field
from typing import Any

from immich_gphotos.clock import Clock, SystemClock
from immich_gphotos.config import Settings
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo


@dataclass
class Services:
    """Everything the HTTP layer needs. Assembled once at startup, injected in tests."""

    assets: AssetRepo
    albums: AlbumRepo
    cursors: CursorRepo
    settings_repo: SettingRepo
    events: EventRepo
    runtime: Any  # sync.runtime.Runtime; Any avoids a cycle
    settings: Settings
    webhook_secret: str
    webhook_header: str = "X-IGP-Secret"
    backfill: Any = None
    wizard: Any = None
    immich: Any = None  # ImmichClient, for the diagnostics page
    workflow_id: str | None = None  # set once the wizard registers the workflow
    clock: Clock = field(default_factory=SystemClock)
