import hmac

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from immich_gphotos.api import auth, hooks, ops, pages, routes, stream
from immich_gphotos.services import Services


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="immich-gphotos", docs_url=None, redoc_url=None)
    app.state.services = services

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

    app.include_router(hooks.router)
    app.include_router(ops.router)
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(stream.router)
    app.include_router(pages.router)
    return app
