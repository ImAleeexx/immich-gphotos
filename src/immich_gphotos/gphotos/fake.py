from collections.abc import Sequence
from pathlib import Path

from immich_gphotos.gphotos.protocol import GPhotosError


class FakeGooglePhotosClient:
    """In-memory Google Photos, including a hook for injecting failures."""

    def __init__(
        self,
        present: dict[str, str] | None = None,
        fail_on: dict[str, GPhotosError] | None = None,
        fail_methods: dict[str, GPhotosError] | None = None,
    ) -> None:
        self.present = dict(present or {})
        self.fail_on = dict(fail_on or {})
        self.fail_methods = dict(fail_methods or {})
        self.uploads: list[tuple[str, str]] = []
        self.albums: dict[str, list[str]] = {}
        self.trashed: list[str] = []
        self._next_key = 0

    def _key(self) -> str:
        self._next_key += 1
        return f"media-key-{self._next_key}"

    def _check_method_failure(self, method: str) -> None:
        if method in self.fail_methods:
            raise self.fail_methods[method]

    def exists(self, checksum: str) -> str | None:
        self._check_method_failure("exists")
        return self.present.get(checksum)

    def upload(self, path: Path, *, checksum: str, filename: str) -> str:
        self._check_method_failure("upload")
        if checksum in self.fail_on:
            raise self.fail_on[checksum]
        self.uploads.append((checksum, filename))
        key = self._key()
        self.present[checksum] = key
        return key

    def create_album(self, name: str, media_keys: Sequence[str]) -> str:
        self._check_method_failure("create_album")
        album_id = f"album-{name}"
        self.albums[album_id] = list(media_keys)
        return album_id

    def add_to_album(self, album_id: str, media_keys: Sequence[str]) -> None:
        self._check_method_failure("add_to_album")
        self.albums.setdefault(album_id, []).extend(media_keys)

    def trash(self, checksums: Sequence[str]) -> None:
        self._check_method_failure("trash")
        for checksum in checksums:
            self.trashed.append(checksum)
            self.present.pop(checksum, None)
