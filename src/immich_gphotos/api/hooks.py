import hmac
import json

from fastapi import APIRouter, HTTPException, Request

from immich_gphotos.checksum import ChecksumError, normalize_checksum
from immich_gphotos.models import Asset, Priority

router = APIRouter()


def asset_from_webhook(payload: dict) -> Asset:
    """Parse Immich's workflow webhook body: {type, trigger, data: {asset: {...}}}."""
    try:
        raw = payload["data"]["asset"]
        checksum = normalize_checksum(raw["checksum"])
        exif = raw.get("exifInfo") or {}
        return Asset(
            immich_id=raw["id"],
            checksum=checksum,
            filename=raw.get("originalFileName") or raw["id"],
            type=raw.get("type", "IMAGE"),
            size_bytes=exif.get("fileSizeInByte"),
            immich_updated_at=raw.get("updatedAt", ""),
            original_path=raw.get("originalPath"),
            visibility=raw.get("visibility", "timeline"),
            is_offline=bool(raw.get("isOffline", False)),
            is_trashed=bool(raw.get("deletedAt")),
            tags=tuple(t["name"] for t in raw.get("tags") or [] if "name" in t),
        )
    except (KeyError, TypeError, ChecksumError) as exc:
        raise ValueError(f"unusable webhook payload: {exc}") from exc


@router.post("/hooks/immich")
async def receive(request: Request) -> dict:
    services = request.app.state.services
    supplied = request.headers.get(services.webhook_header, "")
    if not hmac.compare_digest(supplied, services.webhook_secret):
        raise HTTPException(status_code=401, detail="bad secret")

    try:
        payload = await request.json()
        asset = asset_from_webhook(payload)
    except (json.JSONDecodeError, ValueError) as exc:
        services.events.add("warn", str(exc))
        raise HTTPException(status_code=400, detail="unusable payload") from exc

    # Return immediately: Immich's webhook action ignores the response and never
    # retries, so no work may depend on this request.
    services.assets.upsert_pending(asset, Priority.WEBHOOK)
    return {"queued": asset.immich_id}
