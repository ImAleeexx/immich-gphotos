"""Stream an upload from one pinned descriptor and prove what was sent.

The race this closes: bytes are hashed, then uploaded, then the Google media
item is created. If the file is rewritten in between, the item Google keeps
is not the file that was described -- and a retry then uploads a second,
"stray" item. Here the upload streams from a single open descriptor, the
SHA-1 of the exact bytes streamed is computed on the way out, and the media
item is only created when (a) that streamed hash equals the declared one and
(b) the source still names the same unmodified file. Otherwise nothing is
committed: an uploaded-but-uncommitted blob is not a media item and simply
expires on Google's side.
"""

import base64
import hashlib
import os
from pathlib import Path

from immich_gphotos.gphotos.protocol import SourceChangedDuringUpload, UploadSource

CHUNK = 1024 * 1024


def identity(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def open_source(path: Path, source: UploadSource | None) -> int:
    if source is not None:
        return source.open()
    return os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))


def sha1_b64_of_fd(fd: int, size: int) -> str:
    digest = hashlib.sha1()
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(CHUNK, size - offset), offset)
        if not chunk:
            raise SourceChangedDuringUpload("source shrank while it was being hashed")
        digest.update(chunk)
        offset += len(chunk)
    return base64.b64encode(digest.digest()).decode("ascii")


class FdReader:
    """File-like view of the first `size` bytes of `fd` that hashes what it hands out.

    Uses `pread`, so nothing else's file offset matters, and never reads past
    `size`: bytes appended after the stat are never sent.
    """

    def __init__(self, fd: int, size: int) -> None:
        self._fd = fd
        self._size = size
        self._offset = 0
        self._digest = hashlib.sha1()

    def __len__(self) -> int:
        return self._size

    def read(self, n: int = -1) -> bytes:
        remaining = self._size - self._offset
        if remaining <= 0:
            return b""
        want = remaining if n is None or n < 0 else min(n, remaining)
        chunk = os.pread(self._fd, want, self._offset)
        if not chunk:
            raise SourceChangedDuringUpload("source shrank while it was being uploaded")
        self._offset += len(chunk)
        self._digest.update(chunk)
        return chunk

    def sent_exactly(self, checksum_b64: str) -> bool:
        return (
            self._offset == self._size
            and base64.b64encode(self._digest.digest()).decode("ascii") == checksum_b64
        )


def check_before_commit(
    fd: int, before: os.stat_result, reader: FdReader, checksum: str, source: UploadSource | None
) -> None:
    """Raise unless what was streamed is provably the declared, unchanged file."""
    if not reader.sent_exactly(checksum):
        raise SourceChangedDuringUpload("the bytes sent do not hash to the declared SHA-1")
    if identity(os.fstat(fd)) != identity(before):
        raise SourceChangedDuringUpload("the source changed while it was being uploaded")
    if source is not None and not source.unchanged(fd):
        raise SourceChangedDuringUpload("the source path no longer names the uploaded file")
