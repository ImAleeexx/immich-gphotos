from datetime import UTC, datetime

import httpx
import pytest
import respx

from immich_gphotos.immich.client import HttpImmichClient
from immich_gphotos.immich.protocol import ImmichAuthError

BASE = "https://immich.test"


def client() -> HttpImmichClient:
    return HttpImmichClient(base_url=BASE, api_key="k3y")


@respx.mock
def test_server_version_is_parsed():
    respx.get(f"{BASE}/api/server/version").mock(
        return_value=httpx.Response(200, json={"major": 3, "minor": 2, "patch": 2, "prerelease": 0})
    )
    assert client().server_version() == (3, 2, 2)


@respx.mock
def test_api_key_header_is_sent():
    route = respx.get(f"{BASE}/api/server/version").mock(
        return_value=httpx.Response(200, json={"major": 3, "minor": 0, "patch": 0, "prerelease": 0})
    )
    client().server_version()
    assert route.calls.last.request.headers["x-api-key"] == "k3y"


@respx.mock
def test_key_permissions():
    respx.get(f"{BASE}/api/api-keys/me").mock(
        return_value=httpx.Response(200, json={"id": "1", "name": "k", "permissions": ["asset.read"]})
    )
    assert client().key_permissions() == {"asset.read"}


@respx.mock
def test_plugin_method_keys():
    respx.get(f"{BASE}/api/plugins/methods").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"key": "immich-plugin-core#webhook", "name": "webhook", "title": "Trigger Webhook"},
                {"key": "immich-plugin-core#assetArchive", "name": "assetArchive", "title": "Archive"},
            ],
        )
    )
    assert "immich-plugin-core#webhook" in client().plugin_method_keys()


@respx.mock
def test_search_assets_maps_items_and_next_page():
    respx.post(f"{BASE}/api/search/metadata").mock(
        return_value=httpx.Response(
            200,
            json={
                "albums": {"items": [], "total": 0, "count": 0, "facets": [], "nextPage": None},
                "assets": {
                    "total": 2,
                    "count": 1,
                    "facets": [],
                    "nextCursor": None,
                    "nextPage": "2",
                    "items": [
                        {
                            "id": "a1",
                            "checksum": "qvTGHdzF6KLavt4PO0gs2a6pQ00=",
                            "originalFileName": "IMG_0001.JPG",
                            "type": "IMAGE",
                            "updatedAt": "2026-09-17T10:00:00.000Z",
                            "originalPath": "/data/upload/a1.jpg",
                            "visibility": "timeline",
                            "isOffline": False,
                            "isTrashed": False,
                            "tags": [{"name": "holiday"}],
                            "exifInfo": {"fileSizeInByte": 2048},
                        }
                    ],
                },
            },
        )
    )
    page = client().search_assets(updated_after=datetime(2026, 9, 17, tzinfo=UTC))
    assert page.next_page == 2
    asset = page.assets[0]
    assert (asset.immich_id, asset.filename, asset.size_bytes) == ("a1", "IMG_0001.JPG", 2048)
    assert asset.tags == ("holiday",)


@respx.mock
def test_search_sends_updated_after_and_paging():
    route = respx.post(f"{BASE}/api/search/metadata").mock(
        return_value=httpx.Response(
            200,
            json={
                "albums": {"items": [], "total": 0, "count": 0, "facets": [], "nextPage": None},
                "assets": {"items": [], "total": 0, "count": 0, "facets": [], "nextCursor": None, "nextPage": None},
            },
        )
    )
    client().search_assets(updated_after=datetime(2026, 9, 17, 9, 0, tzinfo=UTC), page=3, size=500)
    body = route.calls.last.request.read().decode()
    assert '"page": 3' in body or '"page":3' in body
    assert "2026-09-17T09:00:00" in body


@respx.mock
def test_401_raises_auth_error():
    respx.get(f"{BASE}/api/server/version").mock(return_value=httpx.Response(401, json={}))
    with pytest.raises(ImmichAuthError):
        client().server_version()


@respx.mock
def test_download_original_writes_the_file(tmp_path):
    respx.get(f"{BASE}/api/assets/a1/original").mock(
        return_value=httpx.Response(200, content=b"JPEGBYTES")
    )
    dest = tmp_path / "a1.jpg"
    client().download_original("a1", dest)
    assert dest.read_bytes() == b"JPEGBYTES"


@respx.mock
def test_create_workflow_posts_the_webhook_step_and_returns_id():
    route = respx.post(f"{BASE}/api/workflows").mock(
        return_value=httpx.Response(201, json={"id": "wf-1", "name": "n", "trigger": "AssetCreate",
                                               "steps": [], "enabled": True, "logging": True,
                                               "description": "", "createdAt": "", "updatedAt": ""})
    )
    workflow_id = client().create_workflow(
        name="Back up to Google Photos",
        url="http://immich-gphotos:8080/hooks/immich",
        header_name="X-IGP-Secret",
        header_value="s3cret",
    )
    assert workflow_id == "wf-1"
    body = route.calls.last.request.read().decode()
    assert "AssetCreate" in body
    assert "immich-plugin-core#webhook" in body
