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
        taken_at=row["taken_at"],
        live_photo_video_id=row["live_photo_video_id"],
        original_path=row["original_path"],
        visibility=row["visibility"],
        is_offline=bool(row["is_offline"]),
        is_trashed=bool(row["is_trashed"]),
        tags=tuple(json.loads(row["tags"])),
        is_favorite=bool(row["is_favorite"]),
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
        favorite_synced=bool(row["favorite_synced"]) if row["favorite_synced"] is not None else None,
        upload_sha1=row["upload_sha1"],
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
                " immich_updated_at, taken_at, live_photo_video_id, original_path, visibility,"
                " is_offline, is_trashed,"
                " tags, is_favorite, state, priority, first_seen_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
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
                "  taken_at = excluded.taken_at,"
                "  live_photo_video_id = excluded.live_photo_video_id,"
                "  visibility = excluded.visibility,"
                "  immich_updated_at = excluded.immich_updated_at,"
                "  is_offline = excluded.is_offline,"
                "  is_favorite = excluded.is_favorite,"
                "  tags = excluded.tags"
                " RETURNING state",
                (
                    asset.immich_id,
                    asset.checksum,
                    asset.filename,
                    asset.type,
                    asset.size_bytes,
                    asset.immich_updated_at,
                    asset.taken_at,
                    asset.live_photo_video_id,
                    asset.original_path,
                    asset.visibility,
                    int(asset.is_offline),
                    int(asset.is_trashed),
                    json.dumps(list(asset.tags)),
                    int(asset.is_favorite),
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
                "UPDATE asset SET state = ?, claimed_at = ?, bandwidth_deferred_at = NULL"
                " WHERE immich_id IN ("
                "  SELECT immich_id FROM asset WHERE state = ?"
                "   AND (next_attempt_at IS NULL OR next_attempt_at <= ?)"
                "   ORDER BY priority ASC, first_seen_at ASC LIMIT ?"
                ") RETURNING *",
                (AssetState.UPLOADING.value, now, AssetState.PENDING.value, now, limit),
            ).fetchall()
        return [_row_to_stored(r) for r in rows]

    def mark_synced(
        self,
        immich_id: str,
        media_key: str,
        outcome: Outcome,
        upload_sha1: str | None = None,
        notes: Sequence[str] = (),
    ) -> None:
        """Record a synced asset.

        `upload_sha1` is the base64 SHA-1 of the bytes described to Google
        (the resolver-certified content hash). Passed whenever bytes were
        resolved this round -- an upload or a content-hash dedup hit -- so
        the delete path can trash by the key Google actually knows; left
        None when the sync happened without touching bytes (checksum-keyed
        dedup hits), where the fallback in `sync.deletions` applies.

        `notes` are the resolution's non-fatal diagnostics (see
        `sync.bytes.TIMESTAMP_UNVERIFIED`), replacing whatever the row
        carried before.
        """
        with self._conn.lock:
            self._conn.execute(
                "UPDATE asset SET state = ?, outcome = ?, media_key = ?, upload_sha1 = ?,"
                " synced_at = ?, error_class = NULL, last_error = NULL, claimed_at = NULL,"
                " sync_notes = ?"
                " WHERE immich_id = ?",
                (
                    AssetState.SYNCED.value,
                    outcome.value,
                    media_key,
                    upload_sha1,
                    self._clock.now().isoformat(),
                    ",".join(sorted(set(notes))) or None,
                    immich_id,
                ),
            )

    def sync_note_counts(self) -> dict[str, int]:
        """How many SYNCED rows carry each diagnostic code."""
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT sync_notes FROM asset WHERE state = ? AND sync_notes IS NOT NULL",
                (AssetState.SYNCED.value,),
            ).fetchall()
        counts: dict[str, int] = {}
        for row in rows:
            for code in row["sync_notes"].split(","):
                if code:
                    counts[code] = counts.get(code, 0) + 1
        return counts

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

    def defer_for_bandwidth(self, immich_id: str, next_attempt_at: datetime) -> None:
        """Requeue a row whose upload wait was too long to sleep out inline,
        marking the deadline as derived from the bandwidth cap.

        Same as `requeue` (no attempt counted -- a cap the user configured is
        not the asset's fault) plus the marker. The marker is what lets a
        later cap change release exactly these rows: `next_attempt_at` alone
        cannot be told apart from a failure backoff, a halt retry or a
        schedule-window defer, and blanket-clearing those would defeat the
        backoff and re-hammer a failing remote. It is cleared again by
        `claim_next` the moment the row is picked up, so it only ever marks a
        row that is genuinely still parked on a cap-derived deadline.
        """
        with self._conn.lock:
            self._conn.execute(
                "UPDATE asset SET state = ?, next_attempt_at = ?, bandwidth_deferred_at = ?,"
                " claimed_at = NULL WHERE immich_id = ?",
                (
                    AssetState.PENDING.value,
                    next_attempt_at.isoformat(),
                    self._clock.now().isoformat(),
                    immich_id,
                ),
            )

    def release_bandwidth_deferrals(self) -> int:
        """Make every row parked on a cap-derived deadline claimable again.

        Called when the bandwidth cap setting changes (including being
        cleared), because those deadlines were arithmetic against the *old*
        cap: nothing else ever revisits them, so a user who raises or removes
        a cap would otherwise keep waiting out the old one -- days or weeks
        for a large backlog -- with no way to fix it short of editing the
        database by hand. The bucket is rebuilt with the graph, so the
        released rows are simply re-metered against the new cap (or not
        metered at all, if it was cleared) on their next claim.

        Only rows carrying the `bandwidth_deferred_at` marker are touched, so
        failure backoffs, halt retries and schedule-window defers keep their
        deadlines. Returns how many rows were released.
        """
        with self._conn.lock:
            cur = self._conn.execute(
                "UPDATE asset SET next_attempt_at = NULL, bandwidth_deferred_at = NULL"
                " WHERE state = ? AND bandwidth_deferred_at IS NOT NULL",
                (AssetState.PENDING.value,),
            )
            return cur.rowcount

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

    def retry_all_failed(self) -> int:
        """Return every quarantined asset to the queue at once.

        The same reset `retry_now` performs, for all FAILED rows in one
        statement -- the bulk form exists for the ordinary case of a fix
        landing after a class of uploads was failing for everyone (a remote
        defect repaired on this side, say), where per-asset retries are not
        a practical way back into the queue.
        """
        with self._conn.lock:
            cur = self._conn.execute(
                "UPDATE asset SET state = ?, attempts = 0, next_attempt_at = NULL,"
                " error_class = NULL, last_error = NULL WHERE state = ?",
                (AssetState.PENDING.value, AssetState.FAILED.value),
            )
            return cur.rowcount

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

    def mark_favorite_synced(self, immich_id: str, is_favorite: bool) -> None:
        """Record that Google now agrees with Immich about starring.

        Only ever written after a successful `set_favorite` call -- never
        alongside the desired `is_favorite` itself -- so the gap between the
        two columns is exactly the drift `favorite_drift` looks for.
        """
        with self._conn.lock:
            self._conn.execute(
                "UPDATE asset SET favorite_synced = ? WHERE immich_id = ?",
                (int(is_favorite), immich_id),
            )

    # --- Live Photos (see the live_photo table) -----------------------------

    _LIVE_FIELDS = (
        "still_sha1",
        "video_sha1",
        "video_media_key",
        "derived_sha1",
        "derived_media_key",
    )

    def live_photo(self, still_id: str) -> sqlite3.Row | None:
        with self._conn.lock:
            return self._conn.execute("SELECT * FROM live_photo WHERE still_id = ?", (still_id,)).fetchone()

    def record_live_photo(
        self,
        still_id: str,
        video_id: str,
        *,
        mode: str,
        status: str,
        reason: str | None = None,
        attempts: int | None = None,
        next_attempt_at: datetime | None = None,
        **identities: str | None,
    ) -> None:
        """Upsert one Live Photo's state. Identity columns not passed are kept."""
        unknown = set(identities) - set(self._LIVE_FIELDS)
        if unknown:
            raise ValueError(f"unknown live_photo fields: {sorted(unknown)}")
        now = self._clock.now().isoformat()
        cols = ["still_id", "video_id", "mode", "status", "reason", "next_attempt_at", "updated_at"]
        vals: list[object] = [
            still_id,
            video_id,
            mode,
            status,
            reason,
            next_attempt_at.isoformat() if next_attempt_at else None,
            now,
        ]
        if attempts is not None:
            cols.append("attempts")
            vals.append(attempts)
        for key, value in identities.items():
            cols.append(key)
            vals.append(value)
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "still_id")
        with self._conn.lock:
            self._conn.execute(
                f"INSERT INTO live_photo ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
                f" ON CONFLICT(still_id) DO UPDATE SET {updates}",
                vals,
            )

    def live_photo_for_video(self, video_id: str) -> sqlite3.Row | None:
        with self._conn.lock:
            return self._conn.execute(
                "SELECT * FROM live_photo WHERE video_id = ? ORDER BY updated_at DESC LIMIT 1", (video_id,)
            ).fetchone()

    def live_photos_due(self, mode: str, limit: int = 50) -> list[sqlite3.Row]:
        """Pending Live Photos under `mode` whose still is SYNCED and whose retry is due."""
        now = self._clock.now().isoformat()
        with self._conn.lock:
            return self._conn.execute(
                "SELECT l.* FROM live_photo l JOIN asset a ON a.immich_id = l.still_id"
                " WHERE l.mode = ? AND l.status = 'pending' AND a.state = ?"
                " AND (l.next_attempt_at IS NULL OR l.next_attempt_at <= ?)"
                " ORDER BY l.updated_at ASC LIMIT ?",
                (mode, AssetState.SYNCED.value, now, limit),
            ).fetchall()

    def live_photo_summary(self) -> dict:
        """Read-only plan: Live Photo states, and SYNCED stills never assessed.

        `untracked_synced_stills` counts stills synced before Live Photo
        tracking existed (or under a build that did not record it): their
        motion was never backed up by this service. Nothing here changes
        them; `request_live_photo_reprocess` is the explicit way to.
        """
        with self._conn.lock:
            groups = self._conn.execute(
                "SELECT mode, status, reason, COUNT(*) AS n FROM live_photo"
                " GROUP BY mode, status, reason ORDER BY mode, status, reason"
            ).fetchall()
            untracked = self._conn.execute(
                "SELECT COUNT(*) AS n FROM asset a WHERE a.state = ? AND a.live_photo_video_id IS NOT NULL"
                " AND NOT EXISTS (SELECT 1 FROM live_photo l WHERE l.still_id = a.immich_id)",
                (AssetState.SYNCED.value,),
            ).fetchone()["n"]
        return {
            "groups": [
                {"mode": g["mode"], "status": g["status"], "reason": g["reason"], "count": g["n"]}
                for g in groups
            ],
            "untracked_synced_stills": untracked,
        }

    def request_live_photo_reprocess(self, mode: str, still_ids: Sequence[str] | None = None) -> int:
        """Explicitly queue SYNCED Live Photo stills for motion backup under `mode`.

        Only stills whose motion is not already complete under `mode` are
        queued. The still's own row (its Google item, hash, albums) is not
        touched -- reprocessing adds the missing motion item, it never
        replaces, re-uploads or deletes what is already there. Returns how
        many were queued.
        """
        now = self._clock.now().isoformat()
        params: list[object] = [AssetState.SYNCED.value]
        where = "a.state = ? AND a.live_photo_video_id IS NOT NULL"
        if still_ids is not None:
            if not still_ids:
                return 0
            where += f" AND a.immich_id IN ({', '.join('?' for _ in still_ids)})"
            params.extend(still_ids)
        with self._conn.lock:
            rows = self._conn.execute(
                f"SELECT a.immich_id, a.live_photo_video_id, l.mode, l.status FROM asset a"
                f" LEFT JOIN live_photo l ON l.still_id = a.immich_id WHERE {where}",
                params,
            ).fetchall()
            queued = 0
            for row in rows:
                if row["mode"] == mode and row["status"] == "complete":
                    continue
                self._conn.execute(
                    "INSERT INTO live_photo (still_id, video_id, mode, status, reason, attempts,"
                    " next_attempt_at, updated_at) VALUES (?, ?, ?, 'pending', 'reprocess_requested', 0,"
                    " NULL, ?) ON CONFLICT(still_id) DO UPDATE SET mode = excluded.mode,"
                    " status = excluded.status, reason = excluded.reason, attempts = 0,"
                    " next_attempt_at = NULL, updated_at = excluded.updated_at",
                    (row["immich_id"], row["live_photo_video_id"], mode, now),
                )
                queued += 1
        return queued

    def mark_companion_synced(
        self, immich_id: str, media_key: str, outcome: Outcome, upload_sha1: str
    ) -> bool:
        """Record a Live Photo's original .MOV as backed up as its own item.

        Compare-and-set: only a companion still parked as hidden/paired (or
        already SYNCED to this very item) is updated, so this can never
        clobber a row some other path is processing.
        """
        with self._conn.lock:
            cur = self._conn.execute(
                "UPDATE asset SET state = ?, outcome = ?, media_key = ?, upload_sha1 = ?, synced_at = ?,"
                " ineligible_reason = NULL, error_class = NULL, last_error = NULL, claimed_at = NULL"
                " WHERE immich_id = ? AND ("
                "  (state = ? AND ineligible_reason IN ('hidden', 'live_photo_paired'))"
                "  OR (state = ? AND media_key = ?))",
                (
                    AssetState.SYNCED.value,
                    outcome.value,
                    media_key,
                    upload_sha1,
                    self._clock.now().isoformat(),
                    immich_id,
                    AssetState.INELIGIBLE.value,
                    AssetState.SYNCED.value,
                    media_key,
                ),
            )
            return cur.rowcount > 0

    def mark_companion_paired(self, immich_id: str, reason: str) -> bool:
        """Mark a Live Photo companion as folded into a Motion Photo.

        Never touches a companion that is SYNCED as its own Google item (an
        `original_pair` backup): its mapping is what deletion and dedup key
        on, and must not be erased by a later derived upload.
        """
        with self._conn.lock:
            cur = self._conn.execute(
                "UPDATE asset SET state = ?, ineligible_reason = ?, claimed_at = NULL"
                " WHERE immich_id = ? AND state != ?",
                (AssetState.INELIGIBLE.value, reason, immich_id, AssetState.SYNCED.value),
            )
            return cur.rowcount > 0

    def remove_sync_note(self, immich_id: str, code: str) -> None:
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT sync_notes FROM asset WHERE immich_id = ?", (immich_id,)
            ).fetchone()
            if row is None or not row["sync_notes"]:
                return
            remaining = [c for c in row["sync_notes"].split(",") if c and c != code]
            self._conn.execute(
                "UPDATE asset SET sync_notes = ? WHERE immich_id = ?",
                (",".join(remaining) or None, immich_id),
            )

    def rows_for_media_key(self, media_key: str) -> list[StoredAsset]:
        """Every row, in any state, that maps onto one Google item."""
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT * FROM asset WHERE media_key = ? ORDER BY first_seen_at ASC", (media_key,)
            ).fetchall()
        return [_row_to_stored(r) for r in rows]

    def media_key_uploaded_here(self, media_key: str) -> bool:
        """Whether this service itself created the Google item (an UPLOADED row)."""
        with self._conn.lock:
            row = self._conn.execute(
                "SELECT 1 FROM asset WHERE media_key = ? AND outcome = ? LIMIT 1",
                (media_key, Outcome.UPLOADED.value),
            ).fetchone()
        return row is not None

    def favorite_remote(self, media_key: str) -> sqlite3.Row | None:
        with self._conn.lock:
            return self._conn.execute(
                "SELECT * FROM favorite_remote WHERE media_key = ?", (media_key,)
            ).fetchone()

    def record_favorite_remote(
        self,
        media_key: str,
        *,
        pushed: bool | None,
        managed: bool,
        intent: bool | None,
    ) -> None:
        """Upsert the per-item favorite ledger (see the favorite_remote table)."""
        with self._conn.lock:
            self._conn.execute(
                "INSERT INTO favorite_remote (media_key, pushed, managed, intent, evaluated_at)"
                " VALUES (?,?,?,?,?)"
                " ON CONFLICT(media_key) DO UPDATE SET pushed = excluded.pushed,"
                "  managed = excluded.managed, intent = excluded.intent,"
                "  evaluated_at = excluded.evaluated_at",
                (
                    media_key,
                    None if pushed is None else int(pushed),
                    int(managed),
                    None if intent is None else int(intent),
                    self._clock.now().isoformat(),
                ),
            )

    def touch_favorite_remote(self, media_key: str) -> None:
        """Note that an item was looked at but deliberately left alone (held)."""
        with self._conn.lock:
            self._conn.execute(
                "INSERT INTO favorite_remote (media_key, evaluated_at) VALUES (?, ?)"
                " ON CONFLICT(media_key) DO UPDATE SET evaluated_at = excluded.evaluated_at",
                (media_key, self._clock.now().isoformat()),
            )

    def settle_favorite_row(self, immich_id: str, is_favorite: bool) -> bool:
        """Mark one row's star as reconciled -- only if it has not changed since.

        Compare-and-set on `is_favorite`: a webhook that flipped the star
        while a push for the old value was in flight leaves the row drifted
        for the next sweep instead of being recorded as settled.
        """
        with self._conn.lock:
            cur = self._conn.execute(
                "UPDATE asset SET favorite_synced = ? WHERE immich_id = ? AND is_favorite = ?",
                (int(is_favorite), immich_id, int(is_favorite)),
            )
            return cur.rowcount > 0

    def favorite_candidate_keys(self, limit: int = 2000) -> list[str]:
        """Google items whose star may need a decision this sweep.

        An item is a candidate when the merged intent of its live rows (any
        SYNCED, non-trashed row starred -> starred) differs from the intent
        last settled for it, or -- for an item the ledger has never seen --
        when it is starred or one of its rows carries row-level drift from
        before the ledger existed. Items with no live row at all (everything
        trashed or gone) are never candidates: nothing here ever unstars an
        item because its sources went missing. Least recently evaluated
        first, so held items rotate rather than starve the rest.
        """
        with self._conn.lock:
            rows = self._conn.execute(
                "WITH desired AS ("
                "  SELECT media_key, MAX(is_favorite) AS fav FROM asset"
                "  WHERE media_key IS NOT NULL AND state = ? AND is_trashed = 0"
                "  GROUP BY media_key"
                ")"
                " SELECT d.media_key FROM desired d"
                " LEFT JOIN favorite_remote f ON f.media_key = d.media_key"
                " WHERE (f.media_key IS NULL AND (d.fav = 1 OR EXISTS ("
                "     SELECT 1 FROM asset a WHERE a.media_key = d.media_key"
                "     AND a.favorite_synced IS NOT NULL AND a.favorite_synced != a.is_favorite)))"
                "  OR (f.media_key IS NOT NULL AND (f.intent IS NULL OR f.intent != d.fav))"
                " ORDER BY f.evaluated_at IS NOT NULL, f.evaluated_at ASC LIMIT ?",
                (AssetState.SYNCED.value, limit),
            ).fetchall()
        return [r["media_key"] for r in rows]

    def favorite_drift(self, limit: int = 500) -> list[StoredAsset]:
        """SYNCED rows whose star Google does not know about yet.

        Only two shapes need a push: starred in Immich but never told to
        Google (`is_favorite = 1` with nothing recorded), or told before
        but changed in Immich since (`favorite_synced` disagrees). A row
        that was never starred and never pushed needs nothing: Google's
        default is unstarred, so pushing `False` for it would be a no-op
        API call -- and across a whole historical library, tens of
        thousands of them, enough to trip rate limiting for zero benefit.
        Only rows with a `media_key` can be pushed -- without one there is
        nothing in Google to star.
        """
        with self._conn.lock:
            rows = self._conn.execute(
                "SELECT * FROM asset WHERE state = ? AND media_key IS NOT NULL"
                " AND ("
                "   (is_favorite = 1 AND favorite_synced IS NULL)"
                "   OR (favorite_synced IS NOT NULL AND favorite_synced != is_favorite)"
                " )"
                " ORDER BY first_seen_at ASC LIMIT ?",
                (AssetState.SYNCED.value, limit),
            ).fetchall()
        return [_row_to_stored(r) for r in rows]
