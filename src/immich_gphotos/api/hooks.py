import hmac
import json

from fastapi import APIRouter, HTTPException, Request

from immich_gphotos.checksum import ChecksumError, normalize_checksum
from immich_gphotos.models import Asset, Priority
from immich_gphotos.services import Services

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


async def _receive(services: Services, request: Request) -> dict:
    """Body of the old single-account `receive`, verbatim, taking the already
    -resolved account's `services` instead of reaching for one itself. Both
    `receive_for_account` and `receive_legacy` below share this -- they only
    differ in how they land on `services` in the first place.
    """
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


@router.post("/hooks/immich/{account_id}")
async def receive_for_account(account_id: str, request: Request) -> dict:
    # RULING R1: this path is open by design (`auth.is_open`'s HOOKS_PREFIX
    # rule) -- Immich authenticates with the secret header, not a session --
    # so `require_session` never runs for it and `request.state.services` is
    # never set. Resolve the account straight from the registry instead of
    # reading that attribute.
    registry = request.app.state.accounts
    account = registry.get(account_id)
    # 401, not 404: this endpoint is unauthenticated by design, and a 404
    # here would tell an unauthenticated caller exactly which account ids
    # exist on the install (and which don't) just by probing the path --
    # an enumeration oracle a "bad secret" response never gives away.
    if account is None:
        raise HTTPException(status_code=401, detail="bad secret")
    return await _receive(account.services, request)


@router.post("/hooks/immich")
async def receive_legacy(request: Request) -> dict:
    """The pre-multi-account path, kept permanently. An install upgraded
    from the single-account version already has a workflow registered in
    Immich pointing at this bare URL; repointing or dropping it stops that
    person's backups with nothing in the UI to show for it. So this stays
    bound to whichever account the migration recorded
    (`registry.legacy_account_id()`) for as long as the process runs, not
    merely "the first account" -- `default()` and "the legacy account"
    agree only by coincidence on an install with exactly one account.

    RULING R15: there is no `registry.default()` fallback here, for either
    kind of "no legacy id" -- a dead one (`legacy_account_id()` returns an
    id `registry.get` no longer knows, e.g. Task 8's
    `test_remove_leaves_a_dead_legacy_webhook_key_solvable_by_task_7`) or an
    absent one (`legacy_account_id()` is `None`, because no v1->v2 migration
    ever ran on this data directory). Both are a clean 401.

    This route exists for exactly one reason: to keep a workflow that
    already exists inside someone's Immich, from before multi-account,
    pointed at a real account. The migration that creates that situation
    *always* writes `LEGACY_WEBHOOK_ACCOUNT_KEY` (see
    `accounts.migrate.ensure_control_db`) -- so an install where the key was
    never set is not a migrated single-account install with a workflow to
    honor, it is a fresh multi-account install (or a hand-built test
    registry) with nothing bound to this URL at all. Falling back to
    `default()` in that case does not recover anything real; it manufactures
    a standing alias from the bare path to "whichever account is first by
    position" that nothing in production ever asked for and that silently
    retargets to a different library the moment that account is removed or
    the registration order changes -- the exact class of misrouting this
    route exists to prevent, just reached from the other end. (It is
    secret-gated either way, so nothing can be queued without the target
    account's own secret -- this is a design call against an alias nothing
    needs, not a vulnerability fix.) A future change here must not
    reintroduce `or registry.default()`: both "dead" and "absent" get the
    same 401, and the 401-not-404 reasoning above applies equally -- "no
    account to check the secret against" and "wrong secret" are
    deliberately indistinguishable from the outside.
    """
    registry = request.app.state.accounts
    account_id = registry.legacy_account_id()
    account = registry.get(account_id) if account_id else None
    if account is None:
        raise HTTPException(status_code=401, detail="bad secret")
    return await _receive(account.services, request)
