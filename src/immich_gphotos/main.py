import os
from collections.abc import Mapping
from pathlib import Path

from immich_gphotos.accounts.build import build_account_services
from immich_gphotos.accounts.registry import AccountRegistry
from immich_gphotos.api.app import create_app
from immich_gphotos.services import Services
from immich_gphotos.sync.loops import LoopsHandle


def build_services(data_dir: Path, env: Mapping[str, str] | None = None) -> tuple[Services, LoopsHandle]:
    """One account's graph, built from the directory its database lives in.

    Kept as the single-account entry point: `AccountRegistry` calls
    `build_account_services` directly, once per account.
    """
    return build_account_services(Path(data_dir), env=env)


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
