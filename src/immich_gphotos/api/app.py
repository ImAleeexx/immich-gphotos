from fastapi import FastAPI

from immich_gphotos.api import hooks
from immich_gphotos.services import Services


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="immich-gphotos", docs_url=None, redoc_url=None)
    app.state.services = services
    app.include_router(hooks.router)
    return app
