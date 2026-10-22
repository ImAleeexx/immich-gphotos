"""Live Photo end to end: still + `.MOV` in, one Motion Photo out."""

import base64
import hashlib
from dataclasses import replace
from pathlib import Path

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Filters, RetryPolicy
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, AssetState, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.livephoto import LIVE_PHOTO_PAIRED_REASON
from immich_gphotos.sync.worker import Worker

STILL_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128

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

MOV = Asset(
    immich_id="mov-1",
    checksum="sum-mov",
    filename="IMG_1.MOV",
    type="VIDEO",
    size_bytes=len(VIDEO_BYTES),
    immich_updated_at="2026-09-17T10:00:00Z",
    taken_at="2022-07-05T12:00:00.000Z",
    live_photo_video_id=None,
    original_path=None,
    visibility="hidden",
    is_offline=False,
    is_trashed=False,
)


def _minimal_heic_still() -> bytes:
    """A tiny but real HEIC still for the ExifTool path (checked-in fixture).

    Generated once with pillow-heif (red square); ExifTool accepts it for
    XMP writes, unlike hand-assembled boxes, so the live test below proves
    the real integration instead of a toy format.
    """
    return (Path(__file__).with_name("fixtures") / "small.heic").read_bytes()


def _sha1_b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


def _still_hash() -> str:
    return _sha1_b64(STILL_BYTES)


def _muxed_hash() -> str:
    from immich_gphotos.sync.livephoto import build_motion_photo_jpeg

    return _sha1_b64(build_motion_photo_jpeg(STILL_BYTES, VIDEO_BYTES))


def _rig(tmp_path):
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    immich = FakeImmichClient(contents={"still-1": STILL_BYTES, "mov-1": VIDEO_BYTES})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch", direct_roots=[tmp_path], assets=assets)
    worker = Worker(
        assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, live_photo_mode="derived_motion"
    )
    return worker, assets, immich, gphotos


def _enqueue(assets: AssetRepo):
    assets.upsert_pending(STILL, Priority.WEBHOOK)
    assets.upsert_pending(MOV, Priority.WEBHOOK)


def test_resolve_muxes_the_pair_into_one_motion_photo(tmp_path):
    worker, assets, _, _ = _rig(tmp_path)
    _enqueue(assets)
    _ = worker

    resolver = ByteResolver(
        FakeImmichClient(contents={"still-1": STILL_BYTES, "mov-1": VIDEO_BYTES}),
        scratch=tmp_path / "scratch2",
        allow_direct=False,
        assets=assets,
    )
    resolved = resolver.resolve(STILL, mux_live_photo=True)

    assert resolved.temporary is True
    assert resolved.paired_video_id == "mov-1"
    data = resolved.path.read_bytes()
    assert data.startswith(b"\xff\xd8")
    assert data.endswith(VIDEO_BYTES)
    assert b"Camera:MotionPhoto" in data
    resolver.release(resolved)
    assert not resolved.path.exists()


def test_worker_uploads_one_muxed_file_and_pairs_the_clip(tmp_path):
    worker, assets, _, gphotos = _rig(tmp_path)
    _enqueue(assets)
    stored = assets.claim_next(limit=1)[0]
    assert stored.asset.immich_id == "still-1"  # webhook priority ties broken by first_seen
    result = worker.process(stored)

    assert result.state is AssetState.SYNCED
    assert result.outcome is Outcome.UPLOADED
    # One upload, described by the muxed bytes' hash -- not the still's.
    assert len(gphotos.uploads) == 1
    assert gphotos.uploads[0][0] == _muxed_hash()
    assert assets.get("mov-1").ineligible_reason == LIVE_PHOTO_PAIRED_REASON
    assert assets.get("still-1").media_key == result.media_key


