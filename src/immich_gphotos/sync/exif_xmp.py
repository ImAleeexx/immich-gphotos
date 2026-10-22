"""XMP for HEIC/AVIF Motion Photos, written by ExifTool.

Inserting XMP into an ISOBMFF `meta` box by hand means re-encoding `iinf`
and `iloc` for every version in the wild and shifting every file offset
past the grown box -- exactly the kind of byte surgery a backup tool
should not own. ExifTool does it correctly for every real-world file, so
this module delegates the read/merge/write half of HEIC muxing to it and
keeps only the Motion Photo specifics (packet building, merging, trailer
appending) in Python -- the same division of labour as MotionPhoto2
(PetrVys/MotionPhoto2, MIT), whose battle-tested recipe this follows.

ExifTool is an *optional* runtime dependency: it is only ever invoked on
the HEIC/AVIF Live Photo path. JPEG muxing is pure Python and never
touches it, and a missing binary (or any ExifTool failure) degrades to
still-only upload rather than failing the backup. Unit tests run entirely
against `FakeMotionPhotoXmp`; the real binary is exercised only by the
env-gated live test (`tests/sync/test_exiftool_live.py`).
"""

import logging
import shutil
import threading
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class MotionPhotoXmp(Protocol):
    """The three ExifTool operations HEIC muxing needs. All best-effort."""

    def keyframe_us(self, video: Path) -> int | None:
        """Presentation timestamp in microseconds, or None when unknown.

        Migrated from the Live Photo clip so the motion starts on the same
        keyframe; None means "unset" and readers fall back to the middle of
        the track (the spec allows -1 for exactly this).
        """
        ...

    def original_xmp(self, image: Path) -> str | None:
        """The still's own XMP packet, if it has one, for merging."""
        ...

    def write_xmp(self, image: Path, packet: bytes) -> None:
        """Write `packet` as `image`'s XMP, in place.

        Only ever called with staged scratch copies -- never Immich's own
        originals. Raises on failure; callers treat that as "still-only".
        """
        ...


class ExifToolMotionPhotoXmp:
    """`MotionPhotoXmp` backed by a persistent ExifTool process per thread.

    One `stay_open` daemon per thread (same shape as `GpmcClient`'s
    per-thread client): ExifTool's ~0.5s Perl startup is paid once per
    worker thread, not once per photo, which matters at 8,000 pairs.
    Daemons are never shut down explicitly; they die with the process.
    """

    def __init__(self) -> None:
        if shutil.which("exiftool") is None:
            raise RuntimeError("exiftool binary not found on PATH")
        self._local = threading.local()

    @classmethod
    def if_available(cls) -> "ExifToolMotionPhotoXmp | None":
        """An instance, or None (with one warning) when there is no binary."""
        try:
            return cls()
        except RuntimeError as exc:
            logger.warning("HEIC Live Photos will upload still-only: %s", exc)
            return None

    @property
    def _helper(self):  # noqa: ANN202 - PyExifTool is an optional import
        helper = getattr(self._local, "helper", None)
        if helper is None:
            import exiftool  # lazy: JPEG-only installs never pay for this import

            helper = exiftool.ExifToolHelper()
            self._local.helper = helper
        return helper

    def keyframe_us(self, video: Path) -> int | None:
        try:
            found = self._helper.get_tags([str(video)], ["QuickTime:StillImageTime"], params=["-n"])
        except Exception as exc:  # noqa: BLE001 - unknown timestamp degrades to -1, never fails
            logger.debug("could not read StillImageTime from %s: %s", video.name, exc)
            return None
        try:
            for key, value in found[0].items():
                if key.endswith(":StillImageTime"):
                    return int(float(value) * 1_000_000)
        except (ValueError, OverflowError, IndexError):
            pass
        return None

    def original_xmp(self, image: Path) -> str | None:
        try:
            out = self._helper.execute("-XMP", "-b", str(image))
        except Exception as exc:  # noqa: BLE001 - missing original XMP just means nothing to merge
            logger.debug("could not read XMP from %s: %s", image.name, exc)
            return None
        return out or None

    def write_xmp(self, image: Path, packet: bytes) -> None:
        sidecar = image.with_suffix(image.suffix + ".xmp")
        try:
            sidecar.write_bytes(packet)
            out = self._helper.execute(
                "-overwrite_original", "-tagsfromfile", str(sidecar), "-xmp", str(image)
            )
            if "1 image files updated" not in out:
                raise RuntimeError(f"exiftool refused the write: {out.strip() or 'no output'}")
        finally:
            sidecar.unlink(missing_ok=True)


class FakeMotionPhotoXmp:
    """In-memory `MotionPhotoXmp` for tests. Records every write."""

    def __init__(self, keyframe_us: int | None = None, original_xmp: str | None = None) -> None:
        self.keyframe_us_value = keyframe_us
        self.original_xmp_value = original_xmp
        self.writes: list[tuple[str, bytes]] = []

    def keyframe_us(self, video: Path) -> int | None:  # noqa: ARG002
        return self.keyframe_us_value

    def original_xmp(self, image: Path) -> str | None:  # noqa: ARG002
        return self.original_xmp_value

    def write_xmp(self, image: Path, packet: bytes) -> None:
        # Faithful enough for the engine tests: the packet is recorded for
        # assertions, and the file is left for the trailer append -- exactly
        # what the real writer leaves behind (a still carrying the packet).
        self.writes.append((str(image), packet))
