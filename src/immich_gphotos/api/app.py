from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from immich_gphotos.api import auth, hooks, pages, routes, stream
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
        if not expected or supplied != expected:
            if request.url.path.startswith("/api"):
                return JSONResponse({"detail": "unauthenticated"}, status_code=401)
            return RedirectResponse("/login", status_code=307)
        return await call_next(request)

    app.include_router(hooks.router)
    app.include_router(auth.router)
    app.include_router(routes.router)
    app.include_router(stream.router)
    app.include_router(pages.router)
    return app
