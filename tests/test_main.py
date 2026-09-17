import logging

from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app
from immich_gphotos.api.routes import SETTING_KEY
from immich_gphotos.main import build_services
from immich_gphotos.store.db import connect
from immich_gphotos.store.kv import SettingRepo


def test_build_services_wires_a_working_app(tmp_path):
    services, loops = build_services(tmp_path, env={})
    client = TestClient(create_app(services))
    assert client.get("/healthz").json()["status"] == "ok"
    assert services.webhook_secret
    assert loops is not None


def test_a_credential_added_after_boot_is_scrubbed_from_both_events_and_logs(tmp_path):
    """The wizard persists a credential (e.g. the Immich API key) well after
    build_services ran and the log handler/Redactor were constructed. Both
    must share one Redactor instance and grow together via add_secret, or a
    credential entered through the wizard would never be scrubbed for the
    rest of the process's life."""
    services, _ = build_services(tmp_path, env={})

    handler = logging.getLogger().handlers[0]
    assert handler.formatter._redactor is services.redactor

    services.redactor.add_secret("brand-new-secret")
    services.events.add("info", "token was brand-new-secret")
    assert "brand-new-secret" not in services.events.recent(1)[0]["message"]


def test_the_webhook_secret_survives_a_restart(tmp_path):
    first, _ = build_services(tmp_path, env={})
    second, _ = build_services(tmp_path, env={})
    assert first.webhook_secret == second.webhook_secret


def test_backfill_and_wizard_and_workflow_are_wired(tmp_path):
    """The /api/backfill/* routes use services.backfill unconditionally; if it
    defaults to None those routes 500. The wizard and immich client must also be
    populated so the setup and diagnostics routes work. On a fresh database no
    workflow has been created yet, so workflow_id must read back as None
    rather than some leftover or hardcoded value."""
    services, _ = build_services(tmp_path, env={})
    assert services.backfill is not None
    assert services.wizard is not None
    assert services.immich is not None
    assert services.clock is not None
    assert services.workflow_id is None


def test_a_persisted_setting_survives_a_rebuild_of_the_services(tmp_path):
    """Correction 1: build_services must read back settings the API persisted
    under the "settings" key, or the settings UI is decorative."""
    conn = connect(tmp_path / "immich-gphotos.db")
    SettingRepo(conn).set(SETTING_KEY, {"quality": "saver", "albums_enabled": False, "worker_threads": 5})

    services, _ = build_services(tmp_path, env={})

    assert services.settings.quality == "saver"
    assert services.settings.albums_enabled is False
    assert services.settings.worker_threads == 5
    # Anything not present in the stored dict keeps its dataclass default.
    assert services.settings.deletions_enabled is False


def test_malformed_persisted_settings_do_not_crash_build_services(tmp_path):
    """A malformed settings row is user-writable JSON and must not make the
    container unstartable: unknown keys and wrong-typed values are ignored."""
    conn = connect(tmp_path / "immich-gphotos.db")
    SettingRepo(conn).set(
        SETTING_KEY,
        {
            "quality": "not-a-real-quality",
            "worker_threads": "five",
            "albums_enabled": "yes",
            "totally_unknown_key": 123,
            "bandwidth_bytes_per_second": -1,
        },
    )

    services, _ = build_services(tmp_path, env={})

    # None of the malformed values were applied; defaults stand.
    assert services.settings.quality == "original"
    assert services.settings.worker_threads == 2
    assert services.settings.albums_enabled is True


def test_a_hand_edited_zero_bandwidth_cap_is_ignored_not_applied(tmp_path):
    """C2: 0 must never reach TokenBucket, including via a stored settings
    row the API itself would now reject (ge=MIN_BANDWIDTH_BYTES_PER_SECOND).
    Falls back to the dataclass default (None, unlimited) exactly like any
    other out-of-bounds value."""
    conn = connect(tmp_path / "immich-gphotos.db")
    SettingRepo(conn).set(SETTING_KEY, {"bandwidth_bytes_per_second": 0})

    services, _ = build_services(tmp_path, env={})

    assert services.settings.bandwidth_bytes_per_second is None


def test_a_hand_edited_bandwidth_cap_below_the_minimum_rate_is_ignored(tmp_path):
    """A value like 1 (byte/second) is positive -- it would have passed the
    old `ge=1` API bound -- but is still far below
    MIN_BANDWIDTH_BYTES_PER_SECOND, the floor that keeps the throttle (which
    now honours its full computed wait rather than truncating it) from being
    reachable with a rate that stalls the background loop for days. A
    hand-edited row must be rejected the same way the API rejects it."""
    conn = connect(tmp_path / "immich-gphotos.db")
    SettingRepo(conn).set(SETTING_KEY, {"bandwidth_bytes_per_second": 1})

    services, _ = build_services(tmp_path, env={})

    assert services.settings.bandwidth_bytes_per_second is None


def test_worker_threads_out_of_the_apis_bounds_is_rejected(tmp_path):
    """The API's SettingsPatch bounds worker_threads to [1, 16]
    (MIN_WORKER_THREADS/MAX_WORKER_THREADS in api.routes). A hand-edited
    database row with a value the API itself would reject must not be
    applied, or a stored row could set what the API refuses to accept."""
    conn = connect(tmp_path / "immich-gphotos.db")
    SettingRepo(conn).set(SETTING_KEY, {"worker_threads": 17})

    services, _ = build_services(tmp_path, env={})

    assert services.settings.worker_threads == 2  # dataclass default, not 17


def test_stale_uploading_assets_are_requeued_at_startup(tmp_path):
    from datetime import timedelta

    from immich_gphotos.clock import SystemClock
    from immich_gphotos.models import Asset, Priority

    conn = connect(tmp_path / "immich-gphotos.db")
    from immich_gphotos.store.assets import AssetRepo

    assets = AssetRepo(conn, SystemClock())
    asset = Asset(
        immich_id="a1",
        checksum="c1",
        filename="f.jpg",
        type="IMAGE",
        size_bytes=1,
        immich_updated_at="2026-01-01T00:00:00+00:00",
        original_path=None,
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
        tags=(),
    )
    assets.upsert_pending(asset, Priority.RECONCILE)
    assets.claim_next(limit=1)
    # Backdate the claim so it looks stale to requeue_stale_uploading.
    with conn.lock:
        conn.execute(
            "UPDATE asset SET claimed_at = ? WHERE immich_id = ?",
            ((SystemClock().now() - timedelta(hours=2)).isoformat(), "a1"),
        )

    services, _ = build_services(tmp_path, env={})
    stored = services.assets.get("a1")
    assert stored.state.value == "pending"
