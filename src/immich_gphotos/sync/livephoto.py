"""Mux an Apple Live Photo (still + `.MOV`) into a Google Motion Photo.

In Immich an Apple Live Photo arrives as two paired assets: the primary
still (`.HEIC`/`.JPG`, `visibility: "timeline"`, carrying
`livePhotoVideoId`) and the companion motion (`.MOV`, `visibility:
"hidden"`). Uploading only the still loses the motion; uploading the `.MOV`
separately litters the timeline with 1.5-3s clips Google never pairs back.

This muxes the pair into a single file following the [Motion Photo format
1.0](https://developer.android.com/media/platform/motion-photo-format):
a primary still with Camera + Container XMP describing one `Primary`
image item and one `MotionPhoto` video item, the video bytes appended
after the still. Google Photos recognises the structure and renders it as
an interactive Motion Photo with sound.

Two still containers are supported, matching the two iPhone originals in
the wild, through two different mechanisms:

- JPEG: pure Python -- the XMP goes in a new APP1 segment after SOI, the
  video is appended after EOI. No dependencies.
- HEIC/AVIF: the XMP is written into the file's `meta` by ExifTool (via
  `sync.exif_xmp`, which owns `iinf`/`iloc` for every version in the wild),
  then the video is appended in an `mpvd` box. The XMP shape and the
  Samsung trailer (`sefd` + `MotionPhoto_Data` offsets) follow the recipe
  of MotionPhoto2 (PetrVys/MotionPhoto2, MIT), the established tool for
  exactly this conversion, whose field layout is mirrored here with credit
  rather than re-derived. ExifTool is optional: without the binary, HEIC
  stills fall back to still-only (JPEG is unaffected).

Anything else -- a still that is neither JPEG nor HEIC/AVIF, an ExifTool
failure of any kind, an empty/oversized/unrecognised video -- falls back
to still-only upload, as before, rather than producing a suspect file.
The old `GCamera:MicroVideo` attributes are deliberately not written:
deleted in 1.0, readers must ignore them.
"""

import base64
import hashlib
import os
import struct
import xml.etree.ElementTree as ET
from pathlib import Path

# Set when the companion `.MOV` was folded into its still's upload, so the
# hidden clip is accounted for instead of sitting in `ineligible: hidden`
# forever alongside thousands of identical rows.
LIVE_PHOTO_PAIRED_REASON = "live_photo_paired"

_JPEG_SOI = b"\xff\xd8"
_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"

_CAMERA_NS = "http://ns.google.com/photos/1.0/camera/"
_CONTAINER_NS = "http://ns.google.com/photos/1.0/container/"
_ITEM_NS = "http://ns.google.com/photos/1.0/container/item/"
_RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

# Cap the motion clip we will fold in. Apple Live clips are ~1.5-3s
# (a few MB); anything far larger is not a Live clip and muxing it would
# bloat scratch and the upload for no motion-photo benefit.
MAX_VIDEO_BYTES = 64 * 1024 * 1024

# Cap the still we will mux. Real Live Photo stills are a few MB; this only
# exists so a mislabelled multi-gigabyte file is refused by a stat() before
# anything is read, rather than streamed into scratch for nothing.
MAX_STILL_BYTES = 256 * 1024 * 1024

# Muxing streams in chunks of this size, so memory per worker thread is
# bounded by a constant rather than by the size of the media it handles.
_CHUNK = 1024 * 1024

# Live Photo backup strategies (`Settings.live_photo_mode`).
#   still_only_legacy  upload the still unchanged and skip the hidden .MOV --
#                      the upstream behaviour, and the default so an upgrade
#                      never changes what an existing account uploads. The
#                      motion is recorded as *not* backed up.
#   original_pair      back up the original still and the original .MOV as
#                      two separate Google items, each under the SHA-1 of its
#                      own unmodified bytes. Google shows them as two items;
#                      it is not a native linked Live Photo.
#   derived_motion     (explicit opt-in) build a Google Motion Photo from the
#                      two originals in scratch and upload that derived file;
#                      the originals are never modified.
LIVE_PHOTO_MODES = ("still_only_legacy", "original_pair", "derived_motion")
DEFAULT_LIVE_PHOTO_MODE = "still_only_legacy"

