import json
import logging
import re
import sys
import threading
from collections.abc import Iterable

PLACEHOLDER = "[redacted]"

# Belt and braces: even an unregistered auth_data blob is scrubbed by shape.
_PATTERNS = [re.compile(r"androidId=[^\s&]+(&app=[^\s]+)?", re.IGNORECASE)]


class Redactor:
    def __init__(self, secrets: Iterable[str | None]) -> None:
        self._secrets = sorted((s for s in secrets if s), key=len, reverse=True)
        # FINDING M3: `add_secret` is read-then-reassign, and one `Redactor`
        # is now shared by every account (see `AccountRegistry`'s class
        # docstring). Two wizards completing at the same moment on different
        # accounts -- two request threads -- both read the same `_secrets`
        # list and both write a list built from it, so the second write drops
        # the first's secret and one account's credential goes unscrubbed
        # from the shared log stream for the life of the process. There is no
        # error and nothing to notice: the logs simply contain a live
        # credential. Only the reassignment needs the lock.
        self._lock = threading.Lock()

    def add_secret(self, secret: str | None) -> None:
        """Register a credential discovered after construction.

        The wizard persists a new Immich API key or Google `auth_data` well
        after `configure_logging` and the stores were built with whatever was
        in the database at boot (nothing, on a fresh install). Without this,
        a credential entered through the wizard would never be scrubbed from
        logs or stored events for the lifetime of the process.

        The membership check is inside the lock too: outside it, two threads
        adding the *same* secret could both find it absent and append it
        twice -- harmless in output, but it would make the list grow on every
        retry of a wizard step.
        """
        if not secret:
            return
        with self._lock:
            if secret in self._secrets:
                return
            self._secrets = sorted((*self._secrets, secret), key=len, reverse=True)

    def scrub(self, text: str) -> str:
        # Deliberately unlocked, and on the hot path for every log line and
        # every stored event: it only ever reads `self._secrets`, and
        # `add_secret` rebinds that attribute rather than mutating the list
        # in place, so this either sees the whole old list or the whole new
        # one -- never a list being appended to underneath it.
        for secret in self._secrets:
            text = text.replace(secret, PLACEHOLDER)
        for pattern in _PATTERNS:
            text = pattern.sub(PLACEHOLDER, text)
        return text


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": self._redactor.scrub(record.name),
            "message": self._redactor.scrub(record.getMessage()),
        }
        if record.exc_info:
            payload["exc_info"] = self._redactor.scrub(self.formatException(record.exc_info))
        return json.dumps(payload)


def configure_logging(
    level: str = "INFO", secrets: Iterable[str | None] = (), redactor: Redactor | None = None
) -> None:
    """Install the JSON logging handler.

    `redactor`, when passed, is installed as-is rather than a fresh copy built
    from `secrets` — callers that also hand the same instance to the store
    layer (see `accounts.build.build_account_services`) get one Redactor whose `add_secret`
    later reaches both logs and persisted events, not two copies that drift
    apart the moment a credential is added after boot.
    """
    if redactor is None:
        redactor = Redactor(secrets)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(redactor))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
