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
from immich_gphotos.main import build_services


def _rig(tmp_path, initial_settings=None):
    services, loops_handle = build_services(tmp_path, env={})
    if initial_settings is not None:
        services.settings_repo.set("settings", initial_settings)
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

    put_response = http.put("/api/settings", json={"deletions_enabled": True})
    assert put_response.status_code == 200

    get_response = http.get("/api/settings")
    assert get_response.json()["deletions_enabled"] is True
    assert services.settings.deletions_enabled is True


def test_worker_thread_settings_also_flow_through_the_live_swap(tmp_path):
    """Not just deletions_enabled: rebuild_runtime rebuilds the whole graph
    against the new Settings, so a Worker built afterwards uses the new
    filters/retry policy too."""
    http, services, loops_handle = _rig(tmp_path)

    response = http.put("/api/settings", json={"worker_threads": 7, "quality": "quota"})
    assert response.status_code == 200

    assert services.settings.worker_threads == 7
    assert services.settings.quality == "quota"
    assert loops_handle.current._runtime._settings.worker_threads == 7