# Per-Live-Photo completeness (`live_photo.status`).
STATUS_NOT_CONFIGURED = "not_configured"  # mode does not back the motion up
STATUS_PENDING = "pending"  # motion still to be backed up under this mode
STATUS_COMPLETE = "complete"  # motion verifiably backed up (MOV item or Motion Photo)
STATUS_FAILED = "failed"  # a permanent reason; only an explicit reprocess retries
STATUS_DEFERRED = "deferred"  # gave up for now (or original already present); reprocess retries

# Sync note on a still whose motion is not in Google (see bytes.TIMESTAMP_UNVERIFIED).
MOTION_NOT_BACKED_UP = "motion_not_backed_up"


def is_jpeg(data: bytes) -> bool:
    """True when `data` starts with the JPEG SOI marker."""
    return data[:2] == _JPEG_SOI


def looks_like_video(data: bytes) -> bool:
    """True for bytes plausibly an MP4/MOV container.

    Checks the first 32 bytes for an `ftyp`, `moov`, `mdat`, `free` or
    `wide` box -- enough to refuse muxing garbage while accepting every
    real `.MOV`/`.MP4` companion. Anything else falls back to still-only
    rather than producing a file Google would reject.
    """
    head = data[:32]
    return any(tag in head for tag in (b"ftyp", b"moov", b"mdat", b"free", b"wide"))


def _sha1_b64(digest: "hashlib._Hash") -> str:
    return base64.b64encode(digest.digest()).decode("ascii")


def _read_head(path: Path, n: int = 4096) -> bytes:
    with path.open("rb") as handle:
        return handle.read(n)


def check_video_file(path: Path) -> int:
    """Reject a companion clip that must not be muxed, by stat + header only.

    Returns its size. Raises ValueError (permanent: muxing would never work)
    or OSError (unreadable right now). Never reads the whole clip.
    """
    size = path.stat().st_size
    if size == 0:
        raise ValueError("companion video is empty")
    if size > MAX_VIDEO_BYTES:
        raise ValueError(f"companion video too large: {size} bytes")
    if not looks_like_video(_read_head(path, 32)):
        raise ValueError("companion bytes do not look like MP4/MOV")
    return size


def check_still_file(path: Path) -> int:
    size = path.stat().st_size
    if size == 0:
        raise ValueError("still is empty")
    if size > MAX_STILL_BYTES:
        raise ValueError(f"still too large to mux: {size} bytes")
    return size


def still_kind(path: Path) -> str | None:
    """ "jpeg", "heic", "avif" or None, from the first few KiB only."""
    head = _read_head(path)
    if is_jpeg(head):
        return "jpeg"
    return isobmff_image_kind(head)


def _copy_into(dest, path: Path, digest, skip: int = 0) -> None:  # noqa: ANN001 - file + hashlib
    with path.open("rb") as src:
        if skip:
            src.seek(skip)
        for chunk in iter(lambda: src.read(_CHUNK), b""):
            dest.write(chunk)
            digest.update(chunk)


def sha1_b64_of_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return _sha1_b64(digest)


def _jpeg_has_eoi(path: Path) -> bool:
    tail = b""
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            if b"\xff\xd9" in tail + chunk:
                return True
            tail = chunk[-1:]
    return False


def write_motion_photo_jpeg(still: Path, video: Path, dest: Path, video_mime: str) -> str:
    """Stream-build a JPEG Motion Photo at `dest`; returns its base64 SHA-1.

    Same bytes as `build_motion_photo_jpeg(still.read_bytes(), ...)`, but in
    bounded memory: stat + header checks first, then the still and video are
    copied through in chunks. Raises ValueError for anything that must fall
    back to still-only, OSError when a source is unreadable; `dest` is
    removed on any failure (it is only ever a scratch path).
    """
    check_still_file(still)
    if not is_jpeg(_read_head(still, 2)):
        raise ValueError("still is not a JPEG")
    video_size = check_video_file(video)
    if not _jpeg_has_eoi(still):
        raise ValueError("still JPEG has no EOI marker")
    segment = _app1_segment(_xmp_packet(video_size, video_mime))
    digest = hashlib.sha1()
    try:
        with dest.open("wb") as out:
            head = _JPEG_SOI + segment
            out.write(head)
            digest.update(head)
            _copy_into(out, still, digest, skip=2)
            _copy_into(out, video, digest)
        if os.stat(video).st_size != video_size:
            raise OSError("companion video changed while muxing")
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return _sha1_b64(digest)


