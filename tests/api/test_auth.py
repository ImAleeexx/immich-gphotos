import pytest
from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app
from immich_gphotos.api.auth import PASSWORD_KEY, hash_password, verify_password
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
    return TestClient(create_app(services), follow_redirects=False), services


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
    http, services = rig
    services.settings_repo.set(PASSWORD_KEY, hash_password("hunter2"))
    assert http.post("/login", data={"password": "hunter2"}).status_code == 303
    assert http.get("/api/status").status_code == 200


def test_login_with_the_wrong_password_is_refused(rig):
    http, services = rig
    services.settings_repo.set(PASSWORD_KEY, hash_password("hunter2"))
    assert http.post("/login", data={"password": "nope"}).status_code == 401
    assert http.get("/api/status").status_code == 401


def test_first_run_sends_you_to_the_wizard_to_set_a_password(rig):
    http, _ = rig
    response = http.get("/login")
    assert response.status_code == 200
    assert "set a password" in response.text.lower()


def test_first_run_login_sets_the_password_and_grants_access(rig):
    """Correction 1: on a fresh install nothing is stored yet, so the login
    handler must treat the first submitted password as the one to set, matching
    the "Set password" button login.html renders in that state. Otherwise no
    code path ever sets a password and every install is locked out forever.
    """
    http, services = rig
    assert services.settings_repo.get(PASSWORD_KEY) is None

    response = http.post("/login", data={"password": "first-timer"})
    assert response.status_code == 303

    stored = services.settings_repo.get(PASSWORD_KEY)
    assert stored is not None
    assert verify_password("first-timer", stored) is True

    assert http.get("/api/status").status_code == 200
