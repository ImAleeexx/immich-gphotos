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
    from immich_gphotos.accounts.registry import Account, AccountRegistry

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
    # /metrics is an open path -- it resolves the default account straight
    # from the registry (Ruling R1), so no login is needed here.
    registry = AccountRegistry(tmp_path / "registry", env={})
    record = registry.accounts_repo.add(
        account_id="acct-1", label="Default", created_at="2026-09-20T10:00:00Z"
    )
    registry.register(Account(record=record, services=services, loops=None))
    return TestClient(create_app(registry))


def test_healthz_is_open_and_ok(http):
    response = http.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_metrics_expose_prometheus_counters(http):
    body = http.get("/metrics").text
    assert "immich_gphotos_assets_total" in body
    assert 'state="synced"' in body
    assert "immich_gphotos_paused 0" in body


def test_metrics_on_a_fresh_install_with_no_accounts_reports_zero_rather_than_500(tmp_path):
    """Ruling R1: /metrics is unauthenticated and unwatched -- Prometheus
    scraping a container nobody has added an account to yet must get a clean
    200 with no asset-state lines, never a 500 from `.services` on `None`."""
    from immich_gphotos.accounts.registry import AccountRegistry

    registry = AccountRegistry(tmp_path / "registry", env={})
    assert registry.default() is None
    http = TestClient(create_app(registry))

    response = http.get("/metrics")

    assert response.status_code == 200
    # The HELP/TYPE header lines are unconditional; what must be absent is any
    # actual per-state gauge value, which only exists once counts_by_state()
    # has something to report.
    assert "immich_gphotos_assets_total{state=" not in response.text
    assert "immich_gphotos_paused 0" in response.text


def test_healthz_is_open_and_ok_with_no_accounts(tmp_path):
    """/healthz never touches an account at all, so it must be unaffected by
    whether any exist."""
    from immich_gphotos.accounts.registry import AccountRegistry

    registry = AccountRegistry(tmp_path / "registry", env={})
    http = TestClient(create_app(registry))

    response = http.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
