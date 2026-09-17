import sqlite3
import threading
from pathlib import Path

from immich_gphotos.store.schema import SCHEMA


class LockingConnection(sqlite3.Connection):
    """A `sqlite3.Connection` that carries its own re-entrant lock.

    `check_same_thread=False` lets one connection be shared across the webhook
    receiver, the reconciler and the worker pool, but a shared C-level
    connection is not safe for genuinely simultaneous `execute()` calls:
    interleaving corrupts statement and cursor state. The lock must live on
    the connection itself (not on any one repo) so that every repo sharing
    this connection serializes against every other. Use `RLock` so a method
    that calls another locking method on the same connection does not
    deadlock itself.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.lock = threading.RLock()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path, check_same_thread=False, isolation_level=None, factory=LockingConnection
    )
    # The database holds the Immich API key and Google auth_data.
    path.chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn
