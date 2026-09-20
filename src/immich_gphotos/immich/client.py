import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from immich_gphotos.checksum import normalize_checksum
from immich_gphotos.immich.protocol import AssetPage, ImmichAlbum, ImmichAuthError, ImmichError
from immich_gphotos.models import Asset

WEBHOOK_METHOD = "immich-plugin-core#webhook"

# Page size for the album-membership search. Matches `search_assets`'s own
# default: the reconciler already walks the whole library at this size, so an
# album -- a subset of it -- costs no more per request.
ALBUM_PAGE_SIZE = 1000


def _to_asset(item: dict[str, Any]) -> Asset:
    exif = item.get("exifInfo") or {}
    return Asset(
        immich_id=item["id"],
        checksum=normalize_checksum(item["checksum"]),
        filename=item.get("originalFileName") or item["id"],
        type=item.get("type", "IMAGE"),
        size_bytes=exif.get("fileSizeInByte"),
        immich_updated_at=item.get("updatedAt", ""),
        original_path=item.get("originalPath"),
        visibility=item.get("visibility", "timeline"),
        is_offline=bool(item.get("isOffline", False)),
        is_trashed=bool(item.get("isTrashed", False)),
        tags=tuple(t["name"] for t in item.get("tags") or [] if "name" in t),
    )


class HttpImmichClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 60.0) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/api",
            headers={"x-api-key": api_key, "Accept": "application/json"},
            timeout=timeout,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ImmichError(f"request to {path} failed: {exc}") from exc
        if response.status_code in (401, 403):
            raise ImmichAuthError(
                f"Immich rejected the API key ({response.status_code})", response.status_code
            )
        if response.status_code >= 400:
            raise ImmichError(f"{method} {path} returned {response.status_code}", response.status_code)
        return response

    def server_version(self) -> tuple[int, int, int]:
        data = self._request("GET", "/server/version").json()
        return data["major"], data["minor"], data["patch"]

    def key_permissions(self) -> set[str]:
        return set(self._request("GET", "/api-keys/me").json().get("permissions", []))

    def plugin_method_keys(self) -> set[str]:
        data = self._request("GET", "/plugins/methods").json()
        return {m["key"] for m in data if "key" in m}

    def search_assets(
        self,
        *,
        updated_after: datetime | None,
        page: int = 1,
        size: int = 1000,
        with_deleted: bool = False,
    ) -> AssetPage:
        # withExif is on because the size filter needs fileSizeInByte, which is
        # only populated when EXIF is joined.
        body: dict[str, Any] = {"page": page, "size": size, "withExif": True}
        if updated_after is not None:
            body["updatedAfter"] = updated_after.isoformat()
        if with_deleted:
            body["withDeleted"] = True
        data = self._request("POST", "/search/metadata", json=body).json()["assets"]
        next_page = data.get("nextPage")
        return AssetPage(
            assets=[_to_asset(i) for i in data.get("items", [])],
            next_page=int(next_page) if next_page else None,
        )

    def download_original(self, asset_id: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Stream to a sibling temp file and atomically replace dest only once the
        # whole body has landed. A partial file at dest would be indistinguishable
        # from a complete one to anything that later just checks existence, and
        # since uploads are keyed on Immich's pre-computed checksum rather than a
        # hash of the bytes we send, a truncated download becomes a corrupt upload
        # recorded as "already present" forever.
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            with self._client.stream("GET", f"/assets/{asset_id}/original") as response:
                if response.status_code in (401, 403):
                    raise ImmichAuthError("Immich rejected the API key", response.status_code)
                if response.status_code >= 400:
                    raise ImmichError(
                        f"download of {asset_id} returned {response.status_code}", response.status_code
                    )
                with tmp.open("wb") as handle:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        handle.write(chunk)
            os.replace(tmp, dest)
        except httpx.HTTPError as exc:
            tmp.unlink(missing_ok=True)
            raise ImmichError(f"download of {asset_id} failed: {exc}") from exc
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    def list_albums(self) -> list[ImmichAlbum]:
        return [
            ImmichAlbum(id=a["id"], name=a["albumName"], asset_count=a.get("assetCount", 0))
            for a in self._request("GET", "/albums").json()
        ]

    def album_asset_ids(self, album_id: str) -> list[str]:
        """Every non-trashed asset in an Immich album.

        Read through `/search/metadata`, not `GET /albums/{id}`. Immich's
        `mapAlbum` does not put an `assets` array on the album detail
        response -- it carries `assetCount` and nothing else -- so the
        previous `data["assets"]` read resolved to `[]` for every album on
        every server. Both callers fail silently on that: `AlbumMirror`
        mirrored nothing to Google, and a `filters.album_allowlist` excluded
        every asset, because an album that returns no ids is indistinguishable
        from a legitimately empty one. No error was raised in either case.

        `albumIds` is deprecated as of Immich 3.2.0 in favour of the new
        `filter` object, but it is still served and is the only spelling that
        also works on the v2 servers this project still supports (see
        `setup.wizard`, which only requires >= 3.0 for workflows).
        """
        ids: list[str] = []
        page: int | None = 1
        while page is not None:
            body = {"albumIds": [album_id], "page": page, "size": ALBUM_PAGE_SIZE}
            data = self._request("POST", "/search/metadata", json=body).json()["assets"]
            ids.extend(item["id"] for item in data.get("items", []) if "id" in item)
            raw_next = data.get("nextPage")
            next_page = int(raw_next) if raw_next else None
            # Only ever move forward: a server that echoes the page it was
            # given back as `nextPage` would otherwise spin this loop forever
            # inside a background tick, with nothing to show for it.
            page = next_page if next_page is not None and next_page > page else None
        return ids

    def create_workflow(self, *, name: str, url: str, header_name: str, header_value: str) -> str:
        body = {
            "name": name,
            "trigger": "AssetCreate",
            "enabled": True,
            "logging": True,
            "steps": [
                {
                    "method": WEBHOOK_METHOD,
                    "enabled": True,
                    "config": {
                        "url": url,
                        "method": "POST",
                        "headerName": header_name,
                        "headerValue": header_value,
                    },
                }
            ],
        }
        return self._request("POST", "/workflows", json=body).json()["id"]

    def workflow_logs(self, workflow_id: str) -> list[dict]:
        return self._request("GET", f"/workflows/{workflow_id}/logs").json()

    def delete_workflow(self, workflow_id: str) -> None:
        self._request("DELETE", f"/workflows/{workflow_id}")
