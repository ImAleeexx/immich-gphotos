import threading
from collections.abc import Sequence
from pathlib import Path

from immich_gphotos.config import Quality
from immich_gphotos.gphotos.protocol import GPhotosError
from immich_gphotos.models import ErrorClass

ALBUM_BATCH = 500

_AUTH_MARKERS = ("401", "403", "unauthorized", "auth token", "auth_data", "authentication")
_RATE_MARKERS = ("429", "rate limit", "too many requests")
_QUOTA_MARKERS = ("quota", "storage full", "out of space")
_TRANSIENT_MARKERS = ("timed out", "timeout", "connection", "temporarily", "502", "503", "504")


def classify_gpmc_error(exc: BaseException) -> ErrorClass:
    """Map a gpmc/requests failure onto our taxonomy.

    gpmc raises few typed exceptions, so most classification is textual. Refine
    this against real failures; the cost of a wrong guess is a retry, except for
    AUTH_INVALID and QUOTA_EXHAUSTED, which halt transfer.
    """
    from gpmc.exceptions import UploadRejectedError

    if isinstance(exc, UploadRejectedError):
        return ErrorClass.UNSUPPORTED_MEDIA
    if isinstance(exc, ConnectionError | TimeoutError):
        return ErrorClass.TRANSIENT
    text = str(exc).lower()
    if any(m in text for m in _AUTH_MARKERS):
        return ErrorClass.AUTH_INVALID
    if any(m in text for m in _RATE_MARKERS):
        return ErrorClass.RATE_LIMITED
    if any(m in text for m in _QUOTA_MARKERS):
        return ErrorClass.QUOTA_EXHAUSTED
    if any(m in text for m in _TRANSIENT_MARKERS):
        return ErrorClass.TRANSIENT
    return ErrorClass.UNKNOWN


class GpmcClient:
    """gpmc-backed implementation.

    gpmc's Client holds a requests.Session and does its own internal threading;
    its thread-safety is unverified, so every worker thread gets its own.
    """

    def __init__(self, auth_data: str, quality: Quality = "original", timeout: int = 60) -> None:
        self._auth_data = auth_data
        self._quality = quality
        self._timeout = timeout
        self._local = threading.local()

    @property
    def _client(self):  # noqa: ANN202 - gpmc has no public type export
        existing = getattr(self._local, "client", None)
        if existing is None:
            from gpmc import Client

            existing = Client(auth_data=self._auth_data, timeout=self._timeout, log_level="WARNING")
            self._local.client = existing
        return existing

    def _guard(self, action: str, fn, *args, **kwargs):  # noqa: ANN001, ANN202
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, then classified
            raise GPhotosError(f"{action} failed: {exc}", classify_gpmc_error(exc)) from exc

    def exists(self, checksum: str) -> str | None:
        return self._guard("hash lookup", self._client.get_media_key_by_hash, checksum)

    def upload(self, path: Path, *, checksum: str, filename: str) -> str:
        result = self._guard(
            "upload",
            self._client.upload,
            target={path: {"hash": checksum, "filename": filename}},
            use_quota=self._quality == "quota",
            saver=self._quality == "saver",
            show_progress=False,
            threads=1,
        )
        if not result:
            raise GPhotosError("upload returned no media key", ErrorClass.UNKNOWN)
        return next(iter(result.values()))

    def create_album(self, name: str, media_keys: Sequence[str]) -> str:
        return self._guard("album creation", self._client.api.create_album, name, list(media_keys))

    def add_to_album(self, album_id: str, media_keys: Sequence[str]) -> None:
        keys = list(media_keys)
        for start in range(0, len(keys), ALBUM_BATCH):
            self._guard(
                "album update",
                self._client.api.add_media_to_album,
                album_id,
                keys[start : start + ALBUM_BATCH],
            )

    def trash(self, checksums: Sequence[str]) -> None:
        self._guard("trash", self._client.move_to_trash, list(checksums))
