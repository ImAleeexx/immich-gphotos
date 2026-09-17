"""PUT /api/settings must take effect on the already-running background loop,
not only after a restart -- see composition.rebuild_runtime. deletions_enabled
is the sharpest case: a stale in-memory loop that keeps trashing Google items
after a user turned it off would be actively dangerous, not just stale.

This uses the real `build_services` wiring (not a hand-built Services), so
the assertion reaches the actual background-loop object the service starts a
thread on in `main()`.
"""

from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app
from immich_gphotos.api.auth import PASSWORD_KEY, hash_password
from immich_gphotos.api.routes import DELETIONS_ENABLE_PHRASE
from immich_gphotos.gphotos.client import GpmcClient
from immich_gphotos.main import build_services
from immich_gphotos.storage_keys import GOOGLE_AUTH_KEY


def _rig(tmp_path, initial_settings=None, google_auth_data=None):
    services, loops_handle = build_services(tmp_path, env={})
    if initial_settings is not None or google_auth_data is not None:
        if initial_settings is not None:
            services.settings_repo.set("settings", initial_settings)
        if google_auth_data is not None:
            # Not a real credential -- GpmcClient only authenticates lazily,
            # per-thread, the first time a call actually touches gpmc.Client
            # (see GpmcClient._client); nothing here does that, so this dummy
            # string is never used as a credential against a real service.
            services.settings_repo.set(GOOGLE_AUTH_KEY, google_auth_data)
        services, loops_handle = build_services(tmp_path, env={})
    services.settings_repo.set(PASSWORD_KEY, hash_password("test-password"))
    http = TestClient(create_app(services), follow_redirects=False)
    login = http.post("/login", data={"password": "test-password"})
    assert login.status_code == 303
    return http, services, loops_handle


def test_turning_deletions_off_stops_the_running_sweeper_without_a_restart(tmp_path):
    http, services, loops_handle = _rig(tmp_path, initial_settings={"deletions_enabled": True})
    assert services.settings.deletions_enabled is True
    assert loops_handle.current._deletion_sweeper._settings.deletions_enabled is True
    original_loops = loops_handle.current

    response = http.put("/api/settings", json={"deletions_enabled": False})
    assert response.status_code == 200

    assert services.settings.deletions_enabled is False
    assert loops_handle.current is not original_loops
    assert loops_handle.current._deletion_sweeper._settings.deletions_enabled is False


def test_get_settings_reports_the_running_value_not_a_stale_stored_one(tmp_path):
    """Regression guard: GET /api/settings used to splat the raw stored row
    over the live values, so it could report a change as applied even when
    nothing downstream had picked it up yet. It must report what the running
    service is actually using."""
    http, services, _ = _rig(tmp_path)

    put_response = http.put(
        "/api/settings",
        json={"deletions_enabled": True, "confirm_deletions": DELETIONS_ENABLE_PHRASE},
    )
    assert put_response.status_code == 200

    get_response = http.get("/api/settings")
    assert get_response.json()["deletions_enabled"] is True
    assert services.settings.deletions_enabled is True


def test_worker_thread_settings_also_flow_through_the_live_swap(tmp_path):
    """Not just deletions_enabled: rebuild_runtime rebuilds the whole graph
    against the new Settings, so a Runtime built afterwards uses the new
    worker_threads too."""
    http, services, loops_handle = _rig(tmp_path)

    response = http.put("/api/settings", json={"worker_threads": 7})
    assert response.status_code == 200

    assert services.settings.worker_threads == 7
    assert loops_handle.current._runtime._settings.worker_threads == 7


def test_changing_quality_actually_changes_what_the_running_uploader_uses(tmp_path):
    """C1 regression guard: GpmcClient bakes `quality` in at construction and
    `upload()` reads it from `self`, not from `Settings`. rebuild_runtime used
    to carry the *existing* gphotos client forward unchanged on a
    settings-only rebuild, so a quality change was accepted, stored, and
    reported back by GET /api/settings -- while the live uploader silently
    kept uploading at the old quality forever. Assert on the client's
    effective quality, not on services.settings, which is exactly the
    distinction that let this ship."""
    http, services, loops_handle = _rig(tmp_path, google_auth_data="not-a-real-credential")
    assert isinstance(services.gphotos, GpmcClient)
    assert services.gphotos.quality == "original"

    response = http.put("/api/settings", json={"quality": "quota"})

    assert response.status_code == 200
    assert response.json()["quality"] == "quota"
    assert services.settings.quality == "quota"
    # The effective behaviour: the actual client the running Worker holds.
    assert services.gphotos.quality == "quota"
    assert loops_handle.current._runtime._worker._gphotos is services.gphotos


