from immich_gphotos.store.db import connect
from immich_gphotos.store.kv import CursorRepo, SettingRepo


def test_cursor_roundtrip(tmp_path):
    c = CursorRepo(connect(tmp_path / "t.db"))
    assert c.get("reconcile") is None
    c.set("reconcile", "2026-09-17T10:00:00+00:00")
    assert c.get("reconcile") == "2026-09-17T10:00:00+00:00"
    c.set("reconcile", "2026-09-17T11:00:00+00:00")
    assert c.get("reconcile") == "2026-09-17T11:00:00+00:00"


def test_the_database_is_not_world_readable(tmp_path):
    """It holds the Immich API key and Google auth_data."""
    import stat

    db = tmp_path / "t.db"
    connect(db)
    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_settings_store_json_values(tmp_path):
    s = SettingRepo(connect(tmp_path / "t.db"))
    s.set("filters", {"max_size_bytes": 10, "skip_raw": True})
    assert s.get("filters") == {"max_size_bytes": 10, "skip_raw": True}
    assert s.get("missing") is None
