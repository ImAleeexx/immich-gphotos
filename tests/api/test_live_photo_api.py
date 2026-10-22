"""Live Photo mode: persisted per account, live-applied, read-only plan and
explicit reprocess (PR #7 follow-up)."""

from immich_gphotos.accounts.build import _merged_settings
from immich_gphotos.api.routes import LIVE_PHOTO_REPROCESS_PHRASE
from immich_gphotos.models import Asset, Outcome, Priority
from immich_gphotos.storage_keys import SETTINGS_KEY

STILL = Asset(
    immich_id="still-1",
    checksum="sum-still",
    filename="IMG_1.JPG",
    type="IMAGE",
    size_bytes=1,
    immich_updated_at="2026-09-17T10:00:00Z",
    original_path=None,
    visibility="timeline",
    is_offline=False,
    is_trashed=False,
    live_photo_video_id="mov-1",
)


def test_an_existing_account_keeps_the_upstream_mode_after_upgrade():
    # A settings row written before the setting existed.
    assert _merged_settings("http://immich", {"quality": "original"}).live_photo_mode == "still_only_legacy"
    assert (
        _merged_settings("http://immich", {"live_photo_mode": "bogus"}).live_photo_mode == "still_only_legacy"
    )
    assert _merged_settings("http://immich", {"live_photo_mode": "original_pair"}).live_photo_mode == (
        "original_pair"
    )


def test_the_mode_is_account_scoped_persisted_and_live_applied(http, rig_registry):
    assert http.get("/api/settings").json()["live_photo_mode"] == "still_only_legacy"
    response = http.put("/api/settings", json={"live_photo_mode": "derived_motion"})
    assert response.status_code == 200
    assert response.json()["live_photo_mode"] == "derived_motion"
    account = rig_registry.default()
    assert account.services.settings_repo.get(SETTINGS_KEY) == {"live_photo_mode": "derived_motion"}
    assert account.services.settings.live_photo_mode == "derived_motion"
    assert http.put("/api/settings", json={"live_photo_mode": "nope"}).status_code == 422


def _synced_still(services):
    services.assets.upsert_pending(STILL, Priority.WEBHOOK)
    services.assets.claim_next()
    services.assets.mark_synced("still-1", "key-1", Outcome.UPLOADED)


def test_the_plan_is_read_only(http, rig_registry):
    services = rig_registry.default().services
    _synced_still(services)
    body = http.get("/api/live-photos").json()
    assert body == {"mode": "still_only_legacy", "groups": [], "untracked_synced_stills": 1}
    assert services.assets.live_photo("still-1") is None


def test_reprocess_needs_an_opt_in_mode_and_a_typed_phrase_for_bulk(http, rig_registry):
    services = rig_registry.default().services
    _synced_still(services)
    assert http.post(
        "/api/live-photos/reprocess", json={"confirm": LIVE_PHOTO_REPROCESS_PHRASE}
    ).status_code == (409)
    http.put("/api/settings", json={"live_photo_mode": "original_pair"})
    assert http.post("/api/live-photos/reprocess", json={}).status_code == 422
    assert services.assets.live_photo("still-1") is None

    ok = http.post("/api/live-photos/reprocess", json={"confirm": LIVE_PHOTO_REPROCESS_PHRASE})
    assert ok.json() == {"queued": 1, "mode": "original_pair"}
    row = services.assets.live_photo("still-1")
    assert (row["mode"], row["status"], row["reason"]) == ("original_pair", "pending", "reprocess_requested")
    # The still itself is untouched.
    assert services.assets.get("still-1").media_key == "key-1"


def test_reprocess_of_named_stills_needs_no_phrase(http, rig_registry):
    services = rig_registry.default().services
    _synced_still(services)
    http.put("/api/settings", json={"live_photo_mode": "derived_motion"})
    assert http.post("/api/live-photos/reprocess", json={"still_ids": ["still-1"]}).json()["queued"] == 1
    assert http.post("/api/live-photos/reprocess", json={"still_ids": []}).json()["queued"] == 0