def test_missing_companion_falls_back_to_the_still_alone(tmp_path):
    """derived_motion waits a bounded number of times for the companion row,
    then backs the still up alone and records the motion as deferred."""
    from immich_gphotos.sync.worker import MAX_LIVE_PHOTO_DEFERS

    worker, assets, _, gphotos = _rig(tmp_path)
    assets.upsert_pending(STILL, Priority.WEBHOOK)
    for _ in range(MAX_LIVE_PHOTO_DEFERS):
        with assets._conn.lock:
            assets._conn.execute("UPDATE asset SET next_attempt_at = NULL")
        result = worker.process(assets.claim_next(limit=1)[0])
        assert result.deferred is True
        assert gphotos.uploads == []
    with assets._conn.lock:
        assets._conn.execute("UPDATE asset SET next_attempt_at = NULL")
    result = worker.process(assets.claim_next(limit=1)[0])

    assert result.state is AssetState.SYNCED
    assert gphotos.uploads == [(_still_hash(), "IMG_1.JPG")]
    live = assets.live_photo("still-1")
    assert live["status"] == "deferred"
    assert live["reason"] == "companion_missing"


def test_heic_still_falls_back_without_pairing(tmp_path):
    heic = STILL_BYTES.replace(b"\xff\xd8", b"\x00\x00", 1)
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    immich = FakeImmichClient(contents={"still-1": heic, "mov-1": VIDEO_BYTES})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch", direct_roots=[tmp_path], assets=assets)
    worker = Worker(
        assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, live_photo_mode="derived_motion"
    )
    _enqueue(assets)

    result = worker.process(assets.claim_next(limit=1)[0])

    assert result.state is AssetState.SYNCED
    assert gphotos.uploads == [(_sha1_b64(heic), "IMG_1.JPG")]
    assert assets.get("mov-1").ineligible_reason != LIVE_PHOTO_PAIRED_REASON


def test_heic_pair_is_muxed_via_exiftool_and_paired_on_upload(tmp_path):
    from immich_gphotos.sync import livephoto
    from immich_gphotos.sync.exif_xmp import FakeMotionPhotoXmp

    heic_bytes = _minimal_heic_still()
    fake = FakeMotionPhotoXmp(keyframe_us=1500000)
    # The fake leaves the staged copy byte-identical (the real writer would
    # embed the packet); the trailer append is real code either way.
    expected = heic_bytes + livephoto.heic_trailer(VIDEO_BYTES, len(heic_bytes))
    expected_hash = base64.b64encode(hashlib.sha1(expected).digest()).decode()

    heic_still = replace(STILL, immich_id="still-1", filename="IMG_1.HEIC", live_photo_video_id="mov-1")
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    immich = FakeImmichClient(contents={"still-1": heic_bytes, "mov-1": VIDEO_BYTES})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(
        immich, scratch=tmp_path / "scratch", direct_roots=[tmp_path], assets=assets, xmp=fake
    )
    worker = Worker(
        assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, live_photo_mode="derived_motion"
    )
    assets.upsert_pending(heic_still, Priority.WEBHOOK)
    assets.upsert_pending(MOV, Priority.WEBHOOK)

    result = worker.process(assets.claim_next(limit=1)[0])

    assert result.state is AssetState.SYNCED
    assert result.outcome is Outcome.UPLOADED
    assert gphotos.uploads == [(expected_hash, "IMG_1.HEIC")]
    assert assets.get("mov-1").ineligible_reason == LIVE_PHOTO_PAIRED_REASON
    # The packet ExifTool was asked to write names the motion bytes and the
    # migrated keyframe.
    assert len(fake.writes) == 1
    packet = fake.writes[0][1].decode()
    assert 'Camera:MotionPhotoPresentationTimestampUs="1500000"' in packet
    assert f'Item:Length="{len(expected) - len(heic_bytes) - 8}"' in packet


