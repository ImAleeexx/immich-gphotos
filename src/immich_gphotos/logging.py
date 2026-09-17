import json
import logging
import re
import sys
from collections.abc import Iterable

PLACEHOLDER = "[redacted]"

# Belt and braces: even an unregistered auth_data blob is scrubbed by shape.
_PATTERNS = [re.compile(r"androidId=[^\s&]+(&app=[^\s]+)?", re.IGNORECASE)]


class Redactor:
    def __init__(self, secrets: Iterable[str | None]) -> None:
        self._secrets = sorted((s for s in secrets if s), key=len, reverse=True)

    def scrub(self, text: str) -> str:
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


def configure_logging(level: str = "INFO", secrets: Iterable[str | None] = ()) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(Redactor(secrets)))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
