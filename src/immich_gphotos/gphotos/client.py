import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

# gpmc.client runs `signal.signal(signal.SIGINT, signal.SIG_DFL)` at module
# import time (to make Ctrl+C cancel its internal threads). signal.signal
# raises ValueError outside the main thread of the main interpreter, so
# `import gpmc` fails unconditionally whenever it first happens off the main
# thread. In this service that is every non-main-thread caller: FastAPI's
# sync-route threadpool and the background worker thread. Do NOT move this
# back to a lazy import inside a property/method "to keep imports cheap" -
# that is what caused every Google operation to fail in production with a
# `ValueError: signal only works in main thread of the main interpreter`,
# misreported as an authentication failure. Importing here, at our own
# module's top level, forces gpmc's import to happen when this module is
# first imported - which is during normal application startup on the main
# thread (see main.py's module-level imports) - long before any worker
# thread would otherwise trigger it.
import gpmc
import requests.exceptions
from gpmc.exceptions import UploadRejectedError

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
_TRANSIENT_MARKERS = ("timed out", "timeout", "connection", "temporarily", "500", "502", "503", "504")


def _is_signal_thread_error(exc: BaseException) -> bool:
    """True for gpmc's `signal.signal(...)` import-time failure.

    `signal.signal` raises `ValueError("signal only works in main thread of
    the main interpreter")` when gpmc is imported off the main thread. That is
    an internal import/threading defect, never a credential problem, and must
    never be classified or force-promoted as AUTH_INVALID.
    """
    return isinstance(exc, ValueError) and "signal" in str(exc).lower() and "thread" in str(exc).lower()


def classify_gpmc_error(exc: BaseException) -> ErrorClass:
    """Map a gpmc/requests failure onto our taxonomy.

    gpmc raises few typed exceptions, so most classification is textual. Refine
    this against real failures; the cost of a wrong guess is a retry, except for
    AUTH_INVALID and QUOTA_EXHAUSTED, which halt transfer.

    Quota markers are checked before auth markers: Google surfaces storage and
    rate quota errors as HTTP 403 with a quota reason string, and misrouting
    those to AUTH_INVALID sends someone to re-extract auth_data for no reason.

    A `ValueError` complaining about signals/threads is not a credential
    problem at all - it is gpmc's module-level `signal.signal(...)` call
    failing because something imported gpmc off the main thread. That must
    never be reported to the user as AUTH_INVALID (which halts the service
    and tells them to re-extract auth_data): it is an internal import/startup
    defect, so it is classified as UNKNOWN rather than matched against the
    auth markers below.
    """
    if isinstance(exc, UploadRejectedError):
        return ErrorClass.UNSUPPORTED_MEDIA
    if isinstance(
        exc,
        ConnectionError | TimeoutError | requests.exceptions.ConnectionError | requests.exceptions.Timeout,
    ):
        return ErrorClass.TRANSIENT
    if _is_signal_thread_error(exc):
        return ErrorClass.UNKNOWN
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
    def quality(self) -> Quality:
        return self._quality

    @quality.setter
    def quality(self, value: Quality) -> None:
        """Let `composition.rebuild_runtime` update the live quality in place.

        `upload()` reads `self._quality` fresh on every call, so mutating it
        here takes effect on the very next upload -- no client reconstruction,
        no thread-local gpmc.Client re-authentication needed. This is what
        lets a settings change (API PUT or the wizard's options step) actually
        change what gets uploaded, instead of only updating `Settings` while
        the client already in use keeps whatever quality it was built with.
        """
        self._quality = value

    @property
    def _client(self):  # noqa: ANN202 - gpmc has no public type export
        existing = getattr(self._local, "client", None)
        if existing is None:
            # gpmc itself is imported eagerly at module scope (see top of
            # file); this only constructs a new instance for this thread.
            existing = gpmc.Client(auth_data=self._auth_data, timeout=self._timeout, log_level="WARNING")
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
        a genuinely unrecognised local failure there (classify_gpmc_error
        returns UNKNOWN — e.g. a KeyError/ValueError from parsing a malformed
        `auth_data` string) is by definition an `auth_data` problem: force it
        to AUTH_INVALID even though the message didn't match an auth marker.

        Anything classify_gpmc_error already has an opinion about is passed
        through unchanged. In particular TRANSIENT (a connection error or
        timeout), RATE_LIMITED and QUOTA_EXHAUSTED must not be forced to
        AUTH_INVALID: "not recognised as an auth problem" and "not transient"
        are different questions, and a construction-phase 429 or 5xx from
        Google's auth endpoint is a passing Google-side problem, not a bad
        credential — forcing it would halt the whole service and demand a
        needless re-extraction of `auth_data` from an Android device.

        Likewise, a `ValueError` about signals/threads (gpmc's module-level
        `signal.signal` call failing when imported off the main thread) is an
        internal defect, never a bad credential: it must not be force-promoted
        to AUTH_INVALID here either, or the same production failure this file
        was fixed for would resurface as a misleading "authentication failed"
        the moment gpmc's import is (re-)triggered off the main thread.
        """
        try:
            client = self._client
        except Exception as exc:  # noqa: BLE001 - deliberately broad, then classified
            error_class = classify_gpmc_error(exc)
            if error_class is ErrorClass.UNKNOWN and not _is_signal_thread_error(exc):
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
