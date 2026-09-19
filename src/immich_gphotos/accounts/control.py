import secrets
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from immich_gphotos.store.db import connect

CONTROL_DB_NAME = "control.db"

# The `setting` table is deliberately identical to the per-account one, so
# `store.kv.SettingRepo` drives both. This one holds the admin password, the
# session token and the global half of the settings row; an account's holds
# that account's credentials and its own half. They never mix, because they
# are different files.
CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
  id         TEXT PRIMARY KEY,
  label      TEXT NOT NULL,
  created_at TEXT NOT NULL,
  position   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect_control(path: Path) -> sqlite3.Connection:
    """Open the control database.

    `migrations=()` is load-bearing: `store.schema.COLUMN_MIGRATIONS` ALTERs
    the `asset` table, which exists only in an account's database. Running
    them here raises `no such table: asset` and makes the container
    unstartable.
    """
    return connect(path, schema=CONTROL_SCHEMA, migrations=())


def new_account_id() -> str:
    """Opaque, so renaming an account never has to move a directory or
    invalidate a webhook URL already registered in someone's Immich."""
    return secrets.token_hex(6)


@dataclass(frozen=True)
class AccountRecord:
    id: str
    label: str
    created_at: str
    position: int


class AccountRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def list(self) -> list[AccountRecord]:
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT id, label, created_at, position FROM account ORDER BY position, id"
            ).fetchall()
        return [AccountRecord(r["id"], r["label"], r["created_at"], r["position"]) for r in rows]

    def get(self, account_id: str) -> AccountRecord | None:
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT id, label, created_at, position FROM account WHERE id = ?", (account_id,)
            ).fetchone()
        return AccountRecord(row["id"], row["label"], row["created_at"], row["position"]) if row else None

    def add(self, *, account_id: str, label: str, created_at: str) -> AccountRecord:
        with self._conn.lock:
            row = self._conn.execute("SELECT COALESCE(MAX(position) + 1, 0) AS n FROM account").fetchone()
            position = row["n"]
            self._conn.execute(
                "INSERT INTO account (id, label, created_at, position) VALUES (?, ?, ?, ?)",
                (account_id, label, created_at, position),
            )
        return AccountRecord(account_id, label, created_at, position)

    def rename(self, account_id: str, label: str) -> None:
        with self._conn.lock:
            self._conn.execute("UPDATE account SET label = ? WHERE id = ?", (label, account_id))

    def remove(self, account_id: str) -> None:
        with self._conn.lock:
            self._conn.execute("DELETE FROM account WHERE id = ?", (account_id,))
