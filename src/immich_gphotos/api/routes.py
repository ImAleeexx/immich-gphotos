from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from immich_gphotos.config import Quality
from immich_gphotos.services import Services
from immich_gphotos.sync.backfill import BACKFILL_CURSOR
from immich_gphotos.sync.throttle import transfer_allowed

router = APIRouter(prefix="/api")

SETTING_KEY = "settings"


class SettingsPatch(BaseModel):
    quality: Quality | None = None
    albums_enabled: bool | None = None
    deletions_enabled: bool | None = None
    worker_threads: int | None = Field(default=None, ge=1, le=16)
    bandwidth_bytes_per_second: int | None = Field(default=None, ge=0)


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
    services: Services = request.app.state.services
    stored = services.settings_repo.get(SETTING_KEY) or {}
    return {
        "quality": services.settings.quality,
        "albums_enabled": services.settings.albums_enabled,
        "deletions_enabled": services.settings.deletions_enabled,
        "worker_threads": services.settings.worker_threads,
        "bandwidth_bytes_per_second": services.settings.bandwidth_bytes_per_second,
        **stored,
    }


@router.put("/settings")
def put_settings(patch: SettingsPatch, request: Request) -> dict:
    services: Services = request.app.state.services
    stored = dict(services.settings_repo.get(SETTING_KEY) or {})
    stored.update(patch.model_dump(exclude_none=True))
    services.settings_repo.set(SETTING_KEY, stored)
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
