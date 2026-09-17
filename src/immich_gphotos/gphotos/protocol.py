from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from immich_gphotos.models import ErrorClass


class GPhotosError(Exception):
    def __init__(self, message: str, error_class: ErrorClass = ErrorClass.UNKNOWN) -> None:
        super().__init__(message)
        self.error_class = error_class


class GooglePhotosClient(Protocol):
    def exists(self, checksum: str) -> str | None: ...
    def upload(self, path: Path, *, checksum: str, filename: str) -> str: ...
    def create_album(self, name: str, media_keys: Sequence[str]) -> str: ...
    def add_to_album(self, album_id: str, media_keys: Sequence[str]) -> None: ...
    def trash(self, checksums: Sequence[str]) -> None: ...
