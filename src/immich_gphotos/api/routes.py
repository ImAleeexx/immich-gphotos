from dataclasses import replace
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from immich_gphotos.composition import rebuild_runtime
from immich_gphotos.config import Quality
from immich_gphotos.services import Services
from immich_gphotos.storage_keys import SETTINGS_KEY as SETTING_KEY
from immich_gphotos.sync.backfill import BACKFILL_CURSOR
from immich_gphotos.sync.throttle import transfer_allowed

router = APIRouter(prefix="/api")

# Shared with main._merged_settings, which validates a stored settings row
# against these same bounds so a hand-edited database row cannot apply a
# worker_threads value the API itself would reject.
MIN_WORKER_THREADS = 1
MAX_WORKER_THREADS = 16

# 0 passes `ge=0` and looks like a reasonable way to type "no cap" (the UI's
# own "blank = unlimited" hint invites exactly that), but TokenBucket must
# reject or ignore a non-positive rate rather than run with one -- so it can
# never be a valid *cap* in the first place. blank/omitted (None) is still
# how "unlimited" is actually spelled; also shared with
# main._merged_settings, for the same reason MIN_WORKER_THREADS is.
#
# Bounded well above 1: Worker._throttle_upload now sleeps the *full* wait
# TokenBucket hands back rather than silently truncating it, so the
# configured rate is actually honoured -- which means the rate itself is the
# only thing standing between a "legitimate-looking" setting (ge=1 alone
# would still permit 1 byte/second) and a single upload stalling the
# reconciler, backfill, album mirror and deletion sweep (all of which share
# the one background-loop thread with tick()) for days. 65536 (64 KiB/s) is
# below what any real, deliberately-throttled connection is likely to be
# capped at, so a value at or below it is far more likely a typo or a
# misunderstanding of the field than an intentional rate.
MIN_BANDWIDTH_BYTES_PER_SECOND = 65536

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
    bandwidth_bytes_per_second: int | None = Field(default=None, ge=MIN_BANDWIDTH_BYTES_PER_SECOND)


def resolve_settings_updates(patch: SettingsPatch, *, exclude: set[str]) -> dict[str, Any]:
    """Turn a `SettingsPatch` (or the wizard's `WizardOptions`, which extends
    it) into the dict of fields to actually apply -- distinguishing "field
    omitted" from "field explicitly set to null".

    `model_dump(exclude_none=True)` alone cannot tell those apart, so a client
    sending `bandwidth_bytes_per_second: null` to mean "clear the cap" (both
    UIs do exactly this for a blank field) had that key silently dropped and
    the old cap kept forever. `model_fields_set` is what actually
    distinguishes them: a field the client sent is in that set even when its
    value is `None`.

    Every other field on `SettingsPatch` (`quality`, `albums_enabled`,
    `deletions_enabled`, `worker_threads`) has no `None` state on `Settings`
    itself -- `None` there only ever means "leave unchanged" -- so an
    explicit null for any of those is treated the same as omitting it.
    `bandwidth_bytes_per_second` is the only field that is genuinely nullable
    on `Settings` (`None` means "no cap"), so it is the only one where an
    explicit null is meaningful and must be applied rather than dropped.
    """
    updates = patch.model_dump(exclude_none=True, exclude=exclude)
    if "bandwidth_bytes_per_second" in patch.model_fields_set and patch.bandwidth_bytes_per_second is None:
        updates["bandwidth_bytes_per_second"] = None
    return updates


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
        # Whether the fast, no-copy read path is enabled at all
        # (`IGP_ALLOW_DIRECT_READS`) -- not a live per-asset signal. Whether
        # any given asset actually takes that path still depends on the
        # mounted `originalPath` being readable at the moment it is synced
        # (see `ByteResolver.resolve`), which this does not attempt to
        # predict.
        "direct_reads_enabled": services.allow_direct,
        # The dashboard's activity feed. It used to append a client-side row
        # reading "status refreshed" on every frame -- a heartbeat dressed as
        # history. These are the real events, already redacted on write by
        # the shared Redactor.
        "events": services.events.recent(8),
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
    updates = resolve_settings_updates(patch, exclude={"confirm_deletions"})
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
