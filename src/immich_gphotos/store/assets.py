import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime, timedelta

from immich_gphotos.clock import Clock
from immich_gphotos.logging import Redactor
from immich_gphotos.models import (
    ALBUM_EXCLUDED_REASON,
    Asset,
    AssetState,
    ErrorClass,
    Outcome,
    Priority,
    StoredAsset,
)


def _row_to_stored(row: sqlite3.Row) -> StoredAsset:
    asset = Asset(
        immich_id=row["immich_id"],
        checksum=row["checksum"],
        filename=row["filename"],
        type=row["type"],
        size_bytes=row["size_bytes"],
        immich_updated_at=row["immich_updated_at"],
        original_path=row["original_path"],
        visibility=row["visibility"],
        is_offline=bool(row["is_offline"]),
        is_trashed=bool(row["is_trashed"]),
        tags=tuple(json.loads(row["tags"])),
    )
    return StoredAsset(
        asset=asset,
        state=AssetState(row["state"]),
        outcome=Outcome(row["outcome"]) if row["outcome"] else None,
        media_key=row["media_key"],
        priority=Priority(row["priority"]),
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        error_class=ErrorClass(row["error_class"]) if row["error_class"] else None,
        last_error=row["last_error"],
        ineligible_reason=row["ineligible_reason"],
    )


