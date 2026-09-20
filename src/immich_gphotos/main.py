import os
from pathlib import Path

from immich_gphotos.accounts.registry import AccountRegistry
from immich_gphotos.api.app import create_app


def main() -> None:
    import uvicorn

    data_dir = Path(os.environ.get("IGP_DATA_DIR", "/data"))
    registry = AccountRegistry(data_dir)
    registry.start_all()
    try:
        uvicorn.run(
            create_app(registry),
            host=os.environ.get("IGP_HOST", "0.0.0.0"),  # noqa: S104 - it is a container
            port=int(os.environ.get("IGP_PORT", "8080")),
            log_config=None,
        )
    finally:
        registry.stop_all()
