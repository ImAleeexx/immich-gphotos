"""Fixtures shared across the API tests.

`test_routes.py` keeps its own `rig` -- it predates this file and returns a
different shape. This exists so the newer tests do not each rebuild a
Services graph by hand.
"""

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.models import Asset
from immich_gphotos.services import Services
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo
from immich_gphotos.store.kv import CursorRepo, SettingRepo


class StubRuntime:
    paused_reason = None


@pytest.fixture
def rig_services(tmp_path):
    """A freshly-booted, unconfigured Services graph -- what a container looks
    like before anyone has opened the wizard."""
    conn = connect(tmp_path / "t.db")
    clock = FakeClock()
    return Services(
        assets=AssetRepo(conn, clock),
        albums=AlbumRepo(conn),
        cursors=CursorRepo(conn),
        settings_repo=SettingRepo(conn),
        events=EventRepo(conn, clock),
        runtime=StubRuntime(),
        settings=Settings(),
        webhook_secret="s",
        clock=clock,
    )


@pytest.fixture
def configured_services(rig_services):
    """A services graph that has been through the wizard: real credentials
    stored and real (non-fake) clients swapped in."""
    from immich_gphotos.storage_keys import GOOGLE_AUTH_KEY, IMMICH_KEY_KEY, IMMICH_URL_KEY

    rig_services.settings_repo.set(IMMICH_URL_KEY, "http://immich:2283")
    rig_services.settings_repo.set(IMMICH_KEY_KEY, "immich-key")
    rig_services.settings_repo.set(GOOGLE_AUTH_KEY, "auth-data")
    rig_services.immich = object()  # stands for any non-Fake client
    rig_services.gphotos = object()
    return rig_services


@pytest.fixture
def rig_registry(tmp_path, rig_services):
    """A registry holding exactly one account, backed by the same
    `rig_services` graph the older fixtures build by hand."""
    from immich_gphotos.accounts.registry import Account, AccountRegistry
    from immich_gphotos.api.auth import PASSWORD_KEY, hash_password

    registry = AccountRegistry(tmp_path / "data", env={})
    record = registry.accounts_repo.add(
        account_id="acct-1", label="Default", created_at="2026-09-20T10:00:00Z"
    )
    registry.register(Account(record=record, services=rig_services, loops=None))
    registry.settings.set(PASSWORD_KEY, hash_password("test-password"))
    return registry


@pytest.fixture
def empty_registry(tmp_path):
    """A registry with zero accounts, as a fresh install looks before anyone
    has added one -- the state Task 9's "add account" flow starts from."""
    from immich_gphotos.accounts.registry import AccountRegistry
    from immich_gphotos.api.auth import PASSWORD_KEY, hash_password

    registry = AccountRegistry(tmp_path / "empty", env={})
    registry.settings.set(PASSWORD_KEY, hash_password("test-password"))
    return registry


def _authenticated(registry):
    from fastapi.testclient import TestClient

    from immich_gphotos.api.app import create_app

    client = TestClient(create_app(registry), follow_redirects=False)
    assert client.post("/login", data={"password": "test-password"}).status_code == 303
    return client


@pytest.fixture
def http(rig_registry):
    """An authenticated TestClient over a fresh, unconfigured install."""
    return _authenticated(rig_registry)


@pytest.fixture
def empty_http(empty_registry):
    """An authenticated TestClient with zero accounts configured -- login
    itself is account-agnostic (Ruling R2), but every other route redirects
    or 409s once there is nothing to serve (Ruling R5)."""
    return _authenticated(empty_registry)


@pytest.fixture
def asset_factory():
    def make(i: str) -> Asset:
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

    return make
