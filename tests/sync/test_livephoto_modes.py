"""Live Photo backup modes (PR #7 follow-up).

still_only_legacy is the default and reproduces the upstream behaviour; the
two opt-in modes keep the original still, the original .MOV and any derived
Motion Photo under separate, traceable identities.
"""

import base64
import hashlib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Filters, RetryPolicy, Settings
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.immich.protocol import ImmichError
from immich_gphotos.models import Asset, AssetState, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync import livephoto
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.livephoto import LIVE_PHOTO_PAIRED_REASON, MOTION_NOT_BACKED_UP
from immich_gphotos.sync.livephoto_backup import LivePhotoCompleter
from immich_gphotos.sync.worker import Worker

STILL_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
VIDEO_BYTES = b"\x00\x00\x00\x18ftypqt  " + b"\x00" * 128

STILL = Asset(
    immich_id="still-1",
    checksum="sum-still",
    filename="IMG_1.JPG",
    type="IMAGE",
    size_bytes=len(STILL_BYTES),
    immich_updated_at="2026-09-17T10:00:00Z",
    taken_at="2022-07-05T12:00:00.000Z",
    live_photo_video_id="mov-1",
    original_path=None,
    visibility="timeline",
    is_offline=False,
    is_trashed=False,
)
MOV = replace(
    STILL,
    immich_id="mov-1",
    checksum="sum-mov",
    filename="IMG_1.MOV",
    type="VIDEO",
    size_bytes=len(VIDEO_BYTES),
    live_photo_video_id=None,
    visibility="hidden",
)


