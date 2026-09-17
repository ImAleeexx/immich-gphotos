from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from immich_gphotos.clock import Clock, SystemClock
from immich_gphotos.config import Settings
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo


@dataclass
class Services:
    """Everything the HTTP layer needs. Assembled once at startup, injected in tests.

    Deliberately not frozen: the wizard and the settings route mutate
    `runtime`, `backfill`, `immich`, `gphotos`, `settings` and `workflow_id`
    in place (see `composition.rebuild_runtime`) so a configuration change
    takes effect on the already-running service without a restart.
    """

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
    immich: Any = None  # ImmichClient, for the diagnostics page and the wizard/settings live swap
    gphotos: Any = None  # GooglePhotosClient, held only so the live swap can rebuild around it
    workflow_id: str | None = None  # set once the wizard registers the workflow
    clock: Clock = field(default_factory=SystemClock)
    redactor: Any = None  # logging.Redactor; shared with the log handler, grown via add_secret
    scratch: Path | None = None  # where ByteResolver stages downloads; needed to rebuild it
    allow_direct: bool = True  # IGP_ALLOW_DIRECT_READS, needed to rebuild ByteResolver
    loops_handle: Any = None  # sync.loops.LoopsHandle; the live-swap target for the background loop
