from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from immich_gphotos.models import Asset


class ImmichError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ImmichAuthError(ImmichError):
    """The API key was rejected."""


@dataclass(frozen=True)
class AssetPage:
    assets: list[Asset]
    next_page: int | None


@dataclass(frozen=True)
class ImmichAlbum:
    id: str
    name: str
    asset_count: int


class ImmichClient(Protocol):
    def server_version(self) -> tuple[int, int, int]: ...
    def key_permissions(self) -> set[str]: ...
    def plugin_method_keys(self) -> set[str]: ...
    def search_assets(
        self,
        *,
        updated_after: datetime | None,
        page: int = 1,
        size: int = 1000,
        with_deleted: bool = False,
    ) -> AssetPage: ...
    def download_original(self, asset_id: str, dest: Path) -> None: ...
    def list_albums(self) -> list[ImmichAlbum]: ...
    def album_asset_ids(self, album_id: str) -> list[str]: ...
    def create_workflow(self, *, name: str, url: str, header_name: str, header_value: str) -> str: ...
    def workflow_logs(self, workflow_id: str) -> list[dict]: ...
    def delete_workflow(self, workflow_id: str) -> None: ...