def append_heic_trailer(dest: Path, video: Path, video_size: int) -> None:
    """Append the mpvd/sefd trailer to an XMP-stamped HEIC at `dest`, streaming the video."""
    image_size = dest.stat().st_size
    prefix, suffix = heic_trailer_parts(video_size, image_size)
    with dest.open("ab") as out:
        out.write(prefix)
        with video.open("rb") as src:
            copied = 0
            for chunk in iter(lambda: src.read(_CHUNK), b""):
                out.write(chunk)
                copied += len(chunk)
        if copied != video_size:
            raise OSError("companion video changed while muxing")
        out.write(suffix)


def check_video_bytes(video_bytes: bytes) -> None:
    """Reject a companion clip that must not be muxed (raises ValueError)."""
    if not video_bytes:
        raise ValueError("companion video is empty")
    if len(video_bytes) > MAX_VIDEO_BYTES:
        raise ValueError(f"companion video too large: {len(video_bytes)} bytes")
    if not looks_like_video(video_bytes):
        raise ValueError("companion bytes do not look like MP4/MOV")


def _xmp_packet(
    video_length: int, video_mime: str, image_mime: str = "image/jpeg", padding: int | None = None
) -> bytes:
    """The Camera + Container XMP describing one still + one motion item.

    `video_length` is the exact byte length of the appended video (the
    secondary item's `Length`); the primary item's length is 0 by convention
    (readers parse the still to find its end). `padding` is the ISOBMFF-only
    primary-item `Padding`: 8 for HEIC/AVIF (the `mpvd` header sitting
    between the image and the video), omitted for JPEG. Presentation
    timestamp is -1 (unset), so readers use the middle of the video track.
    """
    padding_attr = f' Item:Padding="{padding}"' if padding is not None else ""
    xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description rdf:about="" xmlns:Camera="{_CAMERA_NS}" '
        'Camera:MotionPhoto="1" Camera:MotionPhotoVersion="1" '
        'Camera:MotionPhotoPresentationTimestampUs="-1"/>'
        f'<rdf:Description rdf:about="" xmlns:Container="{_CONTAINER_NS}">'
        "<Container:Directory>"
        '<rdf:Seq xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:li rdf:parseType="Resource" xmlns:Item="{_ITEM_NS}" '
        f'Item:Mime="{image_mime}" Item:Semantic="Primary" Item:Length="0"{padding_attr}/>'
        f'<rdf:li rdf:parseType="Resource" xmlns:Item="{_ITEM_NS}" '
        f'Item:Mime="{video_mime}" Item:Semantic="MotionPhoto" '
        f'Item:Length="{video_length}"/>'
        "</rdf:Seq>"
        "</Container:Directory>"
        "</rdf:Description>"
        "</rdf:RDF>"
        "</x:xmpmeta>"
    )
    return xml.encode("utf-8")


def _app1_segment(packet: bytes) -> bytes:
    """One JPEG APP1 segment carrying `packet` (header + XMP).

    Segment layout: `FF E1 LEN(2 bytes BE, including the 2 length bytes)`
    + payload. LEN must fit in 16 bits; our packet is ~1KB, far below the
    ~64KB segment limit, so no extended-XMP chunking is needed.
    """
    payload = _XMP_HEADER + packet
    return b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload


def build_motion_photo_jpeg(
    still_jpeg: bytes, video_bytes: bytes, video_mime: str = "video/quicktime"
) -> bytes:
    """Return `still_jpeg` with Container/Camera XMP inserted and `video_bytes` appended.

    The XMP APP1 is inserted immediately after the SOI marker (APP segments
    must precede the scan data), leaving the original image bytes -- EXIF
    included -- untouched; any viewer ignoring XMP still sees the exact
    original still. Raises `ValueError` for anything that should fall back
    to still-only instead of producing a suspect file.
    """
    if not is_jpeg(still_jpeg):
        raise ValueError("still is not a JPEG")
    check_video_bytes(video_bytes)
    if b"\xff\xd9" not in still_jpeg:
        raise ValueError("still JPEG has no EOI marker")
    packet = _xmp_packet(len(video_bytes), video_mime)
    segment = _app1_segment(packet)
    still_with_xmp = still_jpeg[:2] + segment + still_jpeg[2:]
    return still_with_xmp + video_bytes


