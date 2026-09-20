from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from immich_gphotos.api.auth import requires_setup
from immich_gphotos.storage_keys import ACCOUNT_COOKIE

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "web" / "templates"))


def _render(name: str, request: Request, **context):
    return TEMPLATES.TemplateResponse(request, name, context)


def render_not_found(request: Request):
    return TEMPLATES.TemplateResponse(request, "404.html", {}, status_code=404)


@router.get("/login")
def login_page(request: Request):
    return _render("login.html", request, first_run=requires_setup(request.app.state.accounts))


@router.get("/")
def dashboard(request: Request):
    return _render("dashboard.html", request)


@router.get("/failures")
def failures_page(request: Request):
    return _render("failures.html", request)


@router.get("/settings")
def settings_page(request: Request):
    return _render("settings.html", request)


@router.get("/diagnostics")
def diagnostics_page(request: Request):
    services = request.state.services
    # Immich's own account of what the workflow did, so a broken webhook path is
    # diagnosable from here rather than only from our silence.
    workflow_logs = []
    if services.immich is not None and services.workflow_id:
        try:
            workflow_logs = services.immich.workflow_logs(services.workflow_id)
        except Exception as exc:  # diagnostics must never 500
            services.events.add("warn", f"could not read workflow logs: {exc}")
    return _render(
        "diagnostics.html",
        request,
        events=services.events.recent(100),
        workflow_logs=workflow_logs,
    )


@router.get("/wizard")
def wizard_page(request: Request):
    return _render("wizard.html", request)


@router.post("/accounts/select")
def select_account(request: Request, account_id: str = Form(...)):
    """Switch which account this browser is looking at.

    A plain form post -- not a JSON API call -- because it exists to be a
    same-origin `<form>` submit from the account-switcher UI, the same shape
    as `/login` and `/logout`. The id is validated against the registry
    before the cookie is ever set: an account-switcher link is just as
    capable of going stale (the account was removed in another tab) as the
    cookie itself is (see `resolve_account`'s fallback for that case), and a
    cookie pointed at a dead id would be indistinguishable from one that had
    simply never been set -- silently falling back to the default account
    instead of surfacing the stale link as an error.
    """
    registry = request.app.state.accounts
    if registry.get(account_id) is None:
        raise HTTPException(status_code=404, detail="no such account")
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(ACCOUNT_COOKIE, account_id, httponly=True, samesite="lax")
    return response
