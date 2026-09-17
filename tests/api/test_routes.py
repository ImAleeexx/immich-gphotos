import pytest
from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app
from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.models import Asset, AssetState, ErrorClass, Outcome, Priority
from immich_gphotos.services import Services
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo


def asset(i: str) -> Asset:
    return Asset(
        immich_id=i,
        checksum=f"sum-{i}",
        filename=f"{i}.jpg",
        type="IMAGE",
        size_bytes=1,
        immich_updated_at="2026-09-17T10:00:00Z",
        original_path=None,
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
    )


class StubRuntime:
    paused_reason = None


@pytest.fixture
def rig(tmp_path):
    conn = connect(tmp_path / "t.db")
    clock = FakeClock()
    assets = AssetRepo(conn, clock)
    services = Services(
        assets=assets,
        albums=AlbumRepo(conn),
        cursors=CursorRepo(conn),
        settings_repo=SettingRepo(conn),
        events=EventRepo(conn, clock),
        runtime=StubRuntime(),
        settings=Settings(),
        webhook_secret="s",
    )
    return TestClient(create_app(services)), assets, services


def test_status_reports_counts_and_pause_state(rig):
    http, assets, _ = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_synced("a", "k", Outcome.ALREADY_PRESENT)
    assets.upsert_pending(asset("b"), Priority.WEBHOOK)

    body = http.get("/api/status").json()
    assert body["counts"]["synced"] == 1
    assert body["counts"]["pending"] == 1
    assert body["paused_reason"] is None
    assert body["window_open"] is True


def test_failures_are_listed_with_their_error_class(rig):
    http, assets, _ = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_failed("a", ErrorClass.UNSUPPORTED_MEDIA, "rejected by google")

    body = http.get("/api/failures").json()
    assert body[0]["immich_id"] == "a"
    assert body[0]["error_class"] == "unsupported_media"
    assert body[0]["last_error"] == "rejected by google"


def test_manual_retry_returns_a_failure_to_the_queue(rig):
    http, assets, _ = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_failed("a", ErrorClass.UNKNOWN, "boom")

    assert http.post("/api/failures/a/retry").status_code == 200
    stored = assets.get("a")
    assert stored.state is AssetState.PENDING
    assert stored.attempts == 0


def test_retrying_an_unknown_asset_is_404(rig):
    http, _, _ = rig
    assert http.post("/api/failures/nope/retry").status_code == 404


def test_settings_roundtrip(rig):
    http, _, _ = rig
    response = http.put("/api/settings", json={"quality": "saver", "albums_enabled": False})
    assert response.status_code == 200
    body = http.get("/api/settings").json()
    assert body["quality"] == "saver"
    assert body["albums_enabled"] is False


def test_settings_reject_an_unknown_quality(rig):
    http, _, _ = rig
    assert http.put("/api/settings", json={"quality": "lossless"}).status_code == 422


def test_event_stream_emits_a_status_frame(rig):
    http, _, _ = rig
    with http.stream("GET", "/events?max_events=1") as response:
        assert response.headers["content-type"].startswith("text/event-stream")
        payload = "".join(response.iter_text())
    assert "counts" in payload
