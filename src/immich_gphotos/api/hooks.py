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
    # RULING R1: `/hooks/immich` is in `auth.OPEN_PATHS`, so `require_session`
    # returns before it ever resolves an account -- `request.state.services`
    # is simply never set for this path, and reading it here would turn
    # every webhook delivery into a 500 that Immich's action never surfaces
    # to anyone. Resolve the account directly from the registry instead.
    # Task 7 adds a per-account webhook path; until then this is the one
    # pre-multi-account install's account, recovered the same way the
    # migration recorded it (`legacy_webhook_account`), falling back to the
    # first account if that setting is somehow missing.
    registry = request.app.state.accounts
    account = registry.get(registry.legacy_account_id()) or registry.default()
    if account is None:
        # No account exists at all (a fresh install, or every account
        # removed). Nothing to enqueue against; fail closed rather than
        # raise on `account.services` below.
        raise HTTPException(status_code=503, detail="no accounts configured")
    services = account.services
    supplied = request.headers.get(services.webhook_header, "")
    # Starlette decodes headers as latin-1, so a supplied secret with any byte
    # >= 0x80 is a non-ASCII str; hmac.compare_digest raises TypeError on that
    # rather than just returning False. Compare bytes instead, so a malformed
    # header is a clean rejection, not a 500.
    if not hmac.compare_digest(supplied.encode(), services.webhook_secret.encode()):
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
