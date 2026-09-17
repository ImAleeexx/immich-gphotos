import pytest
from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app
from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.models import Asset, Outcome, Priority
from immich_gphotos.services import Services
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo


class StubRuntime:
    paused_reason = None


@pytest.fixture
def http(tmp_path):
    conn = connect(tmp_path / "t.db")
    clock = FakeClock()
    assets = AssetRepo(conn, clock)
    assets.upsert_pending(
        Asset("a", "sum-a", "a.jpg", "IMAGE", 1, "2026-09-17T10:00:00Z", None, "timeline", False, False),
        Priority.WEBHOOK,
    )
    assets.claim_next(limit=1)
    assets.mark_synced("a", "k", Outcome.UPLOADED)
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
    return TestClient(create_app(services))


def test_healthz_is_open_and_ok(http):
    response = http.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_metrics_expose_prometheus_counters(http):
    body = http.get("/metrics").text
    assert "immich_gphotos_assets_total" in body
    assert 'state="synced"' in body
    assert "immich_gphotos_paused 0" in body
