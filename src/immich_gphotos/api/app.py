import hmac
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from immich_gphotos.api import auth, hooks, ops, pages, readiness, routes, stream, wizard
from immich_gphotos.services import Services

STATIC_DIR = Path(__file__).parent.parent / "web" / "static"


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="immich-gphotos", docs_url=None, redoc_url=None)
    app.state.services = services

    @app.exception_handler(RequestValidationError)
    async def strip_input_from_validation_errors(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """FastAPI's default handler for a pydantic validation failure echoes
        the submitted value back verbatim in each error's `input` field. For a
        "missing field" error, pydantic v2 sets `input` to the *entire*
        request body -- so a wizard request with a misnamed field (e.g.
        `google_auth_data` sent as something else) returns a 422 whose body
        contains the real Google/Immich credential the caller just submitted,
        in a response body that anything downstream (proxies, logs, browser
        devtools) may capture. `immich_api_key` and `google_auth_data` are
        credentials pulled from a live server / an Android device over ADB --
        not secrets a user can casually rotate.

        Rather than auditing every current and future route that accepts a
        credential for a safe request model, strip `input` from every
        validation error application-wide: it is diagnostically useful to a
        developer but never needs to reach an HTTP response, and no route
        should ever depend on the client receiving its own submitted value
        back.
        """
        sanitized = [{k: v for k, v in error.items() if k != "input"} for error in exc.errors()]
        return JSONResponse(status_code=422, content=jsonable_encoder({"detail": sanitized}))

    @app.exception_handler(404)
    async def render_not_found(request: Request, exc: HTTPException) -> Response:
        """Starlette dispatches an HTTPException by status code before it
        ever looks at the exception type, so this handler sees every
        HTTPException(404) raised anywhere in the app -- not just the
        "no route matched" case. A route that deliberately raises its own
        404 with a specific detail (e.g. POST /api/failures/{id}/retry) must
        keep that detail; only Starlette's own unmatched-route 404, which
        always carries the default detail "Not Found", gets the branded
        HTML-vs-JSON treatment below.
        """
        if exc.detail != "Not Found":
            return JSONResponse({"detail": exc.detail}, status_code=404)
        if request.url.path.startswith("/api") or "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"detail": "not found"}, status_code=404)
        return pages.render_not_found(request)

    @app.middleware("http")
    async def require_session(request, call_next):
        if auth.is_open(request.url.path):
            return await call_next(request)
        expected = services.settings_repo.get(auth.SESSION_COOKIE)
        supplied = request.cookies.get(auth.SESSION_COOKIE)
        # A missing cookie or missing stored token must fail closed before
        # ever reaching compare_digest (it requires two real strings); once
        # both are present, the actual token comparison must be constant-time
        # — this is the same bearer token gating every authenticated route.
        # Starlette decodes cookies as latin-1, so a corrupted cookie value
        # with a high byte is a non-ASCII str; compare_digest raises TypeError
        # on that instead of returning False, so compare bytes, not str.
        if not expected or not supplied or not hmac.compare_digest(supplied.encode(), expected.encode()):
            if request.url.path.startswith("/api"):
                return JSONResponse({"detail": "unauthenticated"}, status_code=401)
            return RedirectResponse("/login", status_code=307)
        return await call_next(request)

    # Resolved relative to __file__ for the same reason api/pages.py resolves
    # the template directory that way: the package is installed into
    # site-packages, so a path relative to the working directory does not
    # survive `pip install .`.
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.include_router(hooks.router)
    app.include_router(ops.router)
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(readiness.router)
    app.include_router(wizard.router)
    app.include_router(stream.router)
    app.include_router(pages.router)
    return app
