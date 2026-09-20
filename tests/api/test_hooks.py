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


# --- Task 7: per-account webhook path ---------------------------------------
#
# The tests above exercise the single, pre-Task-7 `/hooks/immich` route
# against hand-built `Services` graphs. These use Task 8's `two_account_http`
# and `webhook_payload` fixtures (tests/api/conftest.py) instead: two *real*
# accounts built through `registry.create`, which is what actually gives each
# one its own `webhook_secret` to discriminate against.


def test_a_webhook_reaches_the_account_named_in_the_path(two_account_http, webhook_payload):
    client, first, second = two_account_http
    response = client.post(
        f"/hooks/immich/{second.id}",
        json=webhook_payload("asset-2"),
        headers={"X-IGP-Secret": second.services.webhook_secret},
    )
    assert response.status_code == 200
    assert second.services.assets.get("asset-2") is not None
    assert first.services.assets.get("asset-2") is None


def test_one_accounts_secret_is_rejected_at_anothers_path(two_account_http, webhook_payload):
    client, first, second = two_account_http
    response = client.post(
        f"/hooks/immich/{second.id}",
        json=webhook_payload("asset-3"),
        headers={"X-IGP-Secret": first.services.webhook_secret},
    )
    assert response.status_code == 401
    assert second.services.assets.get("asset-3") is None
    assert first.services.assets.get("asset-3") is None


def test_an_unknown_account_is_a_401_not_a_404(two_account_http, webhook_payload):
    """404 would confirm which account ids exist to an unauthenticated caller."""
    client, _, _ = two_account_http
    response = client.post(
        "/hooks/immich/deadbeefcafe",
        json=webhook_payload("asset-4"),
        headers={"X-IGP-Secret": "anything"},
    )
    assert response.status_code == 401


def test_the_legacy_path_still_reaches_the_migrated_account(two_account_http, webhook_payload):
    """An install upgraded from v1 already has a workflow in Immich pointing
    at the bare path. Dropping it stops that person's backups silently."""
    client, first, _ = two_account_http
    response = client.post(
        "/hooks/immich",
        json=webhook_payload("asset-5"),
        headers={"X-IGP-Secret": first.services.webhook_secret},
    )
    assert response.status_code == 200
    assert first.services.assets.get("asset-5") is not None


def test_a_dead_legacy_account_is_a_clean_401_not_a_500(two_account_http, webhook_payload):
    """test_remove_leaves_a_dead_legacy_webhook_key_solvable_by_task_7
    (tests/accounts/test_registry.py) deliberately leaves
    `LEGACY_WEBHOOK_ACCOUNT_KEY` pointing at an id `registry.get` no longer
    resolves once that account is removed. This is the HTTP side of that:
    the legacy route must not turn `None.services` into an AttributeError,
    and must not silently reroute into whichever account happens to remain
    (that would queue someone else's asset into the wrong account) -- a
    clean 401 is the only acceptable outcome.
    """
    client, first, second = two_account_http
    registry = client.app.state.accounts
    registry.remove(first.id, delete_data=False)

    # Deliberately `second`'s own, *correct* secret: a naive `registry.get(dead_id)
    # or registry.default()` fallback would resolve to `second` and then accept
    # this, silently queuing the asset into an account the caller never named.
    # Only a strict "the legacy key names a live account, or nothing" check
    # catches that; a plain secret-mismatch would pass even with the bug.
    response = client.post(
        "/hooks/immich",
        json=webhook_payload("asset-6"),
        headers={"X-IGP-Secret": second.services.webhook_secret},
    )

    assert response.status_code == 401
    assert second.services.assets.get("asset-6") is None


def test_unparseable_payload_warns_the_right_accounts_event_log(two_account_http):
    """The 400-on-unparseable-payload behaviour predates Task 7 (see
    `test_non_json_body_returns_400_without_raising` above); what's new is
    that it must land in *that account's* event log, not whichever account
    the old single-account code happened to resolve."""
    client, first, second = two_account_http
    response = client.post(
        f"/hooks/immich/{second.id}",
        json={"nope": True},
        headers={"X-IGP-Secret": second.services.webhook_secret},
    )
    assert response.status_code == 400
    assert any(e["level"] == "warn" for e in second.services.events.recent())
    assert first.services.events.recent() == []
