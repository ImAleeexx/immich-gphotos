"""Every request resolves the account it acts on (Task 4). `http`/`rig_registry`
and `empty_http`/`empty_registry` come from tests/api/conftest.py.
"""


def test_pages_serve_the_default_account(http):
    assert http.get("/").status_code == 200


def test_an_unknown_account_cookie_falls_back_to_the_default(http):
    http.cookies.set("igp_account", "does-not-exist")
    assert http.get("/api/status").status_code == 200


def test_with_no_accounts_everything_redirects_to_the_accounts_page(empty_http):
    response = empty_http.get("/")
    assert response.status_code == 307
    assert response.headers["location"] == "/accounts"


def test_with_no_accounts_an_api_path_gets_a_409_not_a_redirect(empty_http):
    """A browser can follow a redirect; an API client cannot usefully follow
    one back into itself, so /api/* reports the condition instead."""
    response = empty_http.get("/api/status")
    assert response.status_code == 409


def test_logout_works_with_no_accounts_configured(empty_http):
    """Ruling R10: /logout is session-authenticated (not in OPEN_PATHS) but
    account-agnostic -- the session token lives in registry.settings, not
    any account. Before this fix, the no-account redirect swept it up too:
    a 307 to /accounts preserves the POST, so the browser would re-POST
    there and hit a 404/405, leaving the logout button in the shared header
    permanently dead on a fresh install. It must behave exactly like any
    other logout: a 303 to /login, never a redirect to /accounts."""
    response = empty_http.post("/logout")
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_the_password_lives_in_the_control_database_not_an_account(rig_registry):
    from immich_gphotos.api.auth import PASSWORD_KEY

    assert rig_registry.settings.get(PASSWORD_KEY) is not None
    account = rig_registry.default()
    assert account.services.settings_repo.get(PASSWORD_KEY) is None


def test_the_named_account_cookie_is_honoured_over_the_default(tmp_path):
    """resolve_account must actually consult the cookie, not just fall back
    to the default every time -- the naive "always return registry.default()"
    implementation would also pass every other test in this file."""
    from fastapi.testclient import TestClient

    from immich_gphotos.accounts.registry import Account, AccountRegistry
    from immich_gphotos.api.app import ACCOUNT_COOKIE, create_app
    from immich_gphotos.api.auth import PASSWORD_KEY, hash_password
    from immich_gphotos.clock import FakeClock
    from immich_gphotos.config import Settings
    from immich_gphotos.services import Services
    from immich_gphotos.store.albums import AlbumRepo
    from immich_gphotos.store.assets import AssetRepo
    from immich_gphotos.store.db import connect
    from immich_gphotos.store.events import EventRepo
    from immich_gphotos.store.kv import CursorRepo, SettingRepo

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
            webhook_secret="s",
            clock=clock,
        )
        record = registry.accounts_repo.add(
            account_id=account_id, label=account_id, created_at="2026-09-20T10:00:00Z"
        )
        account = Account(record=record, services=services, loops=None)
        registry.register(account)
        return account

    first = _account("acct-a")
    second = _account("acct-b")
    registry.settings.set(PASSWORD_KEY, hash_password("test-password"))

    client = TestClient(create_app(registry), follow_redirects=False)
    assert client.post("/login", data={"password": "test-password"}).status_code == 303
    client.cookies.set(ACCOUNT_COOKIE, second.id)

    first.services.events.add("info", "should not surface")
    second.services.events.add("info", "should surface")

    body = client.get("/api/status").json()
    messages = [e["message"] for e in body["events"]]
    assert "should surface" in messages
    assert "should not surface" not in messages
