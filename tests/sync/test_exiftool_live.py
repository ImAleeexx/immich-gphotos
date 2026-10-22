"""Live ExifTool integration: the real binary against a real HEIC.

Skipped unless IGP_LIVE_EXIFTOOL=1 *and* an `exiftool` binary is on PATH,
so the ordinary suite stays hermetic (same convention as the Immich
contract tests). These are the only tests that prove the premise the whole
HEIC path rests on: ExifTool accepts our packet into a genuine HEIC file
and the XMP reads back intact. Runs in CI too -- GitHub's ubuntu runners
ship exiftool -- once the flag is set there.
"""

import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Filters, RetryPolicy
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, AssetState, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.exif_xmp import ExifToolMotionPhotoXmp
from immich_gphotos.sync.livephoto import heic_xmp_packet
from immich_gphotos.sync.worker import Worker

pytestmark = pytest.mark.skipif(
    os.environ.get("IGP_LIVE_EXIFTOOL") != "1" or shutil.which("exiftool") is None,
    reason="needs IGP_LIVE_EXIFTOOL=1 and an exiftool binary",
)

FIXTURE = Path(__file__).with_name("fixtures") / "small.heic"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def test_real_exiftool_round_trips_our_packet(tmp_path):
    still = tmp_path / "small.heic"
    still.write_bytes(FIXTURE.read_bytes())
    xmp = ExifToolMotionPhotoXmp()

    packet = heic_xmp_packet(len(VIDEO) + 64, "video/quicktime", "image/heic", None)
    xmp.write_xmp(still, packet)

    assert "Camera:MotionPhoto" in (xmp.original_xmp(still) or "")
    # The image survives the rewrite as a valid image.
    found = xmp._helper.get_tags([str(still)], ["FileType"])
    assert found[0].get("File:FileType") == "HEIC"


def test_real_exiftool_full_heic_mux_flow(tmp_path):
    """ByteResolver's HEIC flow with the real writer and a tagless video.

    Tagless synthetic video means the keyframe degrades to -1 (the
    fallback half of the contract); the structural half -- XMP embedded by
    ExifTool, trailer appended, companion paired, one muxed upload -- is
    fully real.
    """
    heic_bytes = FIXTURE.read_bytes()
    still = Asset(
        immich_id="still-1",
        checksum="sum-still",
        filename="IMG_1.HEIC",
        type="IMAGE",
        size_bytes=len(heic_bytes),
        immich_updated_at="2026-09-17T10:00:00Z",
        taken_at="2022-07-05T12:00:00.000Z",
        live_photo_video_id="mov-1",
        original_path=None,
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
    )
    mov = replace(
        still,
        immich_id="mov-1",
        checksum="sum-mov",
        filename="IMG_1.MOV",
        type="VIDEO",
        size_bytes=len(VIDEO),
        live_photo_video_id=None,
        visibility="hidden",
    )
    clock = FakeClock()
    assets = AssetRepo(connect(tmp_path / "t.db"), clock)
    immich = FakeImmichClient(contents={"still-1": heic_bytes, "mov-1": VIDEO})
    gphotos = FakeGooglePhotosClient()
    xmp = ExifToolMotionPhotoXmp()
    resolver = ByteResolver(
        immich, scratch=tmp_path / "scratch", direct_roots=[tmp_path], assets=assets, xmp=xmp
    )
    worker = Worker(
        assets, gphotos, resolver, Filters(), RetryPolicy(jitter=0.0), clock, live_photo_mode="derived_motion"
    )
    assets.upsert_pending(still, Priority.WEBHOOK)
    assets.upsert_pending(mov, Priority.WEBHOOK)

    # Content proof on the muxed bytes themselves, before the upload path
    # cleans up scratch: ExifTool-embedded XMP reads back, trailer present.
    resolved = resolver.resolve(still, mux_live_photo=True)
    try:
        data = resolved.path.read_bytes()
        # Trailer layout: mpvd{ video + sefd{ tags + SEFH..SEFT } } at the end
        # (mpvd_at points at the box *type*; the payload starts 4 past it).
        mpvd_at = data.index(b"mpvd")
        assert data[mpvd_at + 4 : mpvd_at + 4 + len(VIDEO)] == VIDEO
        assert data.endswith(b"SEFT")
        assert "Camera:MotionPhoto" in (xmp.original_xmp(resolved.path) or "")
    finally:
        resolver.release(resolved)

    result = worker.process(assets.claim_next(limit=1)[0])

    assert result.state is AssetState.SYNCED
    assert len(gphotos.uploads) == 1
    assert assets.get("mov-1").ineligible_reason == "live_photo_paired"


def test_real_exiftool_avif_mux_keeps_the_original_untouched(tmp_path):
    """AVIF still (checked-in Pillow-generated fixture) through the real
    ExifTool writer, read directly from a library root: the derived file
    carries the Motion Photo XMP and trailer, the original is byte-identical
    and keeps its mtime."""
    import os

    library = tmp_path / "library"
    library.mkdir()
    avif = library / "IMG_2.AVIF"
    avif.write_bytes((Path(__file__).with_name("fixtures") / "small.avif").read_bytes())
    clip = library / "IMG_2.MOV"
    clip.write_bytes(VIDEO)
    before = (avif.read_bytes(), avif.stat().st_mtime_ns)
    still = Asset(
        immich_id="still-2",
        checksum="sum-avif",
        filename="IMG_2.AVIF",
        type="IMAGE",
        size_bytes=len(before[0]),
        immich_updated_at="2026-09-17T10:00:00Z",
        taken_at=None,
        live_photo_video_id="mov-2",
        original_path=str(avif),
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
    )
    mov = replace(
        still,
        immich_id="mov-2",
        checksum="sum-mov2",
        filename="IMG_2.MOV",
        type="VIDEO",
        live_photo_video_id=None,
        original_path=str(clip),
        visibility="hidden",
    )
    assets = AssetRepo(connect(tmp_path / "t.db"), FakeClock())
    assets.upsert_pending(mov, Priority.WEBHOOK)
    xmp = ExifToolMotionPhotoXmp()
    resolver = ByteResolver(
        FakeImmichClient(), scratch=tmp_path / "scratch", direct_roots=[library], assets=assets, xmp=xmp
    )
    resolved = resolver.resolve(still, mux_live_photo=True)
    try:
        assert resolved.derived is True
        packet = xmp.original_xmp(resolved.path) or ""
        assert "Camera:MotionPhoto" in packet
        assert 'Item:Mime="image/avif"' in packet
        data = resolved.path.read_bytes()
        assert data.endswith(b"SEFT")
        found = xmp._helper.get_tags([str(resolved.path)], ["FileType"])
        assert found[0].get("File:FileType") == "AVIF"
    finally:
        resolver.release(resolved)
    assert (avif.read_bytes(), avif.stat().st_mtime_ns) == before
    assert sorted(os.listdir(library)) == ["IMG_2.AVIF", "IMG_2.MOV"]
