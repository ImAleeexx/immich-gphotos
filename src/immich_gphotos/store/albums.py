import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class AlbumMapping:
    immich_album_id: str
    gp_album_id: str
    name: str
    item_count: int
    overflow_of: str | None


class AlbumRepo:
    """Maps Immich albums to Google albums, including overflow albums.

    Overflow rows are keyed `<immich_album_id>#2`, `#3` and so on, because Google
    caps an album at 20,000 items and an Immich album may exceed it.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def mapping(self, key: str) -> AlbumMapping | None:
        with self._conn.lock:
            row = self._conn.execute("SELECT * FROM album_map WHERE immich_album_id = ?", (key,)).fetchone()
        if not row:
            return None
        return AlbumMapping(
            immich_album_id=row["immich_album_id"],
            gp_album_id=row["gp_album_id"],
            name=row["name"],
            item_count=row["item_count"],
            overflow_of=row["overflow_of"],
        )

    def chain(self, immich_album_id: str) -> list[AlbumMapping]:
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT * FROM album_map WHERE immich_album_id = ? OR overflow_of = ?"
                " ORDER BY immich_album_id ASC",
                (immich_album_id, immich_album_id),
            ).fetchall()
        return [
            AlbumMapping(
                immich_album_id=r["immich_album_id"],
                gp_album_id=r["gp_album_id"],
                name=r["name"],
                item_count=r["item_count"],
                overflow_of=r["overflow_of"],
            )
            for r in rows
        ]

    def put(
        self,
        key: str,
        gp_album_id: str,
        name: str,
        item_count: int = 0,
        overflow_of: str | None = None,
    ) -> None:
        with self._conn.lock:
            self._conn.execute(
                "INSERT INTO album_map (immich_album_id, gp_album_id, name, item_count, overflow_of)"
                " VALUES (?,?,?,?,?) ON CONFLICT(immich_album_id) DO UPDATE SET"
                " gp_album_id = excluded.gp_album_id, name = excluded.name",
                (key, gp_album_id, name, item_count, overflow_of),
            )

    def bump(self, key: str, n: int) -> None:
        with self._conn.lock:
            self._conn.execute(
                "UPDATE album_map SET item_count = item_count + ? WHERE immich_album_id = ?", (n, key)
            )

    def is_member_added(self, immich_album_id: str, asset_id: str) -> bool:
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT 1 FROM album_member WHERE immich_album_id = ? AND immich_asset_id = ?"
                " AND state = 'added'",
                (immich_album_id, asset_id),
            ).fetchone()
        return row is not None

    def mark_added(self, immich_album_id: str, asset_id: str) -> None:
        with self._conn.lock:
            self._conn.execute(
                "INSERT INTO album_member (immich_album_id, immich_asset_id, state)"
                " VALUES (?,?,'added') ON CONFLICT(immich_album_id, immich_asset_id)"
                " DO UPDATE SET state = 'added'",
                (immich_album_id, asset_id),
            )
