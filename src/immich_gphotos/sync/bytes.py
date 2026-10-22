import base64
import hashlib
import logging
import os
import shutil
import stat
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from immich_gphotos.immich.protocol import ImmichClient
from immich_gphotos.models import Asset

logger = logging.getLogger(__name__)

# A direct-read original whose mtime is within this of `taken_at` is handed
# straight to gpmc. Anything further off is a file whose mtime no longer
# reflects when the photo was taken -- a NAS move/copy/import that reset it
# to the import date is the common case -- and gpmc would send that wrong
# mtime to Google as the capture timestamp for any bytes carrying no date of
# their own. 2s covers filesystem granularity and second-truncated dates
# without copying libraries whose mtimes are merely rounded.
MTIME_TOLERANCE_SECONDS = 2.0

# Where Immich's own container keeps its library by default -- the one root a
# direct read is allowed from when nothing else is configured (see
# `accounts.build.direct_read_roots`). `originalPath` comes from the Immich
# API verbatim, so without a root allowlist any path Immich reports (or a
# compromised/misconfigured Immich could report) -- this service's own
# database, its credentials, /proc -- would be read and uploaded as "media".
DEFAULT_DIRECT_READ_ROOTS: tuple[Path, ...] = (Path("/usr/src/app/upload"),)

# A diagnostic attached to a resolution whose capture date could not be
# confirmed on the bytes handed to gpmc (no date known, a stamp that failed,
# a scratch copy that could not be made). Never fatal: the bytes still go up,
# Google simply dates them as it would have before. Recorded on the asset row
# (`sync_notes`) so "the date is right" is never assumed when it was not
# checked. See `ResolvedBytes.notes`.
TIMESTAMP_UNVERIFIED = "timestamp_unverified"

_HASH_CHUNK = 1024 * 1024


class SourceChangedError(OSError):
    """A direct-read original changed while it was being read.

    Raised when the file's identity (device, inode, size, mtime) differs
    between the stat taken before reading and the one taken after, or when
    the opened descriptor is not the file that was checked. The resolver
    treats it as "do not trust the library copy right now" and falls back to
    the Immich API download; the worker treats one raised after upload as a
    retryable unavailability. Subclasses OSError so every existing handler
    that already treats an unreadable source as retryable keeps doing so.
    """


def _identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _NOFOLLOW | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | _NOFOLLOW | _CLOEXEC | getattr(os, "O_NONBLOCK", 0)


def open_beneath(root: Path, target: Path) -> int:
    """Open `target` read-only, walking down from `root` one component at a time.

    Race-safe replacement for "resolve, check it is under the root, then
    open the path": every directory below `root` is opened relative to its
    parent's descriptor with O_DIRECTORY|O_NOFOLLOW, and the file itself
    with O_NOFOLLOW, so a parent directory swapped for a symlink *after* the
    allowlist check makes the open fail (ELOOP/ENOTDIR) instead of silently
    reading a file elsewhere. `..`, `.` and empty components are refused
    outright. Only `root` itself (a configured, already-resolved mount
    point) is opened by path. The result is verified with fstat to be a
    regular file. Returns a blocking descriptor the caller must close.
    """
    rel = target.relative_to(root)  # ValueError when not beneath it
    parts = rel.parts
    if not parts or any(part in ("", ".", "..") for part in parts):
        raise PermissionError(f"refusing to open {target}: not a plain path beneath {root}")
    dir_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _CLOEXEC)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, _DIR_FLAGS, dir_fd=dir_fd)
            os.close(dir_fd)
            dir_fd = next_fd
        fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=dir_fd)
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PermissionError(f"refusing to open {target}: not a regular file")
        os.set_blocking(fd, True)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _parse_taken_at(taken_at: str | None) -> float | None:
    """Parse Immich's `fileCreatedAt` to a unix timestamp, or None.

    None covers every "no usable date" shape: missing, empty, malformed or
    unrepresentable. Callers treat None as "cannot verify the mtime, upload
    as before" -- a timestamp is never worth failing a backup over.
    """
    if not taken_at:
        return None
    try:
        return datetime.fromisoformat(taken_at.replace("Z", "+00:00")).timestamp()
    except (ValueError, OverflowError):
        return None


