import os
from collections.abc import Sequence
from pathlib import Path

from immich_gphotos.gphotos import stable_upload
from immich_gphotos.gphotos.protocol import GPhotosError, UploadSource


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
        # Uploads whose bytes were streamed (whether or not an item was then
        # created) and an optional hook run just before the commit check.
        self.streamed: list[str] = []
        # Which Google account this fake "is" (sync.favorite_ledger); two
        # service accounts sharing one fake model a shared Google account.
        self.google_account: str | None = None
        self.before_commit = None
        self.favorites: dict[str, bool] = {}
        self.favorite_calls: list[tuple[str, bool]] = []
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

    def upload(self, path: Path, *, checksum: str, filename: str, source: UploadSource | None = None) -> str:
        """Same commit rule as `GpmcClient.upload`: stream from the pinned
        source, and only "create the item" if the streamed bytes hash to
        `checksum` and the source is unchanged. `before_commit`, when set,
        runs between streaming and that check -- tests use it to rewrite or
        swap the file at exactly the racy moment."""
        self._check_method_failure("upload")
        if checksum in self.fail_on:
            raise self.fail_on[checksum]
        if source is not None:
            fd = stable_upload.open_source(path, source)
            try:
                before = os.fstat(fd)
                reader = stable_upload.FdReader(fd, before.st_size)
                while reader.read(stable_upload.CHUNK):
                    pass
                self.streamed.append(checksum)
                if self.before_commit is not None:
                    self.before_commit(path)
                stable_upload.check_before_commit(fd, before, reader, checksum, source)
            finally:
                os.close(fd)
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

    def set_favorite(self, media_key: str, is_favorite: bool) -> None:
        self._check_method_failure("set_favorite")
        self.favorite_calls.append((media_key, is_favorite))
        self.favorites[media_key] = is_favorite
