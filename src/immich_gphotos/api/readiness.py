"""What still stands between this install and a working mirror.

The dashboard polls this, so every check reads local state only -- no
outbound Immich or Google call. That is also why `delivery` reports on
whether a workflow id is stored rather than re-detecting the server's mode:
detection costs an HTTP round trip to Immich, and the reconciler covers the
no-workflow case anyway.
"""

from dataclasses import dataclass
from typing import Literal

from fastapi import APIRouter, Request

from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import AssetState
from immich_gphotos.services import Services
from immich_gphotos.storage_keys import (
    GOOGLE_AUTH_KEY,
    IMMICH_KEY_KEY,
    IMMICH_URL_KEY,
    WORKFLOW_ID_KEY,
)
from immich_gphotos.sync.backfill import BACKFILL_CURSOR

router = APIRouter(prefix="/api")

CheckState = Literal["ok", "pending", "attention"]
Overall = Literal["needs_setup", "attention", "ready"]

# Credentials alone are not enough: main.build_services falls back to the
# fakes when they are absent, and the wizard swaps the real client in via
# composition.rebuild_runtime. A stored key with a fake client still attached
# means the swap did not happen, and reporting "connected" there would be a
# lie the operator has no way to see through.
SETUP_CHECKS = ("immich", "google")


@dataclass(frozen=True)
class Check:
    id: str
    label: str
    state: CheckState
    detail: str
    action_href: str | None = None
    action_label: str | None = None

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "state": self.state,
            "detail": self.detail,
            "action_href": self.action_href,
            "action_label": self.action_label,
        }


@dataclass(frozen=True)
class Readiness:
    overall: Overall
    checks: tuple[Check, ...]

    def as_dict(self) -> dict:
        return {"overall": self.overall, "checks": [c.as_dict() for c in self.checks]}


def _immich_check(services: Services) -> Check:
    url = services.settings_repo.get(IMMICH_URL_KEY)
    key = services.settings_repo.get(IMMICH_KEY_KEY)
    live = services.immich is not None and not isinstance(services.immich, FakeImmichClient)
    if url and key and live:
        return Check("immich", "Immich", "ok", f"Connected to {url}.")
    return Check(
        "immich",
        "Immich",
        "pending",
        "Not connected. The wizard checks the server and the API key's permissions before saving either.",
        "/wizard",
        "Connect Immich",
    )


def _google_check(services: Services) -> Check:
    stored = services.settings_repo.get(GOOGLE_AUTH_KEY)
    live = services.gphotos is not None and not isinstance(services.gphotos, FakeGooglePhotosClient)
    if stored and live:
        return Check("google", "Google Photos", "ok", "Credential accepted.")
    return Check(
        "google",
        "Google Photos",
        "pending",
        "No credential yet. This is the one step that needs an Android device.",
        "/wizard",
        "Add the credential",
    )


def _delivery_check(services: Services) -> Check:
    if services.settings_repo.get(WORKFLOW_ID_KEY):
        return Check("delivery", "Delivery", "ok", "Immich pushes new assets across as they land.")
    return Check(
        "delivery",
        "Delivery",
        "pending",
        "No workflow registered. The reconciler still picks everything up on its next pass, "
        "so nothing is lost -- it just is not immediate.",
        "/wizard",
        "Register the workflow",
    )


def _backfill_check(services: Services) -> Check:
    page = services.cursors.get(BACKFILL_CURSOR)
    if page is not None:
        return Check("backfill", "Backfill", "pending", f"Running, resuming from page {page}.")
    counts = services.assets.counts_by_state()
    waiting = counts.get(AssetState.PENDING.value, 0) + counts.get(AssetState.UPLOADING.value, 0)
    if waiting:
        # Queued work is normal on the event-driven path -- flagging it would
        # leave the panel permanently red on a healthy system.
        return Check("backfill", "Backfill", "pending", f"{waiting} assets queued.")
    return Check("backfill", "Backfill", "ok", "Nothing waiting to transfer.")


def _transfer_check(services: Services) -> Check:
    try:
        reason = getattr(services.runtime, "paused_reason", None)
    except Exception as exc:  # readiness is polled; it must never 500
        return Check(
            "transfer",
            "Transfer",
            "attention",
            f"Could not read the runtime: {exc}",
            "/diagnostics",
            "Open diagnostics",
        )
    if reason:
        return Check(
            "transfer", "Transfer", "attention", f"Paused: {reason}", "/diagnostics", "Open diagnostics"
        )
    return Check("transfer", "Transfer", "ok", "Running.")


def _failures_check(services: Services) -> Check:
    try:
        failed = services.assets.counts_by_state().get(AssetState.FAILED.value, 0)
    except Exception as exc:  # same reason as _transfer_check
        return Check("failures", "Failures", "attention", f"Could not read asset counts: {exc}")
    if failed:
        noun = "asset" if failed == 1 else "assets"
        return Check(
            "failures",
            "Failures",
            "attention",
            f"{failed} {noun} exhausted their retries.",
            "/failures",
            "Review failures",
        )
    return Check("failures", "Failures", "ok", "No assets have exhausted their retries.")


def evaluate_readiness(services: Services) -> Readiness:
    checks = (
        _immich_check(services),
        _google_check(services),
        _delivery_check(services),
        _backfill_check(services),
        _transfer_check(services),
        _failures_check(services),
    )
    by_id = {c.id: c for c in checks}
    # Setup outranks everything: on an unconfigured install the actionable
    # thing is the wizard, not whatever the idle runtime happens to report.
    if any(by_id[name].state != "ok" for name in SETUP_CHECKS):
        overall: Overall = "needs_setup"
    elif any(c.state == "attention" for c in checks):
        overall = "attention"
    else:
        overall = "ready"
    return Readiness(overall=overall, checks=checks)


@router.get("/readiness")
def readiness(request: Request) -> dict:
    return evaluate_readiness(request.app.state.services).as_dict()