class AssetRepo:
    """The asset table is also the work queue."""

    def __init__(self, conn: sqlite3.Connection, clock: Clock, redactor: Redactor | None = None) -> None:
        self._conn = conn
        self._clock = clock
        # Defaults to a secret-less Redactor: it still scrubs the auth_data
        # shape by pattern, so persisted error text is never worse off even
        # when a caller (tests, older call sites) does not wire one in.
        self._redactor = redactor if redactor is not None else Redactor(())

    def upsert_pending(self, asset: Asset, priority: Priority) -> bool:
        """Enqueue an asset. Returns False if it is already in a terminal state.

        Never downgrades an existing priority: a webhook arriving for an asset the
        backfill already queued must jump the queue, not sink into it.

        Every terminal state (SYNCED, or INELIGIBLE for any reason except
        `ALBUM_EXCLUDED_REASON`) is otherwise left exactly as found -- this
        call never resurrects a row a previous pass decided was done or
        permanently ineligible. `ALBUM_EXCLUDED_REASON` is the one exception:
        album membership is mutable and re-resolved every tick (see
        `sync.eligibility.check_eligibility`), so a row parked there is
        reopened back to PENDING (and re-prioritized like any other
        non-terminal row) instead of staying stuck. Without this, a webhook
        or reconciler pass touching the row again would have no effect --
        `claim_next` only ever claims PENDING rows -- and an asset added to
        an allowed album after the fact would never sync.
        """
        now = self._clock.now().isoformat()
        with self._conn.lock:
            row = self._conn.execute(
                "INSERT INTO asset (immich_id, checksum, filename, type, size_bytes,"
                " immich_updated_at, original_path, visibility, is_offline, is_trashed, tags,"
                " state, priority, first_seen_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(immich_id) DO UPDATE SET"
                "  state = CASE"
                "    WHEN state = ? AND ineligible_reason = ? THEN ?"
                "    ELSE state"
                "  END,"
                "  priority = CASE"
                "    WHEN state = ? THEN priority"
                "    WHEN state = ? AND ineligible_reason != ? THEN priority"
                "    ELSE MIN(priority, excluded.priority)"
                "  END,"
                "  ineligible_reason = CASE"
                "    WHEN state = ? AND ineligible_reason = ? THEN NULL"
                "    ELSE ineligible_reason"
                "  END,"
                "  is_trashed = excluded.is_trashed,"
                "  visibility = excluded.visibility,"
                "  immich_updated_at = excluded.immich_updated_at,"
                "  is_offline = excluded.is_offline,"
                "  tags = excluded.tags"
                " RETURNING state",
                (
                    asset.immich_id,
                    asset.checksum,
                    asset.filename,
                    asset.type,
                    asset.size_bytes,
                    asset.immich_updated_at,
                    asset.original_path,
                    asset.visibility,
                    int(asset.is_offline),
                    int(asset.is_trashed),
                    json.dumps(list(asset.tags)),
                    AssetState.PENDING.value,
                    int(priority),
                    now,
                    # state CASE: reopen a soft (album_excluded) ineligibility.
                    AssetState.INELIGIBLE.value,
                    ALBUM_EXCLUDED_REASON,
                    AssetState.PENDING.value,
                    # priority CASE: SYNCED keeps its priority unconditionally;
                    # INELIGIBLE keeps it only for a genuinely terminal reason.
                    # A reopened album_excluded row falls through to the
                    # MIN(...) branch like any other non-terminal row.
                    AssetState.SYNCED.value,
                    AssetState.INELIGIBLE.value,
                    ALBUM_EXCLUDED_REASON,
                    # ineligible_reason CASE: clear it along with the reopen
                    # above, so a row back in PENDING does not keep showing a
                    # stale reason it no longer has.
                    AssetState.INELIGIBLE.value,
                    ALBUM_EXCLUDED_REASON,
                ),
            ).fetchone()
        return not AssetState(row["state"]).is_terminal()

    def claim_next(self, limit: int = 1) -> list[StoredAsset]:
        now = self._clock.now().isoformat()
        with self._conn.lock:
            rows = self._conn.execute(
                "UPDATE asset SET state = ?, claimed_at = ? WHERE immich_id IN ("
                "  SELECT immich_id FROM asset WHERE state = ?"
                "   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                "   ORDER BY priority ASC, first_seen_at ASC LIMIT ?"
                ") RETURNING *",
                (AssetState.UPLOADING.value, now, AssetState.PENDING.value, now, limit),
            ).fetchall()
        return [_row_to_stored(r) for r in rows]

    def mark_synced(self, immich_id: str, media_key: str, outcome: Outcome) -> None:
        with self._conn.lock:
            self._conn.execute(
                "UPDATE asset SET state = ?, outcome = ?, media_key = ?, synced_at = ?,"
                " error_class = NULL, last_error = NULL, claimed_at = NULL WHERE immich_id = ?",
                (
                    AssetState.SYNCED.value,
                    outcome.value,
                    media_key,
                    self._clock.now().isoformat(),
                    immich_id,
                ),
            )

    def mark_ineligible(self, immich_id: str, reason: str) -> None:
        with self._conn.lock:
            self._conn.execute(
                "UPDATE asset SET state = ?, ineligible_reason = ?, claimed_at = NULL WHERE immich_id = ?",
                (AssetState.INELIGIBLE.value, reason, immich_id),
            )

    def mark_retry(
        self, immich_id: str, error_class: ErrorClass, message: str, next_attempt_at: datetime
    ) -> None:
        message = self._redactor.scrub(message)
        with self._conn.lock:
            self._conn.execute(
                "UPDATE asset SET state = ?, attempts = attempts + 1, next_attempt_at = ?,"
                " error_class = ?, last_error = ?, claimed_at = NULL WHERE immich_id = ?",
                (
                    AssetState.PENDING.value,
                    next_attempt_at.isoformat(),
                    error_class.value,
                    message[:500],
                    immich_id,
                ),
            )

    def requeue(self, immich_id: str, next_attempt_at: datetime) -> None:
        """Return a row to pending WITHOUT counting an attempt.

        Used when the failure belongs to the account rather than the asset, so a
        long credential outage cannot quarantine the whole library.
        """
        with self._conn.lock:
            self._conn.execute(
                "UPDATE asset SET state = ?, next_attempt_at = ?, claimed_at = NULL WHERE immich_id = ?",
                (AssetState.PENDING.value, next_attempt_at.isoformat(), immich_id),
            )

    def mark_failed(self, immich_id: str, error_class: ErrorClass, message: str) -> None:
        message = self._redactor.scrub(message)
        with self._conn.lock:
            self._conn.execute(
                "UPDATE asset SET state = ?, attempts = attempts + 1, error_class = ?,"
                " last_error = ?, claimed_at = NULL WHERE immich_id = ?",
                (AssetState.FAILED.value, error_class.value, message[:500], immich_id),
            )

    def get(self, immich_id: str) -> StoredAsset | None:
        with self._conn.lock:
            row = self._conn.execute("SELECT * FROM asset WHERE immich_id = ?", (immich_id,)).fetchone()
        return _row_to_stored(row) if row else None

    def counts_by_state(self) -> dict[str, int]:
        with self._conn.lock:
            rows = self._conn.execute("SELECT state, COUNT(*) AS n FROM asset GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    def media_key_for_checksum(self, checksum: str) -> str | None:
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT media_key FROM asset WHERE checksum = ? AND media_key IS NOT NULL LIMIT 1",
                (checksum,),
            ).fetchone()
        return row["media_key"] if row else None

    def synced_ids(self) -> set[str]:
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT immich_id FROM asset WHERE state = ?", (AssetState.SYNCED.value,)
            ).fetchall()
        return {r["immich_id"] for r in rows}

    def synced_count(self) -> int:
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM asset WHERE state = ?", (AssetState.SYNCED.value,)
            ).fetchone()
        return row["n"]

    def trashed_synced(self) -> list[StoredAsset]:
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT * FROM asset WHERE state = ? AND is_trashed = 1 AND media_key IS NOT NULL",
                (AssetState.SYNCED.value,),
            ).fetchall()
        return [_row_to_stored(r) for r in rows]

    def mark_deleted(self, ids: Sequence[str]) -> None:
        """Terminal, but keeps the media key so a re-added asset resolves instantly."""
        with self._conn.lock:
            self._conn.executemany(
                "UPDATE asset SET state = ?, ineligible_reason = 'deleted_from_immich' WHERE immich_id = ?",
                [(AssetState.INELIGIBLE.value, i) for i in ids],
            )

    def failures(self, limit: int = 200) -> list[StoredAsset]:
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT * FROM asset WHERE state = ? ORDER BY first_seen_at DESC LIMIT ?",
                (AssetState.FAILED.value, limit),
            ).fetchall()
        return [_row_to_stored(r) for r in rows]

    def retry_now(self, immich_id: str) -> bool:
        with self._conn.lock:
            cur = self._conn.execute(
                "UPDATE asset SET state = ?, attempts = 0, next_attempt_at = NULL,"
                " error_class = NULL, last_error = NULL WHERE immich_id = ? AND state = ?",
                (AssetState.PENDING.value, immich_id, AssetState.FAILED.value),
            )
            return cur.rowcount > 0

    def requeue_stale_uploading(self, older_than: timedelta) -> int:
        """Recover rows a crash left claimed."""
        cutoff = (self._clock.now() - older_than).isoformat()
        with self._conn.lock:
            cur = self._conn.execute(
                "UPDATE asset SET state = ?, claimed_at = NULL"
                " WHERE state = ? AND claimed_at IS NOT NULL AND claimed_at <= ?",
                (AssetState.PENDING.value, AssetState.UPLOADING.value, cutoff),
            )
            return cur.rowcount
