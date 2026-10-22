import struct
import xml.etree.ElementTree as ET

import pytest

from immich_gphotos.sync import livephoto

# Minimal valid JPEG: SOI + APP0 (JFIF) + EOI. Real stills are larger; the
# muxer only needs SOI ... EOI with an insertion point after SOI.
STILL = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32 + b"\xff\xd9"
# Minimal MP4/MOV-looking bytes: size + 'ftyp' + payload.
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def _app1_packets(muxed: bytes) -> list[bytes]:
    """Every APP1 payload between SOI and the original still's third byte."""
    assert muxed[:2] == b"\xff\xd8"
    packets = []
    pos = 2
    while muxed[pos : pos + 2] == b"\xff\xe1":
        length = struct.unpack(">H", muxed[pos + 2 : pos + 4])[0]
        packets.append(muxed[pos + 4 : pos + 2 + length])
        pos += 2 + length
    return packets


def test_muxed_file_is_still_then_video_with_xmp_between():
    muxed = livephoto.build_motion_photo_jpeg(STILL, VIDEO)

    # SOI, then our APP1, then the original still bytes verbatim, then video.
    assert muxed[:2] == b"\xff\xd8"
    packets = _app1_packets(muxed)
    assert len(packets) == 1
    assert packets[0].startswith(b"http://ns.adobe.com/xap/1.0/\x00")
    xmp = packets[0].split(b"\x00", 1)[1].decode()
    assert 'Camera:MotionPhoto="1"' in xmp
    assert 'Camera:MotionPhotoVersion="1"' in xmp
    assert f'Item:Length="{len(VIDEO)}"' in xmp
    assert 'Item:Semantic="MotionPhoto"' in xmp
    assert 'Item:Semantic="Primary"' in xmp
    # Original still intact after our segment, video appended at the end.
    assert STILL[2:] in muxed
    assert muxed.endswith(VIDEO)
    assert muxed.index(STILL[2:]) < muxed.index(VIDEO)


def test_muxed_file_keeps_an_existing_exif_segment():
    packets = _app1_packets(livephoto.build_motion_photo_jpeg(STILL, VIDEO))
    assert len(packets) == 1  # ours, inserted first; the JFIF APP0 follows untouched


def test_still_must_be_a_jpeg():
    with pytest.raises(ValueError):
        livephoto.build_motion_photo_jpeg(b"not a jpeg", VIDEO)


def test_video_must_not_be_empty_or_huge_or_garbage():
    with pytest.raises(ValueError):
        livephoto.build_motion_photo_jpeg(STILL, b"")
    with pytest.raises(ValueError):
        livephoto.build_motion_photo_jpeg(STILL, b"x" * (livephoto.MAX_VIDEO_BYTES + 1))
    with pytest.raises(ValueError):
        livephoto.build_motion_photo_jpeg(STILL, b"garbage" * 16)


def test_video_mime_follows_the_companion_extension():
    assert livephoto.video_mime_for_filename("IMG_1.MOV") == "video/quicktime"
    assert livephoto.video_mime_for_filename("clip.mp4") == "video/mp4"
    xmp = livephoto._xmp_packet(10, "video/quicktime").decode()
    assert 'Item:Mime="video/quicktime"' in xmp


# ---------------------------------------------------------------------------
# HEIC/AVIF Motion Photos (ExifTool writes the XMP; Python appends trailer)
# ---------------------------------------------------------------------------


def test_heic_detection_by_brand():
    heic = b"\x00\x00\x00\x1cftypheic\x00\x00\x00\x00heic" + b"\x00" * 16
    assert livephoto.isobmff_image_kind(heic) == "heic"
    assert livephoto.isobmff_image_kind(STILL) is None
    assert livephoto.isobmff_image_kind(VIDEO) is None
    assert livephoto.isobmff_image_kind(b"junk") is None
    assert livephoto.isobmff_image_kind(heic.replace(b"heic", b"avif", 1)) == "avif"
    assert livephoto.image_mime_for_kind("heic") == "image/heic"
    assert livephoto.image_mime_for_kind("avif") == "image/avif"


def test_heic_xmp_packet_shape_matches_the_established_template():
    packet = livephoto.heic_xmp_packet(404, "video/quicktime", "image/heic", 1500000).decode()
    assert 'Camera:MotionPhoto="1"' in packet
    assert 'Camera:MotionPhotoVersion="1"' in packet
    assert 'Camera:MotionPhotoPresentationTimestampUs="1500000"' in packet
    assert 'Item:Mime="image/heic"' in packet
    assert 'Item:Mime="video/quicktime"' in packet
    assert 'Item:Semantic="Primary"' in packet
    assert 'Item:Semantic="MotionPhoto"' in packet
    assert 'Item:Length="404"' in packet
    assert 'Item:Padding="8"' in packet
    assert 'Item:Padding="0"' in packet
    assert 'xmlns:HDRGainMap="http://ns.apple.com/HDRGainMap/1.0/"' in packet
    # Unknown keyframe degrades to the spec's unset value.
    assert (
        'MotionPhotoPresentationTimestampUs="-1"'
        in livephoto.heic_xmp_packet(404, "video/quicktime", "image/heic", None).decode()
    )
    # The packet must parse as XML -- ExifTool rejects malformed sidecars.
    ET.fromstring(livephoto.heic_xmp_packet(404, "video/mp4", "image/avif", None))


def test_merge_xmp_preserves_the_original_and_never_duplicates_the_directory():
    original = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:tiff="http://ns.adobe.com/tiff/1.0/" tiff:Make="Apple">'
        "<tiff:Model>iPhone</tiff:Model>"
        '<Container:Directory xmlns:Container="http://ns.google.com/photos/1.0/container/">stale</Container:Directory>'
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
    )
    ours = livephoto.heic_xmp_packet(10, "video/quicktime", "image/heic", None)
    merged = livephoto.merge_xmp(original, ours).decode()
    assert "Apple" in merged and "iPhone" in merged  # original children carried over
    assert merged.count("Container:Directory") == 2  # ours only: open + close tags
    assert 'Camera:MotionPhoto="1"' in merged  # ours intact


def test_merge_xmp_tolerates_missing_or_broken_originals():
    ours = livephoto.heic_xmp_packet(10, "video/quicktime", "image/heic", None)
    assert livephoto.merge_xmp(None, ours) == ours
    assert livephoto.merge_xmp("not xml at all", ours) == ours
    assert livephoto.merge_xmp("<rdf:RDF/>", ours) == ours


def test_heic_trailer_wraps_video_in_mpvd_with_a_sefd_index():
    trailer = livephoto.heic_trailer(VIDEO, image_size=1000)
    # mpvd box wraps everything; sefd box wraps the Samsung tags.
    assert trailer[:4] == struct.pack(">I", len(trailer))
    assert trailer[4:8] == b"mpvd"
    assert trailer.endswith(b"SEFT")
    assert b"SEFH" in trailer and b"sefd" in trailer
    assert b"MotionPhoto_Data" in trailer and b"mpv3" in trailer
    # The embedded mpv2 offset locates the video past the image + mpvd header.
    assert struct.pack(">i", 1000 + 8) in trailer
    assert struct.pack(">i", len(VIDEO)) in trailer
    # The video bytes ride inside, byte-identical.
    assert VIDEO in trailer
    # Length is image_size-invariant (only the embedded offset value moves).
    assert livephoto.heic_trailer_length(VIDEO) == len(livephoto.heic_trailer(VIDEO, 99999)) - 8
    assert livephoto.heic_trailer_length(VIDEO) == len(trailer) - 8
