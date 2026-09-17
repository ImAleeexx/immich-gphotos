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
from immich_gphotos.api.routes import DELETIONS_ENABLE_PHRASE, MIN_BANDWIDTH_BYTES_PER_SECOND
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
    longer offer 0, or any other value below MIN_BANDWIDTH_BYTES_PER_SECOND:
    the throttle now honours the full configured wait rather than silently
    truncating it (see sync.worker.THROTTLE_SLEEP_CHUNK_SECONDS), so an
    absurdly low-but-nonzero rate is no longer just slow -- it is the only
    thing standing between a "legitimate-looking" setting and a multi-day
    stall of the whole background loop."""
    http, _, _ = _rig(tmp_path)

    settings_page = http.get("/settings")
    wizard_page = http.get("/wizard")

    expected = f'name="bandwidth_bytes_per_second" min="{MIN_BANDWIDTH_BYTES_PER_SECOND}"'
    assert expected in settings_page.text
    assert expected in wizard_page.text
    assert 'name="bandwidth_bytes_per_second" min="0"' not in settings_page.text
    assert 'name="bandwidth_bytes_per_second" min="0"' not in wizard_page.text
    assert 'name="bandwidth_bytes_per_second" min="1"' not in settings_page.text
    assert 'name="bandwidth_bytes_per_second" min="1"' not in wizard_page.text


def test_zero_bandwidth_cap_is_rejected_by_the_api(tmp_path):
    """0 must never reach TokenBucket -- see sync.throttle.TokenBucket, which
    now rejects a non-positive rate outright rather than clamping it to the
    most extreme possible throttle."""
    http, services, _ = _rig(tmp_path)

    response = http.put("/api/settings", json={"bandwidth_bytes_per_second": 0})

    assert response.status_code == 422
    assert services.settings.bandwidth_bytes_per_second is None


def test_a_pathologically_low_bandwidth_cap_is_rejected_by_the_api(tmp_path):
    """1 byte/second passed the old `ge=1` bound but, combined with the
    throttle now honouring its full computed wait rather than truncating it,
    would stall the background loop for as long as the cap and file size
    dictate. MIN_BANDWIDTH_BYTES_PER_SECOND is the guard against that -- a
    value below it is rejected outright, the same way 0 already was."""
    http, services, _ = _rig(tmp_path)

    response = http.put(
        "/api/settings", json={"bandwidth_bytes_per_second": MIN_BANDWIDTH_BYTES_PER_SECOND - 1}
    )

    assert response.status_code == 422
    assert services.settings.bandwidth_bytes_per_second is None


def test_explicit_null_clears_an_existing_bandwidth_cap(tmp_path):
    """I5: both UIs send `bandwidth_bytes_per_second: null` for a blank field.
    exclude_none=True used to drop that key entirely, so the old cap silently
    persisted forever -- "blank = unlimited" was a lie."""
    http, services, _ = _rig(tmp_path, initial_settings={"bandwidth_bytes_per_second": 500_000})
    assert services.settings.bandwidth_bytes_per_second == 500_000

    response = http.put("/api/settings", json={"bandwidth_bytes_per_second": None})

    assert response.status_code == 200
    assert services.settings.bandwidth_bytes_per_second is None
    assert http.get("/api/settings").json()["bandwidth_bytes_per_second"] is None


def test_omitting_the_bandwidth_field_leaves_an_existing_cap_unchanged(tmp_path):
    """The other half of I5: a field the client never sent at all (as opposed
    to explicitly nulling it) must not be touched by an unrelated save."""
    http, services, _ = _rig(tmp_path, initial_settings={"bandwidth_bytes_per_second": 500_000})

    response = http.put("/api/settings", json={"worker_threads": 4})

    assert response.status_code == 200
    assert services.settings.worker_threads == 4
    assert services.settings.bandwidth_bytes_per_second == 500_000


def test_a_settings_save_while_halted_leaves_the_service_halted(tmp_path):
    """I6: rebuild_runtime constructs a fresh Runtime/BackgroundLoops on every
    settings save, which used to silently clear an active AUTH_INVALID/
    QUOTA_EXHAUSTED halt -- dropping the dashboard's banner and resuming a
    transfer against still-bad credentials. A settings save is not itself a
    reason to resume."""
    http, services, loops_handle = _rig(tmp_path)
    services.runtime.pause("AUTH_INVALID")
    # Advances BackgroundLoops' own pause bookkeeping the same way a real
    # background-loop iteration would.
    loops_handle.current.iterate()
    assert loops_handle.current._paused_at is not None

    response = http.put("/api/settings", json={"worker_threads": 5})

    assert response.status_code == 200
    assert services.settings.worker_threads == 5
    assert services.runtime.paused_reason == "AUTH_INVALID"
    assert http.get("/api/status").json()["paused_reason"] == "AUTH_INVALID"
    assert loops_handle.current._paused_at is not None


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
