import sqlite3

from immich_gphotos.clock import Clock


class EventRepo:
    """A bounded ring of recent activity, for the UI's diagnostics view."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock, limit: int = 2000) -> None:
        self._conn = conn
        self._clock = clock
        self._limit = limit

    def add(self, level: str, message: str, asset_id: str | None = None) -> None:
        with self._conn.lock:
            self._conn.execute(
                "INSERT INTO event (ts, level, asset_id, message) VALUES (?,?,?,?)",
                (self._clock.now().isoformat(), level, asset_id, message[:500]),
            )
            self._conn.execute(
                "DELETE FROM event WHERE id NOT IN (SELECT id FROM event ORDER BY id DESC LIMIT ?)",
                (self._limit,),
            )

    def recent(self, n: int = 100) -> list[dict]:
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT ts, level, asset_id, message FROM event ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [dict(r) for r in rows]
