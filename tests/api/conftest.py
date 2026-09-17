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
