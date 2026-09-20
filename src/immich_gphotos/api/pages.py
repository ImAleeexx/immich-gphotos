from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from immich_gphotos.api.auth import requires_setup
from immich_gphotos.api.routes import list_accounts
from immich_gphotos.storage_keys import ACCOUNT_COOKIE

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "web" / "templates"))


def _render(name: str, request: Request, **context):
    """Render a template with the account context every page needs, so no
    route has to opt in individually.

    `current_account` comes from `request.state.account`, set by
    `require_session`'s middleware for every authenticated request -- except
    `/login` itself, which is exempted from that middleware entirely
    (Ruling R2), so `request.state` never gets an `account` attribute there
    at all. `getattr(..., None)` covers that case rather than assuming the
    attribute exists; `login.html` sets `show_chrome = false` so the
    switcher never actually reads it, but `_render` must not crash getting
    there regardless.

    `accounts` defaults to every account in the registry (`Account`
    instances) -- enough for the switcher in `base.html`, which only ever
    reads `.id`/`.label`. A route that needs richer per-account fields (the
    `/accounts` page's state pill and synced count) passes its own
    `accounts=` kwarg, which this `setdefault` leaves alone.
    """
    context.setdefault("current_account", getattr(request.state, "account", None))
    context.setdefault("accounts", request.app.state.accounts.all())
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


@router.get("/accounts")
def accounts_page(request: Request):
    """Ruling R5: this closes the interim gap opened back in Task 4 -- a
    zero-account install has 307ed browsers to `/accounts` since the
    account-required gate landed, and this is the first commit where that
    path actually resolves to something instead of a 404.

    `accounts=list_accounts(request)` deliberately overrides `_render`'s
    default (`registry.all()`, plain `Account` objects) with the same
    per-account dicts `GET /api/accounts` reports -- `immich_url`, `state`,
    `synced`, `paused_reason` -- so this page's table and the API never
    drift on what "state" means for an account. A dict answers `account.id`/
    `account.label` in Jinja exactly the way an `Account` object does (attr
    lookup falls back to `__getitem__`), so the base.html switcher, which
    also reads this same `accounts` context var, keeps working unchanged.
    """
    return _render("accounts.html", request, nav_active="accounts", accounts=list_accounts(request))


@router.post("/accounts/add")
def add_account(request: Request, label: str = Form(...)):
    """RULING R13: creating an account from the UI and selecting it are two
    different concerns living in two different route styles on purpose.

    `POST /api/accounts` (Task 8) stays JSON-only and never sets the
    `igp_account` cookie -- a JSON 200 has nothing that can redirect a
    browser, and nothing about that route should assume its caller is a
    browser at all. But the admin's "add an account" flow *is* a plain
    `<form>` (no JavaScript, per this task's hard constraint), and a plain
    form issues exactly one POST -- so create-and-select-and-land-in-the-
    wizard has to happen in that one request, which means it cannot go
    through the JSON API. This route is that one request: it creates the
    account, points the cookie at it, and 303s into the wizard so the admin
    lands configuring the account they just made rather than the one the
    switcher happened to be on before. Mirrors `/login`, `/logout` and
    `/accounts/select`, which already split the same way.

    The blank-label rejection mirrors `AccountCreate`'s validator in
    `routes.py` (a whitespace-only label would round-trip as an unlabelled
    account forever) without importing that model: it is a FastAPI request
    body parser, and raising its `ValidationError` from inside a plain
    function body would surface as an unhandled 500 here, not the clean 422
    a stray-whitespace label deserves.
    """
    if not label.strip():
        raise HTTPException(status_code=422, detail="label must not be blank")
    registry = request.app.state.accounts
    account = registry.create(label)
    response = RedirectResponse("/wizard", status_code=303)
    response.set_cookie(ACCOUNT_COOKIE, account.id, httponly=True, samesite="lax")
    return response


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
