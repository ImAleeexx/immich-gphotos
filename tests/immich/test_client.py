from datetime import UTC, datetime

import httpx
import pytest
import respx

from immich_gphotos.immich.client import HttpImmichClient
from immich_gphotos.immich.protocol import ImmichAuthError, ImmichError

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
                            "fileCreatedAt": "2022-07-05T12:00:00.000Z",
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
    # `fileCreatedAt`, not `updatedAt`: this is what ends up as the uploaded
    # file's mtime and therefore as the Google Photos capture date.
    assert asset.taken_at == "2022-07-05T12:00:00.000Z"


@respx.mock
def test_search_sends_updated_after_and_paging():
    route = respx.post(f"{BASE}/api/search/metadata").mock(
        return_value=httpx.Response(
            200,
            json={
                "albums": {"items": [], "total": 0, "count": 0, "facets": [], "nextPage": None},
                "assets": {
                    "items": [],
                    "total": 0,
                    "count": 0,
                    "facets": [],
                    "nextCursor": None,
                    "nextPage": None,
                },
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
    respx.get(f"{BASE}/api/assets/a1/original").mock(return_value=httpx.Response(200, content=b"JPEGBYTES"))
    dest = tmp_path / "a1.jpg"
    client().download_original("a1", dest)
    assert dest.read_bytes() == b"JPEGBYTES"


@respx.mock
def test_download_original_failure_leaves_no_partial_file_at_dest(tmp_path):
    # Simulate a connection dropping partway through the body: the mocked
    # response starts streaming successfully (200) but the byte-iterator
    # raises mid-body, the same shape a real dropped TCP connection takes.
    def bad_stream():
        yield b"PART"
        raise httpx.ReadError("connection dropped mid-body")

    respx.get(f"{BASE}/api/assets/a1/original").mock(return_value=httpx.Response(200, content=bad_stream()))
    dest = tmp_path / "a1.jpg"
    with pytest.raises(ImmichError):
        client().download_original("a1", dest)
    assert not dest.exists()
    assert list(tmp_path.iterdir()) == []


@respx.mock
def test_download_original_network_error_raises_immich_error(tmp_path):
    respx.get(f"{BASE}/api/assets/a1/original").mock(side_effect=httpx.ConnectError("boom"))
    dest = tmp_path / "a1.jpg"
    with pytest.raises(ImmichError) as excinfo:
        client().download_original("a1", dest)
    assert not isinstance(excinfo.value, ImmichAuthError)
    assert not dest.exists()


@respx.mock
def test_create_workflow_posts_the_webhook_step_and_returns_id():
    route = respx.post(f"{BASE}/api/workflows").mock(
        return_value=httpx.Response(
            201,
            json={
                "id": "wf-1",
                "name": "n",
                "trigger": "AssetCreate",
                "steps": [],
                "enabled": True,
                "logging": True,
                "description": "",
                "createdAt": "",
                "updatedAt": "",
            },
        )
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


@respx.mock
def test_delete_workflow_deletes_by_id():
    route = respx.delete(f"{BASE}/api/workflows/wf-1").mock(return_value=httpx.Response(200, json={}))
    client().delete_workflow("wf-1")
    assert route.called


def _search_page(items: list[dict], next_page: str | None) -> dict:
    return {
        "albums": {"items": [], "total": 0, "count": 0, "facets": [], "nextPage": None},
        "assets": {
            "items": items,
            "total": len(items),
            "count": len(items),
            "facets": [],
            "nextCursor": None,
            "nextPage": next_page,
        },
    }


@respx.mock
def test_list_albums_maps_name_and_count():
    respx.get(f"{BASE}/api/albums").mock(
        return_value=httpx.Response(
            200,
            json=[{"id": "alb-1", "albumName": "Holiday", "assetCount": 2}],
        )
    )
    albums = client().list_albums()
    assert [(a.id, a.name, a.asset_count) for a in albums] == [("alb-1", "Holiday", 2)]


@respx.mock
def test_album_asset_ids_uses_metadata_search_not_the_album_detail_body():
    """Regression: `GET /albums/{id}` has never carried an `assets` array on
    Immich 3.x -- `mapAlbum` returns `assetCount` and nothing else -- so
    reading `data["assets"]` there silently yielded [] for every album and
    album mirroring never pushed anything to Google.
    """
    detail = respx.get(f"{BASE}/api/albums/alb-1").mock(
        return_value=httpx.Response(200, json={"id": "alb-1", "albumName": "Holiday", "assetCount": 2})
    )
    search = respx.post(f"{BASE}/api/search/metadata").mock(
        return_value=httpx.Response(200, json=_search_page([{"id": "a1"}, {"id": "a2"}], None))
    )

    assert client().album_asset_ids("alb-1") == ["a1", "a2"]

    assert not detail.called
    assert search.called
    body = search.calls.last.request.read().decode()
    assert "alb-1" in body
    assert "albumIds" in body


@respx.mock
def test_album_asset_ids_follows_every_page():
    pages = [
        httpx.Response(200, json=_search_page([{"id": "a1"}], "2")),
        httpx.Response(200, json=_search_page([{"id": "a2"}], None)),
    ]
    route = respx.post(f"{BASE}/api/search/metadata").mock(side_effect=pages)

    assert client().album_asset_ids("alb-1") == ["a1", "a2"]
    assert route.call_count == 2


@respx.mock
def test_asset_taken_at_reads_file_created_at():
    respx.get(f"{BASE}/api/assets/a1").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "a1",
                "fileCreatedAt": "2022-07-05T12:00:00.000Z",
                "updatedAt": "2026-09-20T14:58:08.683Z",
            },
        )
    )
    # `fileCreatedAt`, never `updatedAt`: one is when the photo was taken, the
    # other is when Immich last touched the row.
    assert client().asset_taken_at("a1") == "2022-07-05T12:00:00.000Z"


@respx.mock
def test_asset_taken_at_is_none_when_immich_has_no_date():
    respx.get(f"{BASE}/api/assets/a1").mock(return_value=httpx.Response(200, json={"id": "a1"}))
    assert client().asset_taken_at("a1") is None