def video_mime_for_filename(filename: str) -> str:
    """Container MIME for the secondary item: QuickTime for `.MOV` (Apple
    Live clips), MP4 otherwise."""
    return "video/quicktime" if filename.lower().endswith(".mov") else "video/mp4"


# ---------------------------------------------------------------------------
# ISOBMFF (HEIC/AVIF) Motion Photos via ExifTool
#
# The XMP packet below mirrors MotionPhoto2's template (PetrVys/
# MotionPhoto2, MIT): one rdf:Description carrying the GCamera motion
# attributes and the Container directory, with the namespaces readers of
# real-world files expect. ExifTool writes it into the file's `meta`
# (owning `iinf`/`iloc` for every version in the wild); this module only
# builds the packet, merges the still's own XMP, and appends the trailer.
# ---------------------------------------------------------------------------

_HEIC_BRANDS = frozenset({"heic", "heix", "hevc", "hevx", "heim", "heis", "hevm", "hevs"})
_AVIF_BRANDS = frozenset({"avif", "avis"})
_GENERIC_HEIF_BRANDS = frozenset({"mif1", "msf1"})

# Namespaces declared on the Motion Photo description. HDRGainMap rides
# along because real HDR HEICs carry it and dropping declarations while
# merging would orphan prefixed content copied over from the original.
_HEIC_XMP_NAMESPACES = {
    "x": "adobe:ns:meta/",
    "rdf": _RDF_NS,
    "GCamera": _CAMERA_NS,
    "Container": _CONTAINER_NS,
    "Item": _ITEM_NS,
    "HDRGainMap": "http://ns.apple.com/HDRGainMap/1.0/",
}

# Samsung trailer ("mpv2/mpv3") field layout, ported from MotionPhoto2's
# SamsungTags (MIT): tag ids in emission order, SEFH version, box names.
# Only the two tags this flow ever emits are carried; the construction is
# otherwise verbatim, including the intentionally mixed endianness.
_SAMSUNG_TAG_ORDER = (
    ("MotionPhoto_Data", b"\x00\x00\x30\x0a"),
    ("MotionPhoto_Version", b"\x00\x00\x31\x0a"),
)
_SAMSUNG_SEFH_VERSION = 107
_MPVD_BOX = b"mpvd"
_SEFD_BOX = b"sefd"
_BOX_HEADER_LEN = 8


def _read_box(data: bytes, pos: int) -> tuple[bytes, int, int, int]:
    """Parse one box header at `pos`: (type, box_start, box_size, header_len)."""
    if pos + 8 > len(data):
        raise ValueError("truncated box header")
    size = struct.unpack_from(">I", data, pos)[0]
    box_type = data[pos + 4 : pos + 8]
    if size == 1:
        if pos + 16 > len(data):
            raise ValueError("truncated largesize")
        size = struct.unpack_from(">Q", data, pos + 8)[0]
        header_len = 16
    elif size == 0:
        size = len(data) - pos
        header_len = 8
    else:
        header_len = 8
        if size < 8:
            raise ValueError(f"impossible box size {size} for {box_type!r}")
    if size > len(data) - pos:
        raise ValueError(f"{box_type!r} box overruns its buffer")
    return box_type, pos, size, header_len


def isobmff_image_kind(data: bytes) -> str | None:
    """`"heic"`, `"avif"`, or None for bytes that are not a still we can mux.

    Reads only the `ftyp` brands: a HEIC-family (or generic-HEIF-with-HEIC
    compatible) major/compatible brand means HEIC, an AVIF brand means AVIF.
    Anything unparseable -- or a video `ftyp` like `mp42`/`qt` -- is None.
    """
    try:
        box_type, _, size, header_len = _read_box(data, 0)
    except ValueError:
        return None
    if box_type != b"ftyp" or size < header_len + 8:
        return None
    payload = data[header_len:size]
    major = payload[0:4].decode("latin1")
    compat = {payload[i : i + 4].decode("latin1") for i in range(4, len(payload) - 3, 4)}
    if major in _HEIC_BRANDS or (major in _GENERIC_HEIF_BRANDS and compat & _HEIC_BRANDS):
        return "heic"
    if major in _AVIF_BRANDS:
        return "avif"
    return None