def test_settings_page_no_longer_invites_zero_as_a_bandwidth_cap(tmp_path):
    """C2: `min="0"` on this field, next to the "blank = unlimited" hint, is
    exactly what invited "0 means unlimited" -- but 0 passes the (old) API
    validation and wedges the background loop for ~58 days on a single 5 MB
    upload (TokenBucket used to clamp it to 1 byte/second). The field must no
    longer offer 0."""
    http, _, _ = _rig(tmp_path)

    settings_page = http.get("/settings")
    wizard_page = http.get("/wizard")

    assert 'name="bandwidth_bytes_per_second" min="1"' in settings_page.text
    assert 'name="bandwidth_bytes_per_second" min="1"' in wizard_page.text
    assert 'name="bandwidth_bytes_per_second" min="0"' not in settings_page.text
    assert 'name="bandwidth_bytes_per_second" min="0"' not in wizard_page.text


def test_zero_bandwidth_cap_is_rejected_by_the_api(tmp_path):
    """0 must never reach TokenBucket -- see sync.throttle.TokenBucket, which
    now rejects a non-positive rate outright rather than clamping it to the
    most extreme possible throttle."""
    http, services, _ = _rig(tmp_path)

    response = http.put("/api/settings", json={"bandwidth_bytes_per_second": 0})

    assert response.status_code == 422
    assert services.settings.bandwidth_bytes_per_second is None


# --- Deletion propagation's typed confirmation (spec: "Off by default, ------
# behind an explicit toggle with typed confirmation") -----------------------


def test_enabling_deletions_without_confirmation_is_rejected_and_nothing_is_stored(tmp_path):
    http, services, _ = _rig(tmp_path)

    response = http.put("/api/settings", json={"deletions_enabled": True})

    assert response.status_code == 422
    assert services.settings.deletions_enabled is False
    assert services.settings_repo.get("settings") is None


def test_enabling_deletions_with_the_wrong_phrase_is_rejected(tmp_path):
    http, services, _ = _rig(tmp_path)

    response = http.put(
        "/api/settings",
        json={"deletions_enabled": True, "confirm_deletions": "yes, enable it"},
    )

    assert response.status_code == 422
    assert services.settings.deletions_enabled is False


def test_enabling_deletions_with_the_exact_phrase_succeeds(tmp_path):
    http, services, loops_handle = _rig(tmp_path)

    response = http.put(
        "/api/settings",
        json={"deletions_enabled": True, "confirm_deletions": DELETIONS_ENABLE_PHRASE},
    )

    assert response.status_code == 200
    assert services.settings.deletions_enabled is True
    assert loops_handle.current._deletion_sweeper._settings.deletions_enabled is True


def test_disabling_deletions_needs_no_confirmation(tmp_path):
    http, services, _ = _rig(tmp_path, initial_settings={"deletions_enabled": True})

    response = http.put("/api/settings", json={"deletions_enabled": False})

    assert response.status_code == 200
    assert services.settings.deletions_enabled is False


def test_resaving_while_already_enabled_does_not_re_demand_the_phrase(tmp_path):
    """The settings page always submits the whole form, so an unrelated save
    (e.g. worker_threads) resubmits deletions_enabled: true unchanged. That is
    not a new "enable" and must not be blocked for lacking the phrase."""
    http, services, _ = _rig(tmp_path, initial_settings={"deletions_enabled": True})

    response = http.put("/api/settings", json={"deletions_enabled": True, "worker_threads": 4})

    assert response.status_code == 200
    assert services.settings.deletions_enabled is True
    assert services.settings.worker_threads == 4
