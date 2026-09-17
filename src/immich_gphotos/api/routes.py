from dataclasses import replace
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from immich_gphotos.composition import rebuild_runtime
from immich_gphotos.config import Quality
from immich_gphotos.services import Services
from immich_gphotos.sync.backfill import BACKFILL_CURSOR
from immich_gphotos.sync.throttle import transfer_allowed

router = APIRouter(prefix="/api")

SETTING_KEY = "settings"

# Shared with main._merged_settings, which validates a stored settings row
# against these same bounds so a hand-edited database row cannot apply a
# worker_threads value the API itself would reject.
MIN_WORKER_THREADS = 1
MAX_WORKER_THREADS = 16

# Spec: deletion propagation is "off by default, behind an explicit toggle
# with typed confirmation" -- this is the only setting in the project that
# can destroy a user's data. The exact phrase the caller must echo back to
# turn it on; turning it off is never gated.
DELETIONS_ENABLE_PHRASE = "ENABLE DELETIONS"


class SettingsPatch(BaseModel):
    quality: Quality | None = None
    albums_enabled: bool | None = None
    deletions_enabled: bool | None = None
    confirm_deletions: str | None = None
    worker_threads: int | None = Field(default=None, ge=MIN_WORKER_THREADS, le=MAX_WORKER_THREADS)
    bandwidth_bytes_per_second: int | None = Field(default=None, ge=0)


def require_deletion_confirmation(patch: SettingsPatch, *, currently_enabled: bool) -> None:
    """Guard the one direction that matters: turning deletion propagation ON.

    Only a patch that actually flips deletions_enabled from off to on needs
    the typed phrase. Disabling stays frictionless (never gated), and a save
    that merely keeps an already-enabled toggle on (e.g. the settings page
    resubmitting the whole form to change worker_threads) is not "enabling"
    anything and does not re-demand the phrase.
    """
    if patch.deletions_enabled is not True or currently_enabled:
        return
    if patch.confirm_deletions != DELETIONS_ENABLE_PHRASE:
        raise HTTPException(
            status_code=422,
            detail=(
                "enabling deletion propagation requires confirm_deletions to be "
                f'exactly "{DELETIONS_ENABLE_PHRASE}"'
            ),
        )


def status_snapshot(services: Services) -> dict[str, Any]:
    backfill_page = services.cursors.get(BACKFILL_CURSOR)
    return {
        "counts": services.assets.counts_by_state(),
        "paused_reason": getattr(services.runtime, "paused_reason", None),
        "backfill": {"running": backfill_page is not None, "page": backfill_page},
        "window_open": transfer_allowed(services.clock.now(), services.settings.window),
    }


@router.get("/status")
def status(request: Request) -> dict:
    return status_snapshot(request.app.state.services)


@router.get("/failures")
def failures(request: Request) -> list[dict]:
    services: Services = request.app.state.services
    return [
        {
            "immich_id": s.asset.immich_id,
            "filename": s.asset.filename,
            "attempts": s.attempts,
            "error_class": s.error_class.value if s.error_class else None,
            "last_error": s.last_error,
        }
        for s in services.assets.failures()
    ]


@router.post("/failures/{asset_id}/retry")
def retry(asset_id: str, request: Request) -> dict:
    services: Services = request.app.state.services
    if not services.assets.retry_now(asset_id):
        raise HTTPException(status_code=404, detail="no quarantined asset with that id")
    services.events.add("info", f"manual retry requested for {asset_id}")
    return {"retrying": asset_id}


@router.get("/settings")
def get_settings(request: Request) -> dict:
    """Report the settings the running service is actually using.

    This used to splat the raw stored row over the live values, so a write
    that failed to take effect (before the live-swap in `rebuild_runtime`
    existed) would still be reported back as applied -- dangerously so for
    `deletions_enabled`, where a user turning deletions *off* would see "off"
    while a stale in-memory loop kept trashing Google items. `put_settings`
    now rebuilds the live runtime graph on every write, so `services.settings`
    is always the truth and nothing needs to be read back from storage here.
    """
    services: Services = request.app.state.services
    return {
        "quality": services.settings.quality,
        "albums_enabled": services.settings.albums_enabled,
        "deletions_enabled": services.settings.deletions_enabled,
        "worker_threads": services.settings.worker_threads,
        "bandwidth_bytes_per_second": services.settings.bandwidth_bytes_per_second,
    }


@router.put("/settings")
def put_settings(patch: SettingsPatch, request: Request) -> dict:
    services: Services = request.app.state.services
    require_deletion_confirmation(patch, currently_enabled=services.settings.deletions_enabled)
    updates = patch.model_dump(exclude_none=True, exclude={"confirm_deletions"})
    stored = dict(services.settings_repo.get(SETTING_KEY) or {})
    stored.update(updates)
    services.settings_repo.set(SETTING_KEY, stored)
    if updates:
        # Live swap: rebuild the runtime graph (Runtime, Reconciler, the
        # DeletionSweeper, ...) against the new Settings and hand it to the
        # background loop in place, so e.g. deletions_enabled=False stops the
        # sweeper on its next pass instead of only after a manual restart.
        rebuild_runtime(services, settings=replace(services.settings, **updates))
    services.events.add("info", "settings updated")
    return stored


@router.post("/backfill/start")
def backfill_start(request: Request) -> dict:
    services: Services = request.app.state.services
    services.backfill.start()
    services.events.add("info", "backfill started")
    return {"running": True}


@router.post("/backfill/reset")
def backfill_reset(request: Request) -> dict:
    services: Services = request.app.state.services
    services.backfill.reset()
    return {"running": False}