def test_heic_falls_back_when_exiftool_is_missing_or_refuses(tmp_path):
    from immich_gphotos.sync.exif_xmp import FakeMotionPhotoXmp

    heic_bytes = _minimal_heic_still()

    class Refusing(FakeMotionPhotoXmp):
        def write_xmp(self, image, packet):
            raise RuntimeError("exiftool refused")

    for index, xmp in enumerate((None, Refusing())):
        clock = FakeClock()
        assets = AssetRepo(connect(tmp_path / f"t{index}.db"), clock)
        immich = FakeImmichClient(contents={"still-1": heic_bytes, "mov-1": VIDEO_BYTES})
        gphotos = FakeGooglePhotosClient()
        resolver = ByteResolver(
            immich, scratch=tmp_path / "scratch", direct_roots=[tmp_path], assets=assets, xmp=xmp
        )
        worker = Worker(
            assets,
            gphotos,
            resolver,
            Filters(),
            RetryPolicy(jitter=0.0),
            clock,
            live_photo_mode="derived_motion",
        )
        assets.upsert_pending(
            replace(STILL, filename="IMG_1.HEIC", live_photo_video_id="mov-1"), Priority.WEBHOOK
        )
        assets.upsert_pending(MOV, Priority.WEBHOOK)

        result = worker.process(assets.claim_next(limit=1)[0])

        assert result.state is AssetState.SYNCED
        assert gphotos.uploads == [(_sha1_b64(heic_bytes), "IMG_1.HEIC")]
        assert assets.get("mov-1").ineligible_reason != LIVE_PHOTO_PAIRED_REASON


def test_muxed_remote_hit_marks_synced_without_uploading(tmp_path):
    worker, assets, _, gphotos = _rig(tmp_path)
    _enqueue(assets)
    gphotos.present[_muxed_hash()] = "motion-key"

    result = worker.process(assets.claim_next(limit=1)[0])

    assert result.outcome is Outcome.ALREADY_PRESENT
    assert result.media_key == "motion-key"
    assert gphotos.uploads == []
    assert assets.get("mov-1").ineligible_reason == LIVE_PHOTO_PAIRED_REASON


def test_direct_read_pair_is_muxed_without_touching_originals(tmp_path):
    still_path = tmp_path / "IMG_1.JPG"
    mov_path = tmp_path / "IMG_1.MOV"
    still_path.write_bytes(STILL_BYTES)
    mov_path.write_bytes(VIDEO_BYTES)
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    immich = FakeImmichClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch", direct_roots=[tmp_path], assets=assets)
    assets.upsert_pending(replace(STILL, original_path=str(still_path)), Priority.WEBHOOK)
    assets.upsert_pending(replace(MOV, original_path=str(mov_path)), Priority.WEBHOOK)

    resolved = resolver.resolve(replace(STILL, original_path=str(still_path)), mux_live_photo=True)

    assert resolved.paired_video_id == "mov-1"
    assert resolved.path.read_bytes().endswith(VIDEO_BYTES)
    assert still_path.read_bytes() == STILL_BYTES  # untouched
    assert mov_path.read_bytes() == VIDEO_BYTES  # untouched
    resolver.release(resolved)


def test_avif_pair_is_muxed_with_an_avif_primary_item(tmp_path):
    from immich_gphotos.sync import livephoto
    from immich_gphotos.sync.exif_xmp import FakeMotionPhotoXmp

    avif_bytes = (Path(__file__).with_name("fixtures") / "small.avif").read_bytes()
    assert livephoto.isobmff_image_kind(avif_bytes) == "avif"
    fake = FakeMotionPhotoXmp(keyframe_us=None)
    expected = avif_bytes + livephoto.heic_trailer(VIDEO_BYTES, len(avif_bytes))

    still = replace(STILL, immich_id="still-1", filename="IMG_1.AVIF", live_photo_video_id="mov-1")
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    immich = FakeImmichClient(contents={"still-1": avif_bytes, "mov-1": VIDEO_BYTES})
    gphotos = FakeGooglePhotosClient()
    resolver = ByteResolver(
        immich, scratch=tmp_path / "scratch", direct_roots=[tmp_path], assets=assets, xmp=fake
    )
    worker = Worker(
        assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, live_photo_mode="derived_motion"
    )
    assets.upsert_pending(still, Priority.WEBHOOK)
    assets.upsert_pending(MOV, Priority.WEBHOOK)

    result = worker.process(assets.claim_next(limit=1)[0])

    assert result.state is AssetState.SYNCED
    assert gphotos.uploads == [(_sha1_b64(expected), "IMG_1.AVIF")]
    packet = fake.writes[0][1].decode()
    assert 'Item:Mime="image/avif"' in packet
    row = assets.live_photo("still-1")
    assert (row["status"], row["still_sha1"], row["derived_sha1"]) == (
        "complete",
        _sha1_b64(avif_bytes),
        _sha1_b64(expected),
    )
