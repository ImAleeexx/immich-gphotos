from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from immich_gphotos.models import ErrorClass


class GPhotosError(Exception):
    def __init__(self, message: str, error_class: ErrorClass = ErrorClass.UNKNOWN) -> None:
        super().__init__(message)
        self.error_class = error_class


class SourceChangedDuringUpload(GPhotosError):
    """The bytes streamed to Google were not the bytes that were hashed.

    Raised *before* the media item is created (gpmc's commit step), so no
    Google item exists for the attempt -- an uncommitted upload token just
    expires. Retryable: the next attempt re-resolves and re-hashes.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, ErrorClass.ASSET_UNAVAILABLE)


class UploadSource(Protocol):
    """Where an upload's bytes come from, pinned to one file.

    `open()` returns a read-only descriptor of exactly the file that was
    resolved and hashed (or raises OSError); `unchanged(fd)` says whether
    that descriptor *and* the path it was resolved from still name the same,
    unmodified file. The uploader streams from the descriptor, never from
    the path, and checks `unchanged` again before creating the media item.
    """

    def open(self) -> int: ...
    def unchanged(self, fd: int) -> bool: ...


class GooglePhotosClient(Protocol):
    def exists(self, checksum: str) -> str | None: ...
    def upload(
        self, path: Path, *, checksum: str, filename: str, source: UploadSource | None = None
    ) -> str: ...
    def create_album(self, name: str, media_keys: Sequence[str]) -> str: ...
    def add_to_album(self, album_id: str, media_keys: Sequence[str]) -> None: ...
    def trash(self, checksums: Sequence[str]) -> None: ...
    def set_favorite(self, media_key: str, is_favorite: bool) -> None: ...
