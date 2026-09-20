"""Routes that drive the four-screen setup wizard described in the spec.

Each step validates against the real service before persisting anything
(`Wizard.check_immich` / `Wizard.check_google`), stores credentials only on
success, and never echoes a credential back in a response body. On success,
the credential is (1) persisted under the exact key `main.py` reads, (2)
registered with the shared `Redactor` so it is scrubbed from logs and events
from this point on, and (3) used to rebuild the live runtime graph
(`composition.rebuild_runtime`) so the change takes effect immediately,
without a restart.

These routes sit behind the session-auth middleware in `api/app.py` like
everything else under `/api` -- they are deliberately not in
`auth.OPEN_PATHS`.
"""

from dataclasses import replace

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from immich_gphotos.api.routes import SETTING_KEY as CONFIG_SETTING_KEY
from immich_gphotos.api.routes import SettingsPatch, require_deletion_confirmation, resolve_settings_updates
from immich_gphotos.composition import rebuild_runtime
from immich_gphotos.gphotos.client import GpmcClient
from immich_gphotos.immich.client import HttpImmichClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.immich.protocol import ImmichError
from immich_gphotos.logging import Redactor
from immich_gphotos.services import Services
from immich_gphotos.storage_keys import GOOGLE_AUTH_KEY, IMMICH_KEY_KEY, IMMICH_URL_KEY, WORKFLOW_ID_KEY

router = APIRouter(prefix="/api/wizard")


def _mode(event_driven: bool) -> str:
    return "event_driven" if event_driven else "reconciler_only"


class ImmichRequest(BaseModel):
    immich_url: str
    immich_api_key: str


class GoogleRequest(BaseModel):
    google_auth_data: str


class WorkflowRequest(BaseModel):
    webhook_url: str


class WizardOptions(SettingsPatch):
    start_backfill: bool = False


@router.get("/status")
def wizard_status(request: Request) -> dict:
    """What's already configured, so the page can skip finished steps and
    report the detected mode -- never a credential, only booleans/ids."""
    services: Services = request.state.services
    immich_configured = bool(
        services.settings_repo.get(IMMICH_URL_KEY) and services.settings_repo.get(IMMICH_KEY_KEY)
    )
    google_configured = bool(services.settings_repo.get(GOOGLE_AUTH_KEY))
    workflow_id = services.settings_repo.get(WORKFLOW_ID_KEY)

    mode = None
    immich_is_real = services.immich is not None and not isinstance(services.immich, FakeImmichClient)
    if immich_configured and immich_is_real:
        try:
            check = services.wizard.check_immich(services.immich)
            mode = _mode(check.event_driven)
        except Exception:  # noqa: BLE001 - status must never 500 over a flaky server
            mode = None

    return {
        "immich_configured": immich_configured,
        "google_configured": google_configured,
        "workflow_id": workflow_id,
        "immich_url": services.settings_repo.get(IMMICH_URL_KEY) or "",
        "mode": mode,
    }


@router.post("/immich")
def wizard_immich(payload: ImmichRequest, request: Request) -> dict:
    services: Services = request.state.services
    url = payload.immich_url.strip()
    key = payload.immich_api_key.strip()
    if not url or not key:
        raise HTTPException(status_code=422, detail="both the server URL and API key are required")

    client = HttpImmichClient(url, key)
    check = services.wizard.check_immich(client)
    if not check.ok:
        client.close()
        # Belt and braces: check.message is ours and never contains the key,
        # but scrub anyway in case a future ImmichError ever echoes it.
        message = Redactor([key]).scrub(check.message)
        raise HTTPException(
            status_code=422,
            detail={"message": message, "missing_permissions": sorted(check.missing_permissions)},
        )

    services.settings_repo.set(IMMICH_URL_KEY, url)
    services.settings_repo.set(IMMICH_KEY_KEY, key)
    if services.redactor is not None:
        services.redactor.add_secret(key)

    new_settings = replace(services.settings, immich_url=url)
    rebuild_runtime(services, immich=client, settings=new_settings)
    services.events.add("info", "setup wizard: immich connected")

    return {
        "ok": True,
        "version": list(check.version) if check.version else None,
        "event_driven": check.event_driven,
        "webhook_method_present": check.webhook_method_present,
        "mode": _mode(check.event_driven),
        "message": check.message,
    }


@router.post("/google")
def wizard_google(payload: GoogleRequest, request: Request) -> dict:
    services: Services = request.state.services
    auth_data = payload.google_auth_data.strip()
    if not auth_data:
        raise HTTPException(status_code=422, detail="auth_data is required")

    client = GpmcClient(auth_data, quality=services.settings.quality)
    check = services.wizard.check_google(client)
    if not check.ok:
        message = Redactor([auth_data]).scrub(check.message)
        raise HTTPException(status_code=422, detail=message)

    services.settings_repo.set(GOOGLE_AUTH_KEY, auth_data)
    if services.redactor is not None:
        services.redactor.add_secret(auth_data)

    rebuild_runtime(services, gphotos=client)
    services.events.add("info", "setup wizard: google photos connected")
    return {"ok": True, "message": check.message}


@router.post("/workflow")
def wizard_workflow(payload: WorkflowRequest, request: Request) -> dict:
    services: Services = request.state.services
    if not (services.settings_repo.get(IMMICH_URL_KEY) and services.settings_repo.get(IMMICH_KEY_KEY)):
        raise HTTPException(status_code=400, detail="connect Immich before registering the workflow")
    if services.immich is None or isinstance(services.immich, FakeImmichClient):
        raise HTTPException(status_code=400, detail="connect Immich before registering the workflow")

    webhook_url = payload.webhook_url.strip()
    if not webhook_url:
        raise HTTPException(status_code=422, detail="a webhook URL is required")

    # Reuse the secret build_account_services already generated and persisted
    # at boot (itself produced by Wizard.generate_secret) -- never mint a second one,
    # or the receiver at /hooks/immich would check against a secret the
    # workflow was never told about.
    try:
        workflow_id = services.wizard.register_workflow(
            services.immich,
            public_url=webhook_url,
            secret=services.webhook_secret,
            header=services.webhook_header,
        )
    except ImmichError as exc:
        # `immich/client.py` only ever builds `ImmichError` messages from the
        # method, path and status code -- never a header or body -- so there
        # is nothing secret in `exc` today. Still route it through the same
        # `Redactor` every other wizard error path uses, rather than being
        # the one place that assumes that invariant holds forever.
        message = services.redactor.scrub(str(exc)) if services.redactor is not None else str(exc)
        raise HTTPException(status_code=422, detail=f"workflow registration failed: {message}") from exc

    services.settings_repo.set(WORKFLOW_ID_KEY, workflow_id)
    services.workflow_id = workflow_id
    services.events.add("info", "setup wizard: workflow registered")
    return {"ok": True, "workflow_id": workflow_id}


@router.post("/options")
def wizard_options(payload: WizardOptions, request: Request) -> dict:
    services: Services = request.state.services
    require_deletion_confirmation(payload, currently_enabled=services.settings.deletions_enabled)
    updates = resolve_settings_updates(payload, exclude={"start_backfill", "confirm_deletions"})

    if updates:
        stored = dict(services.settings_repo.get(CONFIG_SETTING_KEY) or {})
        stored.update(updates)
        services.settings_repo.set(CONFIG_SETTING_KEY, stored)
        rebuild_runtime(services, settings=replace(services.settings, **updates))

    if payload.start_backfill:
        services.backfill.start()

    services.events.add("info", "setup wizard: options saved")
    return {"ok": True, "backfill_started": bool(payload.start_backfill)}