def sha1(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


MUXED = livephoto.build_motion_photo_jpeg(STILL_BYTES, VIDEO_BYTES)


class Rig:
    def __init__(self, tmp_path: Path, mode: str, db: str = "t.db", immich=None, gphotos=None):
        self.tmp_path = tmp_path
        self.clock = FakeClock()
        self.assets = AssetRepo(connect(tmp_path / db), self.clock)
        self.immich = immich or FakeImmichClient(contents={"still-1": STILL_BYTES, "mov-1": VIDEO_BYTES})
        self.gphotos = gphotos or FakeGooglePhotosClient()
        self.resolver = ByteResolver(
            self.immich, scratch=tmp_path / "scratch", direct_roots=[], assets=self.assets
        )
        self.set_mode(mode)

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        self.completer = LivePhotoCompleter(self.assets, self.gphotos, self.resolver, self.clock, mode)
        self.worker = Worker(
            self.assets,
            self.gphotos,
            self.resolver,
            Filters(),
            RetryPolicy(jitter=0.0),
            self.clock,
            live_photo_mode=mode,
            live_photos=self.completer,
        )

    def enqueue(self, *assets: Asset) -> None:
        for a in assets:
            self.assets.upsert_pending(a, Priority.WEBHOOK)

    def drain(self):
        results = []
        while True:
            claimed = self.assets.claim_next(limit=1)
            if not claimed:
                return results
            results.append(self.worker.process(claimed[0]))

    def notes(self, immich_id: str) -> str | None:
        with self.assets._conn.lock:
            row = self.assets._conn.execute(
                "SELECT sync_notes FROM asset WHERE immich_id = ?", (immich_id,)
            ).fetchone()
        return row["sync_notes"]

    def scratch_files(self):
        scratch = self.tmp_path / "scratch"
        return list(scratch.iterdir()) if scratch.exists() else []


# --- defaults / legacy ---------------------------------------------------------


def test_the_default_mode_is_the_upstream_still_only_behaviour():
    assert Settings().live_photo_mode == "still_only_legacy"
    assert livephoto.DEFAULT_LIVE_PHOTO_MODE == "still_only_legacy"


def test_legacy_uploads_the_unmodified_still_and_never_claims_the_motion(tmp_path):
    rig = Rig(tmp_path, "still_only_legacy")
    rig.enqueue(STILL, MOV)
    rig.drain()

    assert rig.gphotos.uploads == [(sha1(STILL_BYTES), "IMG_1.JPG")]
    assert rig.assets.get("still-1").upload_sha1 == sha1(STILL_BYTES)
    assert rig.assets.get("mov-1").ineligible_reason == "hidden"  # never "paired"
    live = rig.assets.live_photo("still-1")
    assert live["status"] == "not_configured"
    assert live["reason"] == MOTION_NOT_BACKED_UP
    assert live["still_sha1"] == sha1(STILL_BYTES)
    assert rig.notes("still-1") == MOTION_NOT_BACKED_UP
    assert rig.scratch_files() == []


def test_resolve_never_muxes_unless_asked(tmp_path):
    rig = Rig(tmp_path, "still_only_legacy")
    rig.enqueue(MOV)
    resolved = rig.resolver.resolve(STILL)
    assert resolved.derived is False
    assert resolved.path.read_bytes() == STILL_BYTES


# --- original_pair ---------------------------------------------------------------


def test_original_pair_backs_up_both_originals_under_their_own_hashes(tmp_path):
    rig = Rig(tmp_path, "original_pair")
    rig.enqueue(STILL, MOV)
    rig.drain()

    assert sorted(rig.gphotos.uploads) == sorted(
        [(sha1(STILL_BYTES), "IMG_1.JPG"), (sha1(VIDEO_BYTES), "IMG_1.MOV")]
    )
    mov = rig.assets.get("mov-1")
    assert mov.state is AssetState.SYNCED
    assert mov.upload_sha1 == sha1(VIDEO_BYTES)
    live = rig.assets.live_photo("still-1")
    assert live["status"] == "complete"
    assert live["video_media_key"] == mov.media_key
    assert live["video_sha1"] == sha1(VIDEO_BYTES)
    assert live["derived_sha1"] is None
    assert rig.notes("still-1") is None  # motion is backed up
    assert rig.scratch_files() == []


def test_original_pair_completes_when_the_companion_arrives_later(tmp_path):
    rig = Rig(tmp_path, "original_pair")
    rig.enqueue(STILL)
    rig.drain()
    assert rig.assets.live_photo("still-1")["status"] == "pending"
    assert rig.notes("still-1") == MOTION_NOT_BACKED_UP

    rig.enqueue(MOV)
    rig.drain()  # the worker parks the hidden clip and completes the pair right away
    assert rig.assets.live_photo("still-1")["status"] == "complete"
    assert rig.assets.get("mov-1").state is AssetState.SYNCED


def _park_clip_without_worker(rig):
    """The clip row settles as hidden through a path that does not complete
    the pair inline (e.g. a crash right after, or an older build)."""
    rig.assets.upsert_pending(MOV, Priority.WEBHOOK)
    rig.assets.claim_next()
    rig.assets.mark_ineligible("mov-1", "hidden")


def test_original_pair_survives_a_restart_while_pending(tmp_path):
    rig = Rig(tmp_path, "original_pair")
    rig.enqueue(STILL)
    rig.drain()
    _park_clip_without_worker(rig)

    reopened = Rig(tmp_path, "original_pair", gphotos=rig.gphotos)
    reopened.clock.advance(timedelta(hours=12))
    assert reopened.completer.sweep().completed == 1
    assert reopened.assets.live_photo("still-1")["status"] == "complete"


def test_original_pair_never_uploads_the_clip_twice_after_a_lost_bookkeeping_write(tmp_path):
    rig = Rig(tmp_path, "original_pair")
    rig.enqueue(STILL)
    rig.drain()
    _park_clip_without_worker(rig)
    # The clip went up but the process died before recording it.
    rig.gphotos.present[sha1(VIDEO_BYTES)] = "clip-key"
    rig.clock.advance(timedelta(hours=12))
    rig.completer.sweep()
    assert [u for u in rig.gphotos.uploads if u[1] == "IMG_1.MOV"] == []
    assert rig.assets.get("mov-1").media_key == "clip-key"
    assert rig.assets.get("mov-1").outcome is Outcome.ALREADY_PRESENT


def test_original_pair_retries_a_temporarily_unreadable_clip(tmp_path):
    class Flaky(FakeImmichClient):
        fail = True

        def download_original(self, asset_id, dest):
            if asset_id == "mov-1" and self.fail:
                raise ImmichError("immich restarting")
            super().download_original(asset_id, dest)

    immich = Flaky(contents={"still-1": STILL_BYTES, "mov-1": VIDEO_BYTES})
    rig = Rig(tmp_path, "original_pair", immich=immich)
    rig.enqueue(MOV, STILL)
    rig.drain()
    live = rig.assets.live_photo("still-1")
    assert live["status"] == "pending"
    assert rig.assets.get("still-1").state is AssetState.SYNCED  # never blocked by the clip

    immich.fail = False
    rig.clock.advance(timedelta(days=1))
    rig.completer.sweep()
    assert rig.assets.live_photo("still-1")["status"] == "complete"


# --- derived_motion --------------------------------------------------------------


def test_derived_motion_records_three_separate_identities(tmp_path):
    rig = Rig(tmp_path, "derived_motion")
    rig.enqueue(STILL, MOV)
    rig.drain()

    assert rig.gphotos.uploads == [(sha1(MUXED), "IMG_1.JPG")]
    still = rig.assets.get("still-1")
    live = rig.assets.live_photo("still-1")
    assert live["status"] == "complete"
    assert live["derived_sha1"] == sha1(MUXED) == still.upload_sha1
    assert live["derived_media_key"] == still.media_key
    assert live["still_sha1"] == sha1(STILL_BYTES)  # never confused with the derived hash
    assert rig.assets.get("mov-1").ineligible_reason == LIVE_PHOTO_PAIRED_REASON
    assert rig.notes("still-1") is None


def test_an_original_already_in_google_is_not_taken_for_the_motion_photo(tmp_path):
    """Regression: the worker used to dedup on Immich's checksum *before*
    resolving, so an original already in Google was recorded as if the
    Motion Photo were there."""
    rig = Rig(tmp_path, "derived_motion")
    rig.gphotos.present["sum-still"] = "phone-backup-key"
    rig.enqueue(STILL, MOV)
    rig.drain()

    assert rig.gphotos.uploads == []
    live = rig.assets.live_photo("still-1")
    assert live["status"] == "deferred"
    assert live["reason"] == "original_present"
    assert live["derived_media_key"] is None
    assert rig.assets.get("mov-1").ineligible_reason == "hidden"  # NOT paired
    assert rig.notes("still-1") == MOTION_NOT_BACKED_UP


def test_an_original_found_by_content_hash_is_not_taken_for_the_motion_photo(tmp_path):
    """External Library: the checksum is virtual, so the original is only
    found by the content hash of its real bytes, after resolving."""
    rig = Rig(tmp_path, "derived_motion")
    rig.gphotos.present[sha1(STILL_BYTES)] = "phone-backup-key"
    rig.enqueue(replace(STILL, checksum="sha1-of-path"), MOV)
    rig.drain()

    assert rig.gphotos.uploads == []
    still = rig.assets.get("still-1")
    assert still.media_key == "phone-backup-key"
    assert still.upload_sha1 == sha1(STILL_BYTES)
    assert rig.assets.live_photo("still-1")["reason"] == "original_present"
    assert rig.assets.get("mov-1").ineligible_reason == "hidden"


def test_a_motion_photo_already_in_google_completes_without_upload(tmp_path):
    rig = Rig(tmp_path, "derived_motion")
    rig.gphotos.present[sha1(MUXED)] = "motion-key"
    rig.enqueue(STILL, MOV)
    rig.drain()
    assert rig.gphotos.uploads == []
    assert rig.assets.live_photo("still-1")["status"] == "complete"
    assert rig.assets.get("mov-1").ineligible_reason == LIVE_PHOTO_PAIRED_REASON


def test_a_permanent_mux_failure_backs_up_the_still_and_says_so(tmp_path):
    odd = b"GIF89a" + b"\x00" * 32
    immich = FakeImmichClient(contents={"still-1": odd, "mov-1": VIDEO_BYTES})
    rig = Rig(tmp_path, "derived_motion", immich=immich)
    rig.enqueue(STILL, MOV)
    rig.drain()

    assert rig.gphotos.uploads == [(sha1(odd), "IMG_1.JPG")]
    live = rig.assets.live_photo("still-1")
    assert live["status"] == "failed"
    assert live["reason"] == "still_format_unsupported"
    assert rig.assets.get("mov-1").ineligible_reason == "hidden"
    assert rig.notes("still-1") == MOTION_NOT_BACKED_UP
    assert rig.scratch_files() == []


def test_an_oversized_clip_is_refused_by_stat_without_reading_it(tmp_path, monkeypatch):
    monkeypatch.setattr(livephoto, "MAX_VIDEO_BYTES", 16)
    rig = Rig(tmp_path, "derived_motion")
    rig.enqueue(STILL, MOV)
    rig.drain()
    live = rig.assets.live_photo("still-1")
    assert live["status"] == "failed"
    assert live["reason"].startswith("rejected: companion video too large")


def test_muxing_never_loads_whole_files_into_memory(tmp_path, monkeypatch):
    def forbidden(self):
        raise AssertionError(f"read_bytes() on {self}")

    monkeypatch.setattr(Path, "read_bytes", forbidden)
    rig = Rig(tmp_path, "derived_motion")
    rig.enqueue(STILL, MOV)
    rig.drain()
    assert rig.assets.live_photo("still-1")["status"] == "complete"


def test_the_streamed_jpeg_matches_the_reference_builder(tmp_path):
    still, video, dest = tmp_path / "s.jpg", tmp_path / "v.mov", tmp_path / "out.jpg"
    still.write_bytes(STILL_BYTES)
    video.write_bytes(VIDEO_BYTES)
    digest = livephoto.write_motion_photo_jpeg(still, video, dest, "video/quicktime")
    with dest.open("rb") as handle:
        data = handle.read()
    assert data == MUXED
    assert digest == sha1(MUXED)


def test_a_mux_interrupted_by_io_leaves_no_scratch_and_retries(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(livephoto, "write_motion_photo_jpeg", boom)
    rig = Rig(tmp_path, "derived_motion")
    rig.enqueue(STILL, MOV)
    results = rig.drain()
    assert results[0].deferred is True  # transient: waits instead of settling for the still
    assert rig.gphotos.uploads == []
    assert rig.scratch_files() == []


# --- mode switches, history and reprocessing ---------------------------------------


def test_switching_mode_never_touches_already_synced_history(tmp_path):
    rig = Rig(tmp_path, "still_only_legacy")
    rig.enqueue(STILL, MOV)
    rig.drain()
    before = rig.assets.get("still-1")

    rig.set_mode("original_pair")
    rig.clock.advance(timedelta(days=1))
    assert rig.completer.sweep().completed == 0
    rig.set_mode("derived_motion")
    assert rig.completer.sweep().completed == 0

    assert rig.gphotos.uploads == [(sha1(STILL_BYTES), "IMG_1.JPG")]
    assert rig.assets.get("still-1") == before
    assert rig.assets.live_photo("still-1")["mode"] == "still_only_legacy"


def test_the_plan_is_read_only_and_counts_untracked_history(tmp_path):
    rig = Rig(tmp_path, "original_pair")
    rig.assets.upsert_pending(STILL, Priority.WEBHOOK)
    rig.assets.claim_next()
    rig.assets.mark_synced("still-1", "old-key", Outcome.UPLOADED)  # synced by an older build
    summary = rig.assets.live_photo_summary()
    assert summary == {"groups": [], "untracked_synced_stills": 1}
    assert rig.assets.live_photo("still-1") is None


def test_an_explicit_reprocess_adds_the_clip_without_touching_the_still(tmp_path):
    rig = Rig(tmp_path, "still_only_legacy")
    rig.enqueue(STILL, MOV)
    rig.drain()
    before = rig.assets.get("still-1")

    rig.set_mode("original_pair")
    assert rig.assets.request_live_photo_reprocess("original_pair") == 1
    rig.completer.sweep()

    assert rig.assets.get("still-1").media_key == before.media_key
    assert rig.assets.get("still-1").upload_sha1 == before.upload_sha1
    assert [u[1] for u in rig.gphotos.uploads] == ["IMG_1.JPG", "IMG_1.MOV"]
    assert rig.assets.live_photo("still-1")["status"] == "complete"
    # Complete under this mode: a second request queues nothing.
    assert rig.assets.request_live_photo_reprocess("original_pair") == 0


def test_a_derived_reprocess_adds_a_motion_photo_next_to_the_existing_still(tmp_path):
    rig = Rig(tmp_path, "derived_motion")
    rig.gphotos.present["sum-still"] = "phone-backup-key"
    rig.enqueue(STILL, MOV)
    rig.drain()
    assert rig.assets.live_photo("still-1")["reason"] == "original_present"

    assert rig.assets.request_live_photo_reprocess("derived_motion", ["still-1"]) == 1
    rig.completer.sweep()

    still = rig.assets.get("still-1")
    live = rig.assets.live_photo("still-1")
    assert still.media_key == "phone-backup-key"  # original untouched, not replaced
    assert live["status"] == "complete"
    assert live["derived_sha1"] == sha1(MUXED)
    assert live["derived_media_key"] not in (None, "phone-backup-key")
    assert rig.gphotos.uploads == [(sha1(MUXED), "IMG_1.JPG")]
    assert rig.assets.get("mov-1").ineligible_reason == LIVE_PHOTO_PAIRED_REASON


def test_a_derived_reprocess_never_unpairs_a_clip_backed_up_as_its_own_item(tmp_path):
    rig = Rig(tmp_path, "original_pair")
    rig.enqueue(STILL, MOV)
    rig.drain()
    mov_before = rig.assets.get("mov-1")
    assert mov_before.state is AssetState.SYNCED

    rig.set_mode("derived_motion")
    rig.assets.request_live_photo_reprocess("derived_motion")
    rig.completer.sweep()
    assert rig.assets.get("mov-1") == mov_before


@pytest.mark.parametrize("mode", ["still_only_legacy", "original_pair", "derived_motion"])
def test_an_ordinary_still_is_never_held_back_by_any_mode(tmp_path, mode):
    rig = Rig(tmp_path, mode)
    plain = replace(STILL, immich_id="plain", checksum="sum-plain", live_photo_video_id=None)
    rig.immich.contents["plain"] = STILL_BYTES + b"x"
    rig.enqueue(plain)
    (result,) = rig.drain()
    assert result.state is AssetState.SYNCED
    assert rig.assets.live_photo("plain") is None
    assert rig.notes("plain") is None
