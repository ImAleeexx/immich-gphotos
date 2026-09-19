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
    from immich_gphotos.accounts.registry import Account, AccountRegistry

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
    # /hooks/immich is an open path -- it resolves its own account straight
    # from the registry (Ruling R1) rather than reading request.state, so a
    # session/login is never needed here.
    registry = AccountRegistry(tmp_path / "registry", env={})
    record = registry.accounts_repo.add(
        account_id="acct-1", label="Default", created_at="2026-09-20T10:00:00Z"
    )
    registry.register(Account(record=record, services=services, loops=None))
    return TestClient(create_app(registry)), assets


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


def test_payload_missing_required_field_returns_400(client):
    http, _ = client
    response = http.post("/hooks/immich", json={"nope": True}, headers={"X-IGP-Secret": "s3cret"})
    assert response.status_code == 400


def test_non_json_body_returns_400_without_raising(client):
    """A genuinely unparseable body (not just a missing field) must not become a 500.

    Immich's webhook action never inspects the response and never retries, so a
    500 here is indistinguishable from success on its side and silently drops
    the asset.
    """
    http, _ = client
    response = http.post(
        "/hooks/immich",
        content=b"not json at all {{{",
        headers={"X-IGP-Secret": "s3cret"},
    )
    assert response.status_code == 400


def test_bad_checksum_returns_400(client):
    http, _ = client
    response = http.post("/hooks/immich", json=payload("not-a-hash"), headers={"X-IGP-Secret": "s3cret"})
    assert response.status_code == 400


def test_high_byte_secret_header_is_rejected_not_a_500(client):
    """Starlette decodes headers as latin-1, so a header byte >= 0x80 makes a
    non-ASCII str; hmac.compare_digest raises TypeError on that instead of
    just returning False. A malformed secret must be a clean 401, not a 500 --
    Immich's webhook action never inspects the response, so a 500 here would
    be indistinguishable from success on its side.
    """
    http, assets = client
    response = http.post(
        "/hooks/immich",
        json=payload(B64),
        headers={"X-IGP-Secret": b"wr\xe9ng"},
    )
    assert response.status_code == 401
    assert assets.get("a1") is None


def test_the_webhook_uses_the_legacy_account_not_just_the_first_one(tmp_path):
    """Ruling R1: with more than one account registered, the pre-multi-account
    `/hooks/immich` path must land on the account the migration recorded as
    legacy -- not silently on whichever account happens to sort first."""
    from immich_gphotos.accounts.registry import Account, AccountRegistry
    from immich_gphotos.storage_keys import LEGACY_WEBHOOK_ACCOUNT_KEY

    registry = AccountRegistry(tmp_path / "registry", env={})

    def _account(account_id: str) -> Account:
        conn = connect(tmp_path / f"{account_id}.db")
        clock = FakeClock()
        services = Services(
            assets=AssetRepo(conn, clock),
            albums=AlbumRepo(conn),
            cursors=CursorRepo(conn),
            settings_repo=SettingRepo(conn),
            events=EventRepo(conn, clock),
            runtime=None,
            settings=Settings(),
            webhook_secret="s3cret",
            webhook_header="X-IGP-Secret",
        )
        record = registry.accounts_repo.add(
            account_id=account_id, label=account_id, created_at="2026-09-20T10:00:00Z"
        )
        account = Account(record=record, services=services, loops=None)
        registry.register(account)
        return account

    first = _account("acct-a")
    second = _account("acct-b")
    registry.settings.set(LEGACY_WEBHOOK_ACCOUNT_KEY, second.id)

    http = TestClient(create_app(registry))
    response = http.post("/hooks/immich", json=payload(B64), headers={"X-IGP-Secret": "s3cret"})

    assert response.status_code == 200
    assert first.services.assets.get("a1") is None
    assert second.services.assets.get("a1") is not None
