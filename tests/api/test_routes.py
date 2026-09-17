import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app
from immich_gphotos.api.auth import PASSWORD_KEY, hash_password
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
    settings_repo = SettingRepo(conn)
    services = Services(
        assets=assets,
        albums=AlbumRepo(conn),
        cursors=CursorRepo(conn),
        settings_repo=settings_repo,
        events=EventRepo(conn, clock),
        runtime=StubRuntime(),
        settings=Settings(),
        webhook_secret="s",
        clock=clock,
    )
    # Task 20 adds a session-protected middleware ahead of these routes; log
    # in once here so the pre-existing Task 19 tests keep exercising the same
    # unauthenticated-request-shaped assertions against an authenticated client.
    settings_repo.set(PASSWORD_KEY, hash_password("test-password"))
    http = TestClient(create_app(services), follow_redirects=False)
    login_response = http.post("/login", data={"password": "test-password"})
    assert login_response.status_code == 303
    return http, assets, services


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
    # Finding 5: the dashboard's claim to show "whether the fast, direct-read
    # path is active" needs a real field to render -- `Services.allow_direct`
    # defaults to True (the `rig` fixture never overrides it).
    assert body["direct_reads_enabled"] is True


def test_failures_are_listed_with_their_error_class(rig):
    http, assets, _ = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_failed("a", ErrorClass.UNSUPPORTED_MEDIA, "rejected by google")

    body = http.get("/api/failures").json()
    assert body[0]["immich_id"] == "a"
    assert body[0]["error_class"] == "unsupported_media"
    assert body[0]["last_error"] == "rejected by google"


def test_failures_with_a_malicious_filename_are_not_rendered_unescaped(rig):
    """filename is Immich's originalFileName -- attacker-influenceable by anyone
    who can add a file (a shared album, a mobile client, an external library
    import) -- and last_error is arbitrary Google/gpmc error text. Neither may
    ever be interpolated into the failures page's DOM via innerHTML: a stored
    <script> or event-handler payload must never be executable in this
    credential-holding admin UI.

    The failures page builds its rows from JSON fetched client-side rather
    than server-rendering them, so a plain HTTP client cannot execute the
    page's JS to prove the DOM is safe. This instead asserts, at the level
    this test suite can reach: (1) the malicious strings survive the round
    trip through the store and the JSON API unchanged (a prerequisite for the
    bug -- they are not being silently stripped upstream), and (2) the
    template's row-building script never assigns interpolated field values to
    innerHTML, only ever building cells with textContent/createElement.
    """
    http, assets, _ = rig
    evil_filename = "<script>alert(document.cookie)</script>.jpg"
    evil_error = "<img src=x onerror=alert(document.cookie)>"
    evil_asset = Asset(
        immich_id="a",
        checksum="sum-a",
        filename=evil_filename,
        type="IMAGE",
        size_bytes=1,
        immich_updated_at="2026-09-17T10:00:00Z",
        original_path=None,
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
    )
    assets.upsert_pending(evil_asset, Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_failed("a", ErrorClass.UNKNOWN, evil_error)
    # mark_failed truncates and (per Finding 4) redacts, but does not strip
    # HTML -- the raw payload must still reach the JSON API for this to be a
    # meaningful test of the *rendering* fix rather than an upstream sanitizer.
    body = http.get("/api/failures").json()
    assert body[0]["filename"] == evil_filename
    assert body[0]["last_error"] == evil_error
    assert assets.get("a").last_error == evil_error

    template = Path(__file__).resolve().parents[2] / "src/immich_gphotos/web/templates/failures.html"
    source = template.read_text()
    # No assignment to .innerHTML anywhere in the row-building script (a plain
    # mention of the word, e.g. in a comment, is not what this guards against).
    assert not re.search(r"\.innerHTML\s*=", source)
    # Guard against the exact regression: a template literal splicing a field
    # straight into markup (`${f.filename}` etc. inside a backtick string).
    assert not re.search(r"`[^`]*\$\{f\.(filename|last_error)\}", source)
    assert "textContent" in source
    assert "createElement" in source


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


def test_retrying_a_non_quarantined_asset_is_404_and_leaves_it_unchanged(rig):
    http, assets, _ = rig
    assets.upsert_pending(asset("a"), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_synced("a", "k", Outcome.ALREADY_PRESENT)

    assert http.post("/api/failures/a/retry").status_code == 404
    stored = assets.get("a")
    assert stored.state is AssetState.SYNCED
    assert stored.attempts == 0


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
    assert payload.startswith("data: ")
    assert payload.endswith("\n\n")
    frame = json.loads(payload.removeprefix("data: ").rstrip("\n"))
    assert "counts" in frame
