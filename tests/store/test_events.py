from immich_gphotos.clock import FakeClock
from immich_gphotos.logging import Redactor
from immich_gphotos.store.db import connect
from immich_gphotos.store.events import EventRepo

SECRET_SHAPED = "androidId=1234567890abcdef&app=com.google.android.apps.photos"


def test_recent_events_round_trip(tmp_path):
    conn = connect(tmp_path / "test.db")
    clock = FakeClock()
    events = EventRepo(conn, clock)
    events.add("info", "hello")
    assert events.recent(1)[0]["message"] == "hello"


def test_add_redacts_secret_shaped_message_by_default(tmp_path):
    """Even without an explicit Redactor wired in, EventRepo must not persist
    the auth_data shape verbatim -- this is what backs the /diagnostics page.
    """
    conn = connect(tmp_path / "test.db")
    clock = FakeClock()
    events = EventRepo(conn, clock)
    events.add("error", f"gpmc error: {SECRET_SHAPED}")
    stored = events.recent(1)[0]["message"]
    assert "1234567890abcdef" not in stored
    assert "[redacted]" in stored


def test_add_redacts_a_registered_secret(tmp_path):
    conn = connect(tmp_path / "test.db")
    clock = FakeClock()
    events = EventRepo(conn, clock, redactor=Redactor(["my-google-auth-blob"]))
    events.add("error", "upload failed with my-google-auth-blob")
    stored = events.recent(1)[0]["message"]
    assert "my-google-auth-blob" not in stored
