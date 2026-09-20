import pytest
from fastapi.testclient import TestClient

from immich_gphotos.accounts.registry import Account, AccountRegistry
from immich_gphotos.api.app import create_app
from immich_gphotos.api.auth import PASSWORD_KEY, SESSION_COOKIE, hash_password, verify_password
from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.services import Services
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo


class StubRuntime:
    paused_reason = None


@pytest.fixture
def rig(tmp_path):
    conn = connect(tmp_path / "t.db")
    clock = FakeClock()
    settings_repo = SettingRepo(conn)
    services = Services(
        assets=AssetRepo(conn, clock),
        albums=AlbumRepo(conn),
        cursors=CursorRepo(conn),
        settings_repo=settings_repo,
        events=EventRepo(conn, clock),
        runtime=StubRuntime(),
        settings=Settings(),
        webhook_secret="s3cret",
    )
    # Task 4: the admin password and session token now live in the control
    # database (`registry.settings`), never in an account's own
    # settings_repo -- see accounts/control.py's module docstring.
    registry = AccountRegistry(tmp_path / "registry", env={})
    record = registry.accounts_repo.add(
        account_id="acct-1", label="Default", created_at="2026-09-20T10:00:00Z"
    )
    registry.register(Account(record=record, services=services, loops=None))
    return TestClient(create_app(registry), follow_redirects=False), registry


def test_password_hashes_are_salted_and_verifiable():
    a, b = hash_password("hunter2"), hash_password("hunter2")
    assert a != b
    assert verify_password("hunter2", a) is True
    assert verify_password("wrong", a) is False


def test_dashboard_redirects_to_login_when_not_authenticated(rig):
    http, _ = rig
    response = http.get("/")
    assert response.status_code == 307
    assert response.headers["location"].endswith("/login")


def test_api_returns_401_rather_than_redirecting(rig):
    http, _ = rig
    assert http.get("/api/status").status_code == 401


def test_webhook_is_reachable_without_a_session(rig):
    http, _ = rig
    response = http.post("/hooks/immich", json={"nope": 1}, headers={"X-IGP-Secret": "s3cret"})
    assert response.status_code == 400  # reached the handler, rejected on content


def test_login_with_the_right_password_grants_access(rig):
    http, registry = rig
    registry.settings.set(PASSWORD_KEY, hash_password("hunter2"))
    assert http.post("/login", data={"password": "hunter2"}).status_code == 303
    assert http.get("/api/status").status_code == 200


def test_login_with_the_wrong_password_is_refused(rig):
    http, registry = rig
    original = hash_password("hunter2")
    registry.settings.set(PASSWORD_KEY, original)
    assert http.post("/login", data={"password": "nope"}).status_code == 401
    assert http.get("/api/status").status_code == 401
    # The authenticated (non-first-run) branch must never touch the stored
    # hash -- only the first-run branch is allowed to write PASSWORD_KEY.
    assert registry.settings.get(PASSWORD_KEY) == original


def test_first_run_login_page_offers_to_set_a_password(rig):
    """Renamed from `test_first_run_sends_you_to_the_wizard_to_set_a_password`:
    that name promised a redirect to /wizard which the body never asserted and
    which the implementation never performs -- GET /login simply renders the
    first-run copy in place. Fixed the name to match what is actually tested;
    no such redirect exists in this task's scope.
    """
    http, _ = rig
    response = http.get("/login")
    assert response.status_code == 200
    assert "set a password" in response.text.lower()


def test_logout_invalidates_the_old_session_cookie(rig):
    """A `/logout` that only deletes the browser cookie leaves the session
    token in registry.settings untouched, so a *captured* copy of the old
    cookie (shared machine, browser history, a proxy log) would keep working
    indefinitely. Proving a fresh, cookie-less client is unauthenticated after
    logout would pass even with that bug -- this replays the exact old cookie
    value on a separate client instead.
    """
    http, registry = rig
    registry.settings.set(PASSWORD_KEY, hash_password("hunter2"))
    assert http.post("/login", data={"password": "hunter2"}).status_code == 303
    old_cookie = http.cookies.get(SESSION_COOKIE)
    assert old_cookie is not None
    assert http.get("/api/status").status_code == 200

    assert http.post("/logout").status_code == 303

    replay = TestClient(create_app(registry), follow_redirects=False)
    replay.cookies.set(SESSION_COOKIE, old_cookie)
    assert replay.get("/api/status").status_code == 401


def test_high_byte_session_cookie_is_rejected_not_a_500(rig):
    """Starlette decodes cookies as latin-1, so a cookie byte >= 0x80 makes a
    non-ASCII str; hmac.compare_digest raises TypeError on that instead of
    just returning False. A corrupted cookie must be a clean 401, not a 500.
    """
    http, registry = rig
    registry.settings.set(PASSWORD_KEY, hash_password("hunter2"))
    registry.settings.set(SESSION_COOKIE, "realtoken")
    cookie_header = f"{SESSION_COOKIE}=br\xe9ken".encode("latin-1")
    response = http.get("/api/status", headers={"Cookie": cookie_header})
    assert response.status_code == 401


def test_open_paths_are_matched_exactly_not_by_prefix():
    from immich_gphotos.api.auth import is_open

    assert is_open("/metrics") is True
    assert is_open("/healthz") is True
    assert is_open("/login") is True
    assert is_open("/hooks/immich") is True
    # A future route sharing a prefix with an open path must NOT walk through
    # the auth gate just because it starts with one of these strings.
    assert is_open("/metrics-debug") is False
    assert is_open("/healthzzz") is False
    assert is_open("/login/foo") is False
    # /static/ is the one deliberate prefix exception -- see test_static.py's
    # test_the_allowance_is_scoped_to_static_only for the guard on its scope.
    assert is_open("/static/whatever") is True
    assert is_open("/staticky") is False
    # Task 7: `/hooks/immich/{account_id}` is a second deliberate prefix
    # exception, scoped exactly to it -- Ruling R10 requires this widen the
    # open surface by exactly this much and no more.
    assert is_open("/hooks/immich/abc123") is True
    assert is_open("/hooks/immichigan") is False


def test_first_run_login_sets_the_password_and_grants_access(rig):
    """Correction 1: on a fresh install nothing is stored yet, so the login
    handler must treat the first submitted password as the one to set, matching
    the "Set password" button login.html renders in that state. Otherwise no
    code path ever sets a password and every install is locked out forever.
    """
    http, registry = rig
    assert registry.settings.get(PASSWORD_KEY) is None

    response = http.post("/login", data={"password": "first-timer"})
    assert response.status_code == 303

    stored = registry.settings.get(PASSWORD_KEY)
    assert stored is not None
    assert verify_password("first-timer", stored) is True

    assert http.get("/api/status").status_code == 200