def _stamp_capture_date(path: Path, taken_at: str | None) -> bool:
    """Set `path`'s mtime to when the asset was actually taken.

    gpmc uploads a file's `st_mtime` to Google as the capture timestamp, and
    Google honours it for any file whose bytes carry no date of their own --
    WhatsApp videos, screenshots, anything stripped of EXIF. A file downloaded
    through the API lands with an mtime of "now", which is what put those
    assets in Google Photos dated today instead of when they were taken.

    Best-effort by design: a missing, malformed or unrepresentable date leaves
    the file alone and the asset still uploads, dated by Google as before. A
    timestamp is never worth failing a backup over.

    Returns True only when the date was parsed *and* written, so callers
    can report `TIMESTAMP_UNVERIFIED` for everything else.
    """
    when = _parse_taken_at(taken_at)
    if when is None:
        return False
    try:
        os.utime(path, (when, when))
    except (OSError, OverflowError) as exc:
        logger.debug("could not stamp capture date %r onto %s: %s", taken_at, path.name, exc)
        return False
    return True


def _sha1_base64_of_file(path: Path) -> str:
    """Base64 SHA-1 of the file at `path`. Raises OSError when unreadable.

    Chunked so a multi-gigabyte video never sits in memory twice. Google
    identifies media by the content SHA-1 of the bytes (dedup lookup,
    X-Goog-Hash, trash-by-dedup-key all derive from it), and Immich's own
    checksum is that hash only for internal uploads -- External Library rows
    carry a virtual sha1(path) checksum that never matches either -- so the
    resolved bytes are always hashed here and the worker describes *these*
    bytes to Google, never Immich's checksum.
    """
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


def _sha1_base64_of_stable_file(
    path: Path, expected: tuple[int, int, int, int], root: Path | None = None
) -> str:
    """Hash a direct-read original, refusing one that changed underneath us.

    With `root` (the library root the path was allowed under) the file is
    opened by `open_beneath`, so no component of the path can have been
    swapped for a symlink since the allowlist check; without it, O_NOFOLLOW
    guards the final component only. The open descriptor's identity is
    compared with the pre-read stat, and the path is re-opened/re-stat'ed
    after the last byte: a file replaced, appended to or rewritten between
    check and read raises `SourceChangedError` instead of being hashed as if
    it were the file that was checked.
    """
    fd = open_beneath(root, path) if root is not None else os.open(path, os.O_RDONLY | _NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        if _identity(os.fstat(handle.fileno())) != expected:
            raise SourceChangedError(f"{path.name} changed before it could be read")
        digest = hashlib.sha1()
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b""):
            digest.update(chunk)
        if _identity(os.fstat(handle.fileno())) != expected:
            raise SourceChangedError(f"{path.name} changed while it was being read")
    if _path_identity(path, root) != expected:
        raise SourceChangedError(f"{path.name} changed while it was being read")
    return base64.b64encode(digest.digest()).decode("ascii")


def _copy_from_fd(fd: int, size: int, dest: Path) -> None:
    """Copy the first `size` bytes of `fd` to a new scratch file `dest`."""
    with open(dest, "wb") as out:
        offset = 0
        while offset < size:
            chunk = os.pread(fd, min(_HASH_CHUNK, size - offset), offset)
            if not chunk:
                raise SourceChangedError(f"{dest.name}: source shrank while it was being copied")
            out.write(chunk)
            offset += len(chunk)


def _path_identity(path: Path, root: Path | None) -> tuple[int, int, int, int]:
    """Identity of what `path` names *now*, resolved the same race-safe way."""
    if root is None:
        return _identity(os.stat(path, follow_symlinks=False))
    fd = open_beneath(root, path)
    try:
        return _identity(os.fstat(fd))
    finally:
        os.close(fd)


