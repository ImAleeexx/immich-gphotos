from datetime import datetime
from pathlib import Path

from immich_gphotos.immich.protocol import AssetPage, ImmichAlbum
from immich_gphotos.models import Asset


class FakeImmichClient:
    """In-memory Immich. Lets the whole engine be tested with no network."""

    def __init__(
        self,
        assets: list[Asset] | None = None,
        contents: dict[str, bytes] | None = None,
        albums: dict[str, list[str]] | None = None,
        permissions: set[str] | None = None,
        version: tuple[int, int, int] = (3, 2, 2),
        method_keys: set[str] | None = None,
    ) -> None:
        self.assets = assets or []
        self.contents = contents or {}
        self.albums = albums or {}
        self.permissions = permissions or set()
        self.version = version
        self.method_keys = method_keys or {"immich-plugin-core#webhook"}
        self.downloads: list[str] = []
        self.created_workflows: list[dict] = []
        self.searches: list[dict] = []

    def server_version(self) -> tuple[int, int, int]:
        return self.version

    def key_permissions(self) -> set[str]:
        return set(self.permissions)

    def plugin_method_keys(self) -> set[str]:
        return set(self.method_keys)

    def search_assets(
        self,
        *,
        updated_after: datetime | None,
        page: int = 1,
        size: int = 1000,
        with_deleted: bool = False,
    ) -> AssetPage:
        self.searches.append(
            {"updated_after": updated_after, "page": page, "size": size, "with_deleted": with_deleted}
        )
        pool = [a for a in self.assets if with_deleted or not a.is_trashed]
        start = (page - 1) * size
        window = pool[start : start + size]
        has_more = len(pool) > start + size
        return AssetPage(assets=window, next_page=page + 1 if has_more else None)

    def download_original(self, asset_id: str, dest: Path) -> None:
        self.downloads.append(asset_id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.contents.get(asset_id, b"fake-bytes"))

    def list_albums(self) -> list[ImmichAlbum]:
        return [ImmichAlbum(id=k, name=f"Album {k}", asset_count=len(v)) for k, v in self.albums.items()]

    def album_asset_ids(self, album_id: str) -> list[str]:
        return list(self.albums.get(album_id, []))

    def create_workflow(self, *, name: str, url: str, header_name: str, header_value: str) -> str:
        self.created_workflows.append({"name": name, "url": url, "header_name": header_name})
        return f"wf-{len(self.created_workflows)}"

    def workflow_logs(self, workflow_id: str) -> list[dict]:
        return []