def image_mime_for_kind(kind: str) -> str:
    """Primary-item MIME for an ISOBMFF still kind."""
    return "image/avif" if kind == "avif" else "image/heic"


def heic_xmp_packet(
    video_data_length: int, video_mime: str, image_mime: str, keyframe_us: int | None
) -> bytes:
    """The Motion Photo XMP packet ExifTool will write into the still.

    Shape mirrors MotionPhoto2's template: one description, `Padding="8"`
    on the primary item and `"0"` on the motion item, `Length` covering the
    motion bytes. `keyframe_us=None` writes -1 (unset), which the spec
    allows and readers resolve to the middle of the track.
    """
    timestamp = "-1" if keyframe_us is None else str(keyframe_us)
    xml = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" '
        f'xmlns:GCamera="{_CAMERA_NS}" '
        f'xmlns:Container="{_CONTAINER_NS}" '
        f'xmlns:Item="{_ITEM_NS}" '
        'xmlns:HDRGainMap="http://ns.apple.com/HDRGainMap/1.0/" '
        'GCamera:MotionPhoto="1" '
        'GCamera:MotionPhotoVersion="1" '
        f'GCamera:MotionPhotoPresentationTimestampUs="{timestamp}">'
        "<Container:Directory>"
        "<rdf:Seq>"
        '<rdf:li rdf:parseType="Resource">'
        f'<Container:Item Item:Mime="{image_mime}" Item:Semantic="Primary" '
        'Item:Length="0" Item:Padding="8"/>'
        "</rdf:li>"
        '<rdf:li rdf:parseType="Resource">'
        f'<Container:Item Item:Mime="{video_mime}" Item:Semantic="MotionPhoto" '
        f'Item:Length="{video_data_length}" Item:Padding="0"/>'
        "</rdf:li>"
        "</rdf:Seq>"
        "</Container:Directory>"
        "</rdf:Description>"
        "</rdf:RDF>"
        "</x:xmpmeta>"
    )
    return xml.encode("utf-8")


def merge_xmp(original: str | None, ours: bytes) -> bytes:
    """Fold the still's own XMP into our Motion Photo packet.

    Writing through ExifTool *replaces* the still's XMP, so Apple's own
    metadata must be carried over or it is destroyed. Every non-Directory
    child and every Description attribute of the original is copied (the
    Directory is ours -- never duplicated), mirroring MotionPhoto2's
    merge_xmp. Garbage in means ours unchanged: never fail a backup over
    metadata.
    """
    try:
        ours_root = ET.fromstring(ours)
    except ET.ParseError:
        return ours
    if not original:
        return ours
    try:
        orig_root = ET.fromstring(original)
    except ET.ParseError:
        return ours

    def description_of(root: ET.Element) -> ET.Element | None:
        return next(root.iter(f"{{{_RDF_NS}}}Description"), None)

    ours_desc = description_of(ours_root)
    orig_desc = description_of(orig_root)
    if ours_desc is None or orig_desc is None:
        return ours
    directory_tag = f"{{{_CONTAINER_NS}}}Directory"
    for child in list(orig_desc):
        if child.tag == directory_tag:
            continue
        ours_desc.append(child)
    for key, value in orig_desc.attrib.items():
        ours_desc.set(key, value)
    for prefix, uri in _HEIC_XMP_NAMESPACES.items():
        ET.register_namespace(prefix, uri)
    return ET.tostring(ours_root, encoding="utf-8")


