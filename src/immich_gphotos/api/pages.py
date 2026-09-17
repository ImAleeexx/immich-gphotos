from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.templating import Jinja2Templates

from immich_gphotos.api.auth import requires_setup

router = APIRouter()
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent.parent / "web" / "templates"))


def _render(name: str, request: Request, **context):
    return TEMPLATES.TemplateResponse(request, name, context)


def render_not_found(request: Request):
    return TEMPLATES.TemplateResponse(request, "404.html", {}, status_code=404)


@router.get("/login")
def login_page(request: Request):
    return _render("login.html", request, first_run=requires_setup(request.app.state.services))


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
    services = request.app.state.services
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
