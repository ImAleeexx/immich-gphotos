import pytest
from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app
from immich_gphotos.api.hooks import asset_from_webhook
from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.models import Priority
from immich_gphotos.services import Services
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo

RAW = bytes.fromhex("aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d")
B64 = "qvTGHdzF6KLavt4PO0gs2a6pQ00="


def payload(checksum) -> dict:
    return {
        "type": "AssetV1",
        "trigger": "AssetCreate",
        "data": {
            "asset": {
                "id": "a1",
                "checksum": checksum,
                "originalFileName": "IMG_1.JPG",
                "type": "IMAGE",
                "updatedAt": "2026-09-17T10:00:00.000Z",
                "originalPath": "/data/upload/a1.jpg",
                "visibility": "timeline",
                "isOffline": False,
                "tags": [{"name": "holiday"}],
                "exifInfo": {"fileSizeInByte": 4096},
            }
        },
    }


@pytest.fixture
def client(tmp_path):
    conn = connect(tmp_path / "t.db")
    clock = FakeClock()
    assets = AssetRepo(conn, clock)
    services = Services(
        assets=assets,
        albums=AlbumRepo(conn),
        cursors=CursorRepo(conn),
        settings_repo=SettingRepo(conn),
        events=EventRepo(conn, clock),
        runtime=None,
        settings=Settings(),
        webhook_secret="s3cret",
        webhook_header="X-IGP-Secret",
    )
    return TestClient(create_app(services)), assets


def test_asset_is_parsed_from_a_base64_checksum():
    asset = asset_from_webhook(payload(B64))
    assert asset.immich_id == "a1"
    assert asset.checksum == B64
    assert asset.filename == "IMG_1.JPG"
    assert asset.size_bytes == 4096
    assert asset.tags == ("holiday",)
    assert asset.is_trashed is False


def test_asset_is_parsed_from_a_buffer_shaped_checksum():
    """The Wasm runtime types checksum as Buffer; its JSON form is unverified."""
    asset = asset_from_webhook(payload({"type": "Buffer", "data": list(RAW)}))
    assert asset.checksum == B64


def test_valid_webhook_enqueues_at_webhook_priority(client):
    http, assets = client
    response = http.post("/hooks/immich", json=payload(B64), headers={"X-IGP-Secret": "s3cret"})
    assert response.status_code == 200
    stored = assets.get("a1")
    assert stored.priority is Priority.WEBHOOK


def test_wrong_secret_is_rejected_and_enqueues_nothing(client):
    http, assets = client
    response = http.post("/hooks/immich", json=payload(B64), headers={"X-IGP-Secret": "wrong"})
    assert response.status_code == 401
    assert assets.get("a1") is None


def test_missing_secret_is_rejected(client):
    http, _ = client
    assert http.post("/hooks/immich", json=payload(B64)).status_code == 401


def test_unparseable_payload_returns_400_without_raising(client):
    http, _ = client
    response = http.post("/hooks/immich", json={"nope": True}, headers={"X-IGP-Secret": "s3cret"})
    assert response.status_code == 400


def test_bad_checksum_returns_400(client):
    http, _ = client
    response = http.post("/hooks/immich", json=payload("not-a-hash"), headers={"X-IGP-Secret": "s3cret"})
    assert response.status_code == 400