class _PinnedSource:
    """`gphotos.protocol.UploadSource` for one resolved file.

    A direct-read original is re-opened beneath its library root and must
    still be the exact file (dev, inode, size, mtime) that was hashed; a
    scratch file is opened without following symlinks and pinned to the
    identity it had when first opened.
    """

    def __init__(self, resolved: "ResolvedBytes") -> None:
        self._resolved = resolved
        self._expected = resolved.source_identity

    def open(self) -> int:
        r = self._resolved
        try:
            if r.direct_root is not None:
                fd = open_beneath(r.direct_root, r.path)
            else:
                fd = os.open(r.path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
        except (OSError, ValueError) as exc:
            raise SourceChangedError(f"{r.path.name} could not be reopened for upload: {exc}") from exc
        current = _identity(os.fstat(fd))
        if self._expected is None:
            self._expected = current
        elif current != self._expected:
            os.close(fd)
            raise SourceChangedError(f"{r.path.name} is no longer the file that was hashed")
        return fd

    def unchanged(self, fd: int) -> bool:
        try:
            if _identity(os.fstat(fd)) != self._expected:
                return False
            return _path_identity(self._resolved.path, self._resolved.direct_root) == self._expected
        except (OSError, ValueError):
            return False


@dataclass(frozen=True)
class ResolvedBytes:
    path: Path
    temporary: bool
    # When the still was muxed with its Live Photo motion before upload,
    # the Immich id of the companion `.MOV` that was folded in. The worker
    # marks that row `live_photo_paired` after the upload lands, so the
    # hidden clip is accounted for instead of lingering as `hidden`. None
    # for every ordinary (unmuxed) resolution.
    paired_video_id: str | None = None
    # Base64 SHA-1 of the bytes at `path` -- always set by `resolve()` (the
    # helper constructors leave it out and `resolve()` fills it in once).
    # This is the value google-side consumers must be given; see
    # `_sha1_base64_of_file` for why Immich's checksum step is never used.
    sha1_b64: str | None = None
    # For a direct-read original (temporary=False): the (dev, inode, size,
    # mtime_ns) identity it was checked and hashed under. The worker asks
    # `ByteResolver.still_unchanged` after the upload so bytes that changed
    # while gpmc was reading them are never recorded under a hash they no
    # longer have. None for scratch files, which only this process writes.
    source_identity: tuple[int, int, int, int] | None = None
    # The configured library root a direct read was allowed under; every
    # later open of `path` walks down from it (`open_beneath`).
    direct_root: Path | None = None
    # Non-fatal diagnostics about this resolution (e.g. TIMESTAMP_UNVERIFIED).
    notes: tuple[str, ...] = ()
    # Live Photo bookkeeping, only ever set when `resolve` was asked to mux
    # (`mux_live_photo=True`, i.e. `derived_motion` mode):
    #   derived            True when `path` is a Motion Photo built in scratch
    #                      from the two originals -- a *derived* artifact whose
    #                      SHA-1 (`sha1_b64`) is not the original still's.
    #   raw_still_sha1     the original still's own content SHA-1, so the
    #                      worker can tell "original already in Google" apart
    #                      from "Motion Photo already in Google".
    #   live_failure       why muxing did not happen (bytes are the still alone)
    #   live_failure_transient  whether that reason may clear by itself (the
    #                      companion row not there yet, a read error) as
    #                      opposed to never (unsupported format, oversized clip).
    derived: bool = False
    raw_still_sha1: str | None = None
    live_failure: str | None = None
    live_failure_transient: bool = False


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class ByteResolver:
    """Get at an asset's bytes by the cheapest route available.

    When the container can see Immich's library volume, `originalPath` is
    handed straight to gpmc -- but only when that file's mtime still reflects
    when the photo was taken. Immich normally stores its originals with the
    capture date as the mtime, and then the direct path is a zero-copy win;
    a move, copy or re-import on some NAS/server setups resets the mtime to
    the import date instead, and gpmc would send that wrong mtime to Google
    as the capture timestamp for any bytes carrying no date of their own.
    A direct file whose mtime disagrees with `taken_at` is therefore copied
    to scratch, stamped, and uploaded from there -- Immich's own file is
    never modified (its library is mounted read-only) and never re-dated.

    Otherwise the original is downloaded to scratch and deleted afterwards.

    A direct read is only ever attempted for a path that, once fully
    resolved (symlinks included), lies inside one of `direct_roots` -- the
    read-only Immich library mount(s) the operator configured -- and never
    inside this service's own scratch directory. Anything else (a relative
    path, `..` games, a symlink pointing out of the library, a path to the
    data directory) is downloaded through the Immich API instead, so
    `originalPath` can never turn into "read any file in the container".

    An Apple Live Photo still (one carrying `live_photo_video_id`) is muxed
    with its companion `.MOV` into a single Google Motion Photo before it is
    handed back -- see `_try_resolve_live_photo`. JPEG muxing is pure Python;
    HEIC/AVIF muxing writes the XMP through ExifTool (`sync.exif_xmp`,
    injected as `xmp`, None when the binary is unavailable). Anything that
    makes muxing impossible (companion row not yet in the database, bytes
    unreadable, still not a JPEG/HEIC/AVIF, ExifTool missing or refusing,
    oversized or unrecognised video) falls back to the still alone, as
    before: motion is never worth failing a backup over.
    """

    def __init__(
        self,
        immich: ImmichClient,
        scratch: Path,
        allow_direct: bool = True,
        assets=None,  # noqa: ANN001 - AssetRepo; only for the Live Photo companion lookup
        xmp=None,  # noqa: ANN001 - sync.exif_xmp.MotionPhotoXmp; None disables HEIC muxing
        direct_roots: Iterable[Path] | None = None,
    ) -> None:
        self._immich = immich
        self._scratch = Path(scratch)
        self._allow_direct = allow_direct
        roots = DEFAULT_DIRECT_READ_ROOTS if direct_roots is None else tuple(direct_roots)
        resolved_roots: list[Path] = []
        for root in roots:
            try:
                resolved_roots.append(Path(root).resolve(strict=False))
            except (OSError, RuntimeError):
                continue
        self._direct_roots = tuple(resolved_roots)
        self._assets = assets
        self._xmp = xmp

    def _allowed_direct_path(self, original_path: str) -> Path | None:
        """The real path of `original_path` when a direct read is allowed, else None.

        Allowed means: absolute, fully resolvable (every symlink followed),
        a regular file, inside one of the configured library roots and not
        inside scratch. The *resolved* path is what is returned and read, so
        a symlink inside the library that points outside it is refused here
        rather than followed later.
        """
        if not self._direct_roots:
            return None
        candidate = Path(original_path)
        if not candidate.is_absolute():
            return None
        try:
            real = candidate.resolve(strict=True)
            st = os.stat(real, follow_symlinks=False)
        except (OSError, RuntimeError):
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        try:
            scratch = self._scratch.resolve(strict=False)
        except (OSError, RuntimeError):
            scratch = self._scratch
        if _within(real, scratch):
            return None
        if self._root_for(real) is None:
            logger.debug("refusing direct read outside the library roots: %s", real)
            return None
        return real

    def _root_for(self, real: Path) -> Path | None:
        for root in self._direct_roots:
            if real != root and _within(real, root):
                return root
        return None

    def _direct_or_copy(self, candidate: Path, asset: Asset, taken_at: str | None) -> ResolvedBytes:
        """Hand back `candidate` itself when its mtime already agrees with the
        capture date, else a stamped scratch copy of it.

        `taken_at` is the resolved capture-date string (already falling back
        to a lookup for legacy rows); None means there is no date to check
        against, so the file goes up as-is, as before -- flagged
        `TIMESTAMP_UNVERIFIED`, since nothing confirmed its mtime.

        Raises OSError (the caller falls back to the API download) when the
        file vanishes, or `SourceChangedError` when it changes while being
        copied. A copy that fails part-way removes its own partial scratch
        file -- only that nonce path, never anything in the library -- so a
        long NAS sync cannot fill the disk with orphaned partial copies.
        """
        root = self._root_for(candidate)
        if root is None:
            raise PermissionError(f"{candidate} is not beneath a library root")
        fd = open_beneath(root, candidate)
        try:
            before = os.fstat(fd)
            identity = _identity(before)
            when = _parse_taken_at(taken_at)
            direct = ResolvedBytes(
                path=candidate, temporary=False, source_identity=identity, direct_root=root
            )
            if when is None:
                return replace(direct, notes=(TIMESTAMP_UNVERIFIED,))
            if abs(before.st_mtime - when) <= MTIME_TOLERANCE_SECONDS:
                return direct
            dest = self._scratch_path(asset)
            try:
                # Copied from the descriptor opened beneath the root, never
                # by re-opening the path.
                _copy_from_fd(fd, before.st_size, dest)
            except SourceChangedError:
                dest.unlink(missing_ok=True)
                raise
            except OSError as exc:
                dest.unlink(missing_ok=True)
                # The copy is an optimisation for correctness; losing it must
                # never lose the backup. Fall back to handing back the original:
                # the upload still happens, dated by Google as before -- and
                # flagged, so nobody mistakes it for a verified date.
                logger.debug("could not copy %s to scratch for re-dating: %s", candidate.name, exc)
                return replace(direct, notes=(TIMESTAMP_UNVERIFIED,))
            try:
                changed = _identity(os.fstat(fd)) != identity or _path_identity(candidate, root) != identity
            except (OSError, ValueError):
                dest.unlink(missing_ok=True)
                raise
            if changed:
                dest.unlink(missing_ok=True)
                raise SourceChangedError(f"{candidate.name} changed while it was being copied")
        finally:
            os.close(fd)
        if not _stamp_capture_date(dest, taken_at):
            return ResolvedBytes(path=dest, temporary=True, notes=(TIMESTAMP_UNVERIFIED,))
        return ResolvedBytes(path=dest, temporary=True)

    def still_unchanged(self, resolved: ResolvedBytes) -> bool:
        """Whether a direct-read original is still the file that was hashed.

        Always True for scratch files. For a direct read, compares the file's
        current identity with the one it was hashed under -- the worker calls
        this after the upload so a library file rewritten while gpmc was
        streaming it is retried instead of recorded under a stale hash.
        """
        if resolved.temporary or resolved.source_identity is None:
            return True
        try:
            return _path_identity(resolved.path, resolved.direct_root) == resolved.source_identity
        except (OSError, ValueError):
            return False

    def upload_source(self, resolved: ResolvedBytes) -> _PinnedSource:
        """The pinned descriptor source the uploader must stream from."""
        return _PinnedSource(resolved)

    def resolve(self, asset: Asset, *, mux_live_photo: bool = False) -> ResolvedBytes:
        """Resolve `asset`'s bytes and certify their content SHA-1.

        The muxed Live Photo branch wins when it can mux; otherwise the
        single-asset path runs. Either way, the returned `ResolvedBytes`
        carries the SHA-1 of the exact bytes at `path` -- Google keys dedup
        lookup, upload integrity and trash on the *content* hash, and only
        Internal-upload Immich checksums ever equal that (External Library
        rows are sha1(path)), so the worker describes these bytes, never
        Immich's checksum. An unreadable file raises OSError: unhashed bytes
        go nowhere rather than up with a description we could not verify.

        Muxing only ever happens when the caller asks for it
        (`mux_live_photo`, set by the worker only in `derived_motion` mode):
        a Live Photo still is otherwise resolved exactly like any other
        image, its original bytes untouched.
        """
        if mux_live_photo and asset.live_photo_video_id and self._assets is not None:
            muxed, reason, transient = self._try_resolve_live_photo(asset)
            if muxed is not None:
                return muxed
            single = self._certify(asset, self._resolve_single(asset))
            return replace(
                single,
                raw_still_sha1=single.sha1_b64,
                live_failure=reason,
                live_failure_transient=transient,
            )
        return self._certify(asset, self._resolve_single(asset))

    def _certify(self, asset: Asset, resolved: ResolvedBytes) -> ResolvedBytes:
        """Attach the content SHA-1, re-resolving through the API if a direct
        original changed while being hashed."""
        if resolved.temporary or resolved.source_identity is None:
            return replace(resolved, sha1_b64=_sha1_base64_of_file(resolved.path))
        try:
            return replace(
                resolved,
                sha1_b64=_sha1_base64_of_stable_file(
                    resolved.path, resolved.source_identity, resolved.direct_root
                ),
            )
        except (OSError, ValueError) as exc:
            logger.debug("direct original %s unstable, downloading instead: %s", asset.immich_id, exc)
            downloaded = self._download(asset)
            return replace(downloaded, sha1_b64=_sha1_base64_of_file(downloaded.path))

    def _scratch_path(self, asset: Asset) -> Path:
        """A fresh scratch path for one resolution.

        The nonce guarantees a fresh path per call: two concurrent
        resolutions of the *same* asset (e.g. a slow-but-healthy upload that
        crosses requeue_stale_uploading's liveness-free age threshold and gets
        handed to a second worker) must never share a scratch file, or one
        caller's release() could delete bytes the other is still writing or
        uploading. Only the basename of the filename is used because
        `originalFileName` arrives unsanitized from Immich.
        """
        self._scratch.mkdir(parents=True, exist_ok=True)
        nonce = uuid.uuid4().hex
        return self._scratch / f"{asset.immich_id}-{nonce}-{Path(asset.filename).name}"

    def _try_resolve_live_photo(self, still: Asset) -> tuple[ResolvedBytes | None, str | None, bool]:
        """Build a Motion Photo from a Live Photo still and its companion.

        Returns (muxed, None, False) on success, else (None, reason,
        transient): `transient` reasons (companion row not in the database
        yet, a source unreadable right now, an I/O error mid-mux) may clear
        by themselves; the rest (unsupported still format, rejected clip,
        ExifTool missing or refusing) will not without a change on the
        user's side. Never raises out of `resolve`.

        Memory is bounded: sizes and headers are checked by stat and a short
        read before anything else, and both sources are streamed through in
        chunks into a fresh scratch file. Only scratch copies are ever
        written -- the originals (direct-read or downloaded) are read-only
        inputs here -- and a failed mux removes its own scratch file.
        """
        from immich_gphotos.sync import livephoto

        assert self._assets is not None
        try:
            companion_stored = self._assets.get(still.live_photo_video_id or "")
        except Exception as exc:  # noqa: BLE001 - a lookup failure costs motion, not the backup
            logger.debug("could not look up Live Photo motion for %s: %s", still.immich_id, exc)
            return None, "companion_lookup_failed", True
        if companion_stored is None:
            return None, "companion_missing", True
        companion = companion_stored.asset
        try:
            still_single = self._resolve_single(still)
        except Exception as exc:  # noqa: BLE001 - motion is never worth failing the still over
            logger.debug("could not resolve Live Photo still %s: %s", still.immich_id, exc)
            return None, "still_unreadable", True
        try:
            companion_single = self._resolve_single(companion)
        except Exception as exc:  # noqa: BLE001 - see above
            logger.debug("could not resolve Live Photo motion for %s: %s", still.immich_id, exc)
            self.release(still_single)
            return None, "companion_unreadable", True
        muxed_path: Path | None = None
        try:
            try:
                kind = livephoto.still_kind(still_single.path)
                raw_still_sha1 = livephoto.sha1_b64_of_file(still_single.path)
            except OSError as exc:
                logger.debug("could not read Live Photo still %s: %s", still.immich_id, exc)
                return None, "still_unreadable", True
            if kind is None:
                logger.debug("not muxing Live Photo %s: still is not JPEG/HEIC/AVIF", still.immich_id)
                return None, "still_format_unsupported", False
            video_mime = livephoto.video_mime_for_filename(companion.filename)
            muxed_path = self._scratch_path(still)
            try:
                if kind == "jpeg":
                    livephoto.write_motion_photo_jpeg(
                        still_single.path, companion_single.path, muxed_path, video_mime
                    )
                else:
                    reason = self._mux_heic_with_exiftool(
                        still, still_single.path, companion_single.path, muxed_path, kind, video_mime
                    )
                    if reason is not None:
                        muxed_path.unlink(missing_ok=True)
                        return None, reason, False
            except ValueError as exc:
                muxed_path.unlink(missing_ok=True)
                logger.debug("not muxing Live Photo %s: %s", still.immich_id, exc)
                return None, f"rejected: {exc}", False
            except OSError as exc:
                muxed_path.unlink(missing_ok=True)
                logger.debug("could not stage muxed Live Photo %s: %s", still.immich_id, exc)
                return None, "mux_io_error", True
            notes: tuple[str, ...] = ()
            if not _stamp_capture_date(muxed_path, still.taken_at or self._lookup_taken_at(still)):
                notes = (TIMESTAMP_UNVERIFIED,)
            muxed = ResolvedBytes(
                path=muxed_path,
                temporary=True,
                paired_video_id=companion.immich_id,
                sha1_b64=_sha1_base64_of_file(muxed_path),
                notes=notes,
                derived=True,
                raw_still_sha1=raw_still_sha1,
            )
            muxed_path = None  # handed over; the caller releases it
            return muxed, None, False
        finally:
            if muxed_path is not None:
                # Every failure path: never leave a half-built scratch file.
                muxed_path.unlink(missing_ok=True)
            # The muxed file is an independent copy; both source files are
            # no longer needed once it is staged (or once we have given up).
            self.release(still_single)
            self.release(companion_single)

    def _mux_heic_with_exiftool(
        self,
        still: Asset,
        still_path: Path,
        video_path: Path,
        muxed_path: Path,
        kind: str,
        video_mime: str,
    ) -> str | None:
        """Mux a HEIC/AVIF still via ExifTool into `muxed_path`.

        Returns None on success or a permanent failure reason. ExifTool
        writes the merged Motion Photo XMP into the scratch copy (never the
        original -- only `muxed_path` is ever passed to `write_xmp`), then
        the trailer is appended with the clip streamed through. Raises
        ValueError for a rejected clip/still and OSError for I/O trouble.
        """
        from immich_gphotos.sync import livephoto

        if self._xmp is None:
            logger.debug("not muxing Live Photo %s: exiftool is unavailable", still.immich_id)
            return "exiftool_unavailable"
        livephoto.check_still_file(still_path)
        video_size = livephoto.check_video_file(video_path)
        try:
            keyframe_us = self._xmp.keyframe_us(video_path)
        except Exception as exc:  # noqa: BLE001 - unknown timestamp degrades to -1
            logger.debug("could not read keyframe for %s: %s", still.immich_id, exc)
            keyframe_us = None
        packet = livephoto.heic_xmp_packet(
            livephoto.heic_trailer_length_for(video_size),
            video_mime,
            livephoto.image_mime_for_kind(kind),
            keyframe_us,
        )
        try:
            original = self._xmp.original_xmp(still_path)
        except Exception as exc:  # noqa: BLE001 - nothing to merge just means ours alone
            logger.debug("could not read original XMP for %s: %s", still.immich_id, exc)
            original = None
        shutil.copyfile(still_path, muxed_path)
        try:
            self._xmp.write_xmp(muxed_path, livephoto.merge_xmp(original, packet))
        except Exception as exc:  # noqa: BLE001 - a refused write costs motion, not the backup
            logger.debug("could not write Motion Photo XMP for %s: %s", still.immich_id, exc)
            return "exiftool_refused"
        livephoto.append_heic_trailer(muxed_path, video_path, video_size)
        return None

    def _resolve_single(self, asset: Asset) -> ResolvedBytes:
        if self._allow_direct and asset.original_path:
            candidate = self._allowed_direct_path(asset.original_path)
            if candidate is not None:
                try:
                    return self._direct_or_copy(
                        candidate, asset, asset.taken_at or self._lookup_taken_at(asset)
                    )
                except (OSError, ValueError) as exc:
                    logger.debug("direct read of %s failed, downloading: %s", asset.immich_id, exc)
        return self._download(asset)

    def _download(self, asset: Asset) -> ResolvedBytes:
        dest = self._scratch_path(asset)
        try:
            self._immich.download_original(asset.immich_id, dest)
        except BaseException:
            dest.unlink(missing_ok=True)
            raise
        # The scratch copy only: a direct-read original is either already
        # correct (handed back above) or was copied-and-stamped there, and
        # Immich's library is mounted read-only.
        if not _stamp_capture_date(dest, asset.taken_at or self._lookup_taken_at(asset)):
            return ResolvedBytes(path=dest, temporary=True, notes=(TIMESTAMP_UNVERIFIED,))
        return ResolvedBytes(path=dest, temporary=True)

    def _lookup_taken_at(self, asset: Asset) -> str | None:
        """Ask Immich for a capture date the stored row does not have.

        Reached only for a row whose `taken_at` is empty -- in practice one
        queued before that column existed. Those rows are never re-read
        before the worker claims them (a reconcile or backfill pass only
        refreshes what it happens to walk), so without this the entire
        backlog present at upgrade time would upload dated today.
        Steady-state rows already carry the date and never get here, so this
        costs one extra request per legacy asset, once. On the download path
        that request runs against bytes already on disk; on the direct path
        it decides whether the original's mtime can be trusted at all.

        Swallows everything: the bytes are on disk (or the direct file is
        still uploadable) and the upload is the valuable part, so a metadata
        call that fails costs the date, not the backup.
        """
        try:
            return self._immich.asset_taken_at(asset.immich_id)
        except Exception as exc:  # noqa: BLE001 - a date is never worth failing a backup over
            logger.debug("could not look up the capture date for %s: %s", asset.immich_id, exc)
            return None

    def release(self, resolved: ResolvedBytes) -> None:
        if resolved.temporary:
            resolved.path.unlink(missing_ok=True)
