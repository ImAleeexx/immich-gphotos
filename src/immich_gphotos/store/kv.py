import json
import sqlite3


class CursorRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, name: str) -> str | None:
        row = self._conn.execute("SELECT value FROM cursor WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def set(self, name: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO cursor (name, value) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, value),
        )

    def delete(self, name: str) -> None:
        self._conn.execute("DELETE FROM cursor WHERE name = ?", (name,))


class SettingRepo:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str) -> object | None:
        row = self._conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def set(self, key: str, value: object) -> None:
        self._conn.execute(
            "INSERT INTO setting (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )
