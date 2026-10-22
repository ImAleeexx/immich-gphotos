"""Star ownership shared by every service account that uses one Google account.

Each service account keeps its own database, so two of them backing up into
the *same* Google account (say, two Immich users whose libraries both hold
the same photo, deduped by Google onto one item) used to reconcile that
item's star independently -- one starring, the other unstarring, every
sweep. Google offers no per-app ownership of a star and no cheap read-back,
so the only fix is to agree before writing: this ledger lives in the
process-wide control database, keyed by a stable Google account identity
(the account e-mail gpmc authenticates as), and every account publishes its
*intent* for an item there before deciding.

What it can and cannot do:
- Accounts in this one deployment that share a Google account converge:
  starred if any of them wants it starred; unstarred only when none does,
  none is in flux, and some account's own service put the star there.
- Two separate deployments (different data directories / containers) that
  share a Google account cannot see each other's ledger. Nothing on
  Google's side lets them coordinate; that remains unsolved and is
  documented as such.
"""

import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl

LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS favorite_claim (
  google_account TEXT NOT NULL,
  media_key      TEXT NOT NULL,
  account_id     TEXT NOT NULL,
  intent         INTEGER,          -- 1 star, 0 unstar, NULL held (sources in flux)
  managed        INTEGER NOT NULL DEFAULT 0,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (google_account, media_key, account_id)
);
CREATE TABLE IF NOT EXISTS favorite_item (
  google_account TEXT NOT NULL,
  media_key      TEXT NOT NULL,
  pushed         INTEGER,
  updated_at     TEXT NOT NULL,
  PRIMARY KEY (google_account, media_key)
);
"""


def google_identity(auth_data: str | None) -> str | None:
    """The Google account an auth_data string authenticates as, normalised.

    gpmc requires an `Email` field (it refuses auth_data without one), so
    this is stable across re-captures of the same account and differs
    between accounts. None when absent: such an account simply does not
    share a ledger with anyone.
    """
    if not auth_data:
        return None
    for key, value in parse_qsl(auth_data.strip(), keep_blank_values=True):
        if key == "Email" and value.strip():
            return value.strip().lower()
    return None


@dataclass(frozen=True)
class Claim:
    account_id: str
    intent: bool | None
    managed: bool


class SharedFavoriteLedger:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        lock = getattr(conn, "lock", None)
        self._db_lock = lock if lock is not None else threading.RLock()
        with self._db_lock:
            conn.executescript(LEDGER_SCHEMA)
        self._item_locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()

    def lock_for(self, google_account: str, media_key: str) -> threading.Lock:
        """One lock per Google item across every account in the process."""
        with self._guard:
            key = (google_account, media_key)
            lock = self._item_locks.get(key)
            if lock is None:
                lock = self._item_locks[key] = threading.Lock()
            return lock

    def publish(
        self, google_account: str, media_key: str, account_id: str, *, intent: bool | None, managed: bool
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._db_lock:
            self._conn.execute(
                "INSERT INTO favorite_claim"
                " (google_account, media_key, account_id, intent, managed, updated_at)"
                " VALUES (?,?,?,?,?,?) ON CONFLICT(google_account, media_key, account_id) DO UPDATE SET"
                " intent = excluded.intent, managed = MAX(managed, excluded.managed),"
                " updated_at = excluded.updated_at",
                (
                    google_account,
                    media_key,
                    account_id,
                    None if intent is None else int(intent),
                    int(managed),
                    now,
                ),
            )

    def others(self, google_account: str, media_key: str, account_id: str) -> list[Claim]:
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT account_id, intent, managed FROM favorite_claim"
                " WHERE google_account = ? AND media_key = ? AND account_id != ?",
                (google_account, media_key, account_id),
            ).fetchall()
        return [Claim(r[0], None if r[1] is None else bool(r[1]), bool(r[2])) for r in rows]

    def pushed(self, google_account: str, media_key: str) -> bool | None:
        with self._db_lock:
            row = self._conn.execute(
                "SELECT pushed FROM favorite_item WHERE google_account = ? AND media_key = ?",
                (google_account, media_key),
            ).fetchone()
        if row is None or row[0] is None:
            return None
        return bool(row[0])

    def record_pushed(self, google_account: str, media_key: str, pushed: bool | None) -> None:
        now = datetime.now(UTC).isoformat()
        with self._db_lock:
            self._conn.execute(
                "INSERT INTO favorite_item (google_account, media_key, pushed, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(google_account, media_key) DO UPDATE SET"
                " pushed = excluded.pushed, updated_at = excluded.updated_at",
                (google_account, media_key, None if pushed is None else int(pushed), now),
            )

    def accounts_sharing(self, google_account: str) -> set[str]:
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT DISTINCT account_id FROM favorite_claim WHERE google_account = ?", (google_account,)
            ).fetchall()
        return {r[0] for r in rows}

    def forget_account(self, account_id: str) -> None:
        """A removed account's intents must not keep a star pinned forever."""
        with self._db_lock:
            self._conn.execute("DELETE FROM favorite_claim WHERE account_id = ?", (account_id,))
