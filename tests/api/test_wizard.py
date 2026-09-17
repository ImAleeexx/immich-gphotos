"""Tests for the /api/wizard/* routes: the wiring between the tested Wizard
class and an actually-running service.

Immich is exercised through respx-mocked HTTP (real HttpImmichClient, no real
network); Google is exercised by monkeypatching `gpmc.Client` (real
GpmcClient facade, no real network) -- the same techniques the existing
immich/gphotos client test suites already use.
"""

import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app
from immich_gphotos.api.auth import PASSWORD_KEY, hash_password
from immich_gphotos.gphotos.client import GpmcClient
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.client import HttpImmichClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.main import build_services
from immich_gphotos.setup.wizard import CORE_PERMISSIONS, REQUIRED_PERMISSIONS
from immich_gphotos.storage_keys import GOOGLE_AUTH_KEY, IMMICH_KEY_KEY, IMMICH_URL_KEY, WORKFLOW_ID_KEY

BASE = "https://immich.test"

# Deliberately not shaped like an androidId=...&app=... blob: this proves the
# wizard's own per-request scrub, not just Redactor's built-in shape pattern.
SECRET_AUTH_DATA = "totally-not-android-id-shaped-secret-value"


@pytest.fixture
def rig(tmp_path):
    services, loops_handle = build_services(tmp_path, env={})
    services.settings_repo.set(PASSWORD_KEY, hash_password("test-password"))
    http = TestClient(create_app(services), follow_redirects=False)
    login = http.post("/login", data={"password": "test-password"})
    assert login.status_code == 303
    return http, services, loops_handle


def mock_server(version=(3, 2, 2), permissions=None, method_keys=None):
    permissions = sorted(permissions if permissions is not None else REQUIRED_PERMISSIONS)
    method_keys = method_keys if method_keys is not None else ["immich-plugin-core#webhook"]
    respx.get(f"{BASE}/api/server/version").mock(
        return_value=httpx.Response(200, json={"major": version[0], "minor": version[1], "patch": version[2]})
    )
    respx.get(f"{BASE}/api/api-keys/me").mock(
        return_value=httpx.Response(200, json={"permissions": permissions})
    )
    respx.get(f"{BASE}/api/plugins/methods").mock(
        return_value=httpx.Response(200, json=[{"key": k} for k in method_keys])
    )


class _FakeGpmcOk:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def get_media_key_by_hash(self, checksum):
        return None


class _FakeGpmcFail:
    def __init__(self, auth_data=None, **kwargs) -> None:
        raise ValueError(f"could not parse: {auth_data}")


# --- Immich step ---------------------------------------------------------


@respx.mock
def test_immich_missing_permission_is_rejected_and_not_persisted(rig):
    http, services, _ = rig
    mock_server(permissions=set(REQUIRED_PERMISSIONS) - {"asset.download"})

    response = http.post("/api/wizard/immich", json={"immich_url": BASE, "immich_api_key": "bad-key"})

    assert response.status_code == 422
    assert response.json()["detail"]["missing_permissions"] == ["asset.download"]
    assert services.settings_repo.get(IMMICH_URL_KEY) is None
    assert services.settings_repo.get(IMMICH_KEY_KEY) is None
    assert isinstance(services.immich, FakeImmichClient)  # unchanged: still the boot-time fake
    assert "bad-key" not in response.text


@respx.mock
def test_immich_success_persists_and_swaps_the_live_client(rig):
    http, services, loops_handle = rig
    mock_server()
    original_loops = loops_handle.current

    response = http.post("/api/wizard/immich", json={"immich_url": BASE, "immich_api_key": "good-key"})

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "event_driven"
    assert "good-key" not in response.text

    assert services.settings_repo.get(IMMICH_URL_KEY) == BASE
    assert services.settings_repo.get(IMMICH_KEY_KEY) == "good-key"
    assert isinstance(services.immich, HttpImmichClient)
    # The swap reached the background loop, not just the Services field.
    assert loops_handle.current is not original_loops
    assert loops_handle.current._reconciler._immich is services.immich


@respx.mock
def test_pre_3_0_server_completes_setup_in_reconciler_only_mode(rig):
    http, services, _ = rig
    mock_server(version=(2, 9, 0), permissions=CORE_PERMISSIONS)

    response = http.post("/api/wizard/immich", json={"immich_url": BASE, "immich_api_key": "good-key"})

    assert response.status_code == 200
    assert response.json()["mode"] == "reconciler_only"
    assert response.json()["event_driven"] is False
    assert services.settings_repo.get(IMMICH_KEY_KEY) == "good-key"
    assert isinstance(services.immich, HttpImmichClient)


# --- Google step -----------------------------------------------------------


def test_google_validation_failure_does_not_persist_and_scrubs_the_secret(rig, monkeypatch):
    http, services, _ = rig
    monkeypatch.setattr("gpmc.Client", _FakeGpmcFail)

    response = http.post("/api/wizard/google", json={"google_auth_data": SECRET_AUTH_DATA})

    assert response.status_code == 422
    assert SECRET_AUTH_DATA not in response.text
    assert services.settings_repo.get(GOOGLE_AUTH_KEY) is None
    assert isinstance(services.gphotos, FakeGooglePhotosClient)


def test_google_field_validation_error_does_not_echo_the_submitted_secret(rig):
    """Live finding: pydantic v2 sets a "missing field" error's `input` to the
    *entire* request body. A request with a misnamed field (e.g. the real
    field name typo'd) makes `google_auth_data` "missing", and the default
    FastAPI/pydantic error response then echoes the whole body -- including
    the real credential the caller just submitted -- back in the 422. This
    must never reach the response body, regardless of which field was
    misnamed or malformed."""
    http, services, _ = rig

    response = http.post("/api/wizard/google", json={"wrong_field_name": SECRET_AUTH_DATA})

    assert response.status_code == 422
    assert SECRET_AUTH_DATA not in response.text
    assert services.settings_repo.get(GOOGLE_AUTH_KEY) is None


def test_immich_field_validation_error_does_not_echo_the_submitted_secret(rig):
    """Same class of leak as the Google case above, for the Immich API key."""
    http, services, _ = rig

    response = http.post(
        "/api/wizard/immich", json={"immich_url": BASE, "wrong_field_name": "immich-secret-value"}
    )

    assert response.status_code == 422
    assert "immich-secret-value" not in response.text
    assert services.settings_repo.get(IMMICH_KEY_KEY) is None


def test_google_validation_success_persists_and_swaps_the_live_client(rig, monkeypatch):
    http, services, loops_handle = rig
    monkeypatch.setattr("gpmc.Client", _FakeGpmcOk)
    original_loops = loops_handle.current

    response = http.post("/api/wizard/google", json={"google_auth_data": SECRET_AUTH_DATA})

    assert response.status_code == 200
    assert SECRET_AUTH_DATA not in response.text
    assert services.settings_repo.get(GOOGLE_AUTH_KEY) == SECRET_AUTH_DATA
    assert isinstance(services.gphotos, GpmcClient)
    assert loops_handle.current is not original_loops
    assert loops_handle.current._album_mirror._gphotos is services.gphotos


# --- Workflow step -----------------------------------------------------------


def test_workflow_registration_requires_immich_connected_first(rig):
    http, _, _ = rig
    response = http.post("/api/wizard/workflow", json={"webhook_url": "http://igp:8080/hooks/immich"})
    assert response.status_code == 400


@respx.mock
def test_workflow_registration_stores_the_id_and_uses_the_running_secret(rig):
    http, services, _ = rig
    mock_server()
    connected = http.post("/api/wizard/immich", json={"immich_url": BASE, "immich_api_key": "good-key"})
    assert connected.status_code == 200

    route = respx.post(f"{BASE}/api/workflows").mock(return_value=httpx.Response(201, json={"id": "wf-42"}))

    response = http.post("/api/wizard/workflow", json={"webhook_url": "http://igp:8080/hooks/immich"})

    assert response.status_code == 200
    assert response.json()["workflow_id"] == "wf-42"
    assert services.settings_repo.get(WORKFLOW_ID_KEY) == "wf-42"
    assert services.workflow_id == "wf-42"

    sent = json.loads(route.calls.last.request.read())
    header = sent["steps"][0]["config"]
    # The exact secret and header name the running /hooks/immich receiver
    # checks (services.webhook_secret/webhook_header) -- not a second,
    # freshly minted one.
    assert header["headerValue"] == services.webhook_secret
    assert header["headerName"] == services.webhook_header


# --- Options step -----------------------------------------------------------


def test_options_step_persists_settings_and_can_start_backfill(rig):
    http, services, _ = rig
    response = http.post(
        "/api/wizard/options",
        json={"quality": "saver", "albums_enabled": False, "start_backfill": True},
    )
    assert response.status_code == 200
    assert response.json()["backfill_started"] is True
    assert services.settings.quality == "saver"
    assert services.settings.albums_enabled is False
    assert services.backfill.is_running() is True


def test_options_step_quality_actually_reaches_the_google_client_from_step_2(rig, monkeypatch):
    """C1's exact repro: the wizard's own primary path is step 2 (Google)
    constructing a GpmcClient at the settings default ("original"), then step
    4 (Options) setting the user's chosen quality. rebuild_runtime used to
    carry the *existing* gphotos client forward unchanged on a settings-only
    rebuild, so this had no effect on the client actually in use, no matter
    what GET /api/settings reported."""
    http, services, _ = rig
    monkeypatch.setattr("gpmc.Client", _FakeGpmcOk)

    google_response = http.post("/api/wizard/google", json={"google_auth_data": SECRET_AUTH_DATA})
    assert google_response.status_code == 200
    assert isinstance(services.gphotos, GpmcClient)
    assert services.gphotos.quality == "original"

    options_response = http.post("/api/wizard/options", json={"quality": "saver"})
    assert options_response.status_code == 200
    assert services.settings.quality == "saver"
    # The effective behaviour, not just the stored setting.
    assert services.gphotos.quality == "saver"


# --- Cross-cutting -----------------------------------------------------------


@respx.mock
def test_wizard_status_never_echoes_either_credential(rig, monkeypatch):
    http, services, _ = rig
    mock_server()
    http.post("/api/wizard/immich", json={"immich_url": BASE, "immich_api_key": "immich-secret-value"})
    monkeypatch.setattr("gpmc.Client", _FakeGpmcOk)
    http.post("/api/wizard/google", json={"google_auth_data": "google-secret-value"})

    status = http.get("/api/wizard/status")

    assert status.status_code == 200
    assert "immich-secret-value" not in status.text
    assert "google-secret-value" not in status.text
    body = status.json()
    assert body["immich_configured"] is True
    assert body["google_configured"] is True
    assert body["mode"] == "event_driven"


def test_wizard_routes_require_a_session(tmp_path):
    """These must sit behind the same session auth as everything else under
    /api -- not added to OPEN_PATHS."""
    services, _ = build_services(tmp_path, env={})
    http = TestClient(create_app(services), follow_redirects=False)
    response = http.post("/api/wizard/immich", json={"immich_url": BASE, "immich_api_key": "x"})
    assert response.status_code == 401
