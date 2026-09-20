import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from immich_gphotos.immich.protocol import ImmichClient
from immich_gphotos.models import Asset

logger = logging.getLogger(__name__)


def _stamp_capture_date(path: Path, taken_at: str | None) -> None:
    """Set `path`'s mtime to when the asset was actually taken.

    gpmc uploads a file's `st_mtime` to Google as the capture timestamp, and
    Google honours it for any file whose bytes carry no date of their own --
    WhatsApp videos, screenshots, anything stripped of EXIF. Immich already
    stores its own originals with the capture date as the mtime, so the
    direct-read path was always correct; a file we download through the API
    lands with an mtime of "now", which is what put those assets in Google
    Photos dated today instead of when they were taken.

    Best-effort by design: a missing, malformed or unrepresentable date leaves
    the file alone and the asset still uploads, dated by Google as before. A
    timestamp is never worth failing a backup over.
    """
    if not taken_at:
        return
    try:
        when = datetime.fromisoformat(taken_at.replace("Z", "+00:00")).timestamp()
        os.utime(path, (when, when))
    except (ValueError, OSError, OverflowError) as exc:
        logger.debug("could not stamp capture date %r onto %s: %s", taken_at, path.name, exc)


@dataclass(frozen=True)
class ResolvedBytes:
    path: Path
    temporary: bool


class ByteResolver:
    """Get at an asset's bytes by the cheapest route available.

    When the container can see Immich's library volume, `originalPath` is handed
    straight to gpmc: no copy, no scratch space, no traffic through the API.
    Otherwise the original is downloaded to scratch and deleted afterwards.
    """

    def __init__(self, immich: ImmichClient, scratch: Path, allow_direct: bool = True) -> None:
        self._immich = immich
        self._scratch = Path(scratch)
        self._allow_direct = allow_direct

    def resolve(self, asset: Asset) -> ResolvedBytes:
        if self._allow_direct and asset.original_path:
            candidate = Path(asset.original_path)
            if candidate.is_file():
                return ResolvedBytes(path=candidate, temporary=False)
        self._scratch.mkdir(parents=True, exist_ok=True)
        # The nonce guarantees a fresh path per call: two concurrent resolutions of
        # the *same* asset (e.g. a slow-but-healthy upload that crosses
        # requeue_stale_uploading's liveness-free age threshold and gets handed to a
        # second worker) must never share a scratch file, or one caller's release()
        # could delete bytes the other is still writing or uploading. Only the
        # basename of the filename is used: `originalFileName` comes from Immich
        # unsanitized, and a separator in it must not turn into a subdirectory.
        nonce = uuid.uuid4().hex
        dest = self._scratch / f"{asset.immich_id}-{nonce}-{Path(asset.filename).name}"
        self._immich.download_original(asset.immich_id, dest)
        # Only ever the scratch copy: Immich's own originals already carry the
        # right mtime and its library is mounted read-only.
        _stamp_capture_date(dest, asset.taken_at or self._lookup_taken_at(asset))
        return ResolvedBytes(path=dest, temporary=True)

    def _lookup_taken_at(self, asset: Asset) -> str | None:
        """Ask Immich for a capture date the stored row does not have.

        Only reached on the download path, and only for a row whose `taken_at`
        is empty -- in practice one queued before that column existed. Those
        rows are never re-read before the worker claims them (a reconcile or
        backfill pass only refreshes what it happens to walk), so without this
        the entire backlog present at upgrade time would upload dated today.
        Steady-state rows already carry the date and never get here, so this
        costs one extra request per legacy asset, once, against a download
        that was already far more expensive.

        Swallows everything: the bytes are on disk and the upload is the
        valuable part, so a metadata call that fails costs the date, not the
        backup.
        """
        try:
            return self._immich.asset_taken_at(asset.immich_id)
        except Exception as exc:  # noqa: BLE001 - a date is never worth failing a backup over
            logger.debug("could not look up the capture date for %s: %s", asset.immich_id, exc)
            return None

    def release(self, resolved: ResolvedBytes) -> None:
        if resolved.temporary:
            resolved.path.unlink(missing_ok=True)
