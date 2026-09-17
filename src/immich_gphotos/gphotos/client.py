import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import requests.exceptions

from immich_gphotos.config import Quality
from immich_gphotos.gphotos.protocol import GPhotosError
from immich_gphotos.models import ErrorClass

ALBUM_BATCH = 500

_AUTH_MARKERS = (
    "401",
    "forbidden",
    "unauthorized",
    "auth token",
    "auth_data",
    "authentication",
    "oauth2",
    "no email value",
)
_RATE_MARKERS = ("429", "rate limit", "too many requests")
_QUOTA_MARKERS = ("quota", "storage full", "out of space")
_TRANSIENT_MARKERS = ("timed out", "timeout", "connection", "temporarily", "502", "503", "504")


def classify_gpmc_error(exc: BaseException) -> ErrorClass:
    """Map a gpmc/requests failure onto our taxonomy.

    gpmc raises few typed exceptions, so most classification is textual. Refine
    this against real failures; the cost of a wrong guess is a retry, except for
    AUTH_INVALID and QUOTA_EXHAUSTED, which halt transfer.

    Quota markers are checked before auth markers: Google surfaces storage and
    rate quota errors as HTTP 403 with a quota reason string, and misrouting
    those to AUTH_INVALID sends someone to re-extract auth_data for no reason.
    """
    from gpmc.exceptions import UploadRejectedError

    if isinstance(exc, UploadRejectedError):
        return ErrorClass.UNSUPPORTED_MEDIA
    if isinstance(
        exc,
        ConnectionError | TimeoutError | requests.exceptions.ConnectionError | requests.exceptions.Timeout,
    ):
        return ErrorClass.TRANSIENT
    text = str(exc).lower()
    if any(m in text for m in _QUOTA_MARKERS):
        return ErrorClass.QUOTA_EXHAUSTED
    if any(m in text for m in _AUTH_MARKERS):
        return ErrorClass.AUTH_INVALID
    if any(m in text for m in _RATE_MARKERS):
        return ErrorClass.RATE_LIMITED
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

    def _guard(self, action: str, fn: Callable[[Any], Any]) -> Any:  # noqa: ANN401
        """Run `fn(client)`, acquiring the (possibly not-yet-constructed) client
        *inside* the guarded region and classifying whatever goes wrong.

        `self._client` is a property that constructs and authenticates gpmc's
        `Client` on first use per thread. If that construction/authentication
        step is evaluated by the caller before this try, its exceptions escape
        the facade entirely (raw `ValueError`, `KeyError`, ...); acquiring it
        here is what keeps every failure inside the `GPhotosError` boundary.

        Construction's only job is obtaining an auth token from `auth_data`, so
        a non-network failure there is by definition an `auth_data` problem —
        force it to AUTH_INVALID even if the message doesn't match a marker.
        A real connection error or timeout during that same step is a passing
        network blip, not a bad credential, so it is left as TRANSIENT rather
        than forced — forcing it would halt the service and demand a needless
        re-extraction of `auth_data` from an Android device.
        """
        try:
            client = self._client
        except Exception as exc:  # noqa: BLE001 - deliberately broad, then classified
            error_class = classify_gpmc_error(exc)
            if error_class is not ErrorClass.TRANSIENT:
                error_class = ErrorClass.AUTH_INVALID
            raise GPhotosError(f"authentication failed: {exc}", error_class) from exc

        try:
            return fn(client)
        except Exception as exc:  # noqa: BLE001 - deliberately broad, then classified
            raise GPhotosError(f"{action} failed: {exc}", classify_gpmc_error(exc)) from exc

    def exists(self, checksum: str) -> str | None:
        return self._guard("hash lookup", lambda client: client.get_media_key_by_hash(checksum))

    def upload(self, path: Path, *, checksum: str, filename: str) -> str:
        result = self._guard(
            "upload",
            lambda client: client.upload(
                target={path: {"hash": checksum, "filename": filename}},
                use_quota=self._quality == "quota",
                saver=self._quality == "saver",
                show_progress=False,
                threads=1,
            ),
        )
        if not result:
            raise GPhotosError("upload returned no media key", ErrorClass.UNKNOWN)
        return next(iter(result.values()))

    def create_album(self, name: str, media_keys: Sequence[str]) -> str:
        keys = list(media_keys)
        return self._guard("album creation", lambda client: client.api.create_album(name, keys))

    def add_to_album(self, album_id: str, media_keys: Sequence[str]) -> None:
        keys = list(media_keys)
        for start in range(0, len(keys), ALBUM_BATCH):
            batch = keys[start : start + ALBUM_BATCH]
            self._guard(
                "album update",
                lambda client, batch=batch: client.api.add_media_to_album(album_id, batch),
            )

    def trash(self, checksums: Sequence[str]) -> None:
        keys = list(checksums)
        self._guard("trash", lambda client: client.move_to_trash(keys))