def heic_trailer_parts(video_length: int, image_size: int) -> tuple[bytes, bytes]:
    """`heic_trailer` split around the video: (bytes before it, bytes after it).

    Lets the trailer be written with the clip streamed in between, so it is
    never held in memory. `prefix + video + suffix == heic_trailer(video, image_size)`.
    """
    mpv2 = b"mpv2" + struct.pack(">i", image_size + _BOX_HEADER_LEN) + struct.pack(">i", video_length)
    tags = {"MotionPhoto_Data": mpv2, "MotionPhoto_Version": b"mpv3"}
    tag_data = b""
    tag_offsets: dict[str, int] = {}
    tag_lengths: dict[str, int] = {}
    for name, tag_id in _SAMSUNG_TAG_ORDER:
        field = tag_id + struct.pack("<i", len(name)) + name.encode("utf-8") + tags[name]
        tag_data += field
        tag_lengths[name] = len(field)
        for preceding, _ in _SAMSUNG_TAG_ORDER:
            tag_offsets[preceding] = len(field) + (tag_offsets.get(preceding) or 0)
            if preceding == name:
                break
    sefh = b"SEFH" + struct.pack("<i", _SAMSUNG_SEFH_VERSION) + struct.pack("<i", len(tags))
    for name, tag_id in _SAMSUNG_TAG_ORDER:
        sefh += tag_id + struct.pack("<i", tag_offsets[name]) + struct.pack("<i", tag_lengths[name])
    sefh_len = len(sefh)
    sefh += struct.pack("<i", sefh_len) + b"SEFT"
    suffix = struct.pack(">i", len(tag_data) + len(sefh) + _BOX_HEADER_LEN) + _SEFD_BOX + tag_data + sefh
    total = _BOX_HEADER_LEN + video_length + len(suffix)
    return struct.pack(">i", total) + _MPVD_BOX, suffix


def heic_trailer_length_for(video_length: int) -> int:
    """`heic_trailer_length` from the clip's size alone (no bytes needed)."""
    prefix, suffix = heic_trailer_parts(video_length, 0)
    return len(prefix) + video_length + len(suffix) - _BOX_HEADER_LEN


def heic_trailer(video_bytes: bytes, image_size: int) -> bytes:
    """The bytes appended after an XMP-stamped HEIC/AVIF still.

    Ported from MotionPhoto2's SamsungTags.video_footer (MIT): the video
    wrapped with an `mpvd` box, followed by a `sefd` box carrying the
    `MotionPhoto_Data` (mpv2 offset+size, locating the video past the
    image) and `MotionPhoto_Version` tags plus the SEFH/SEFT index. Lengths
    (but not contents) are image_size-invariant, so the XMP `Length` can be
    computed before the stamped image size is known.
    """
    mpv2 = b"mpv2" + struct.pack(">i", image_size + _BOX_HEADER_LEN) + struct.pack(">i", len(video_bytes))
    tags = {"MotionPhoto_Data": mpv2, "MotionPhoto_Version": b"mpv3"}
    tag_data = b""
    tag_offsets: dict[str, int] = {}
    tag_lengths: dict[str, int] = {}
    for name, tag_id in _SAMSUNG_TAG_ORDER:
        field = tag_id + struct.pack("<i", len(name)) + name.encode("utf-8") + tags[name]
        tag_data += field
        tag_lengths[name] = len(field)
        for preceding, _ in _SAMSUNG_TAG_ORDER:
            tag_offsets[preceding] = len(field) + (tag_offsets.get(preceding) or 0)
            if preceding == name:
                break
    sefh = b"SEFH" + struct.pack("<i", _SAMSUNG_SEFH_VERSION) + struct.pack("<i", len(tags))
    for name, tag_id in _SAMSUNG_TAG_ORDER:
        sefh += tag_id + struct.pack("<i", tag_offsets[name]) + struct.pack("<i", tag_lengths[name])
    sefh_len = len(sefh)
    sefh += struct.pack("<i", sefh_len) + b"SEFT"
    result = video_bytes + struct.pack(">i", len(tag_data) + len(sefh) + _BOX_HEADER_LEN) + _SEFD_BOX
    result += tag_data + sefh
    return struct.pack(">i", len(result) + _BOX_HEADER_LEN) + _MPVD_BOX + result


def heic_trailer_length(video_bytes: bytes) -> int:
    """`len(heic_trailer(...)) - 8`: the XMP motion-item `Length`.

    Image_size-invariant (only the embedded mpv2 offset *value* moves),
    so callers compute it before the stamped image size exists.
    """
    return len(heic_trailer(video_bytes, 0)) - _BOX_HEADER_LEN
