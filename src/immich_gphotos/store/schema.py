import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset (
  immich_id         TEXT PRIMARY KEY,
  checksum          TEXT NOT NULL,
  filename          TEXT NOT NULL,
  type              TEXT NOT NULL,
  size_bytes        INTEGER,
  immich_updated_at TEXT NOT NULL,
  original_path     TEXT,
  visibility        TEXT NOT NULL,
  is_offline        INTEGER NOT NULL DEFAULT 0,
  is_trashed        INTEGER NOT NULL DEFAULT 0,
  tags              TEXT NOT NULL DEFAULT '[]',
  state             TEXT NOT NULL,
  outcome           TEXT,
  media_key         TEXT,
  priority          INTEGER NOT NULL,
  attempts          INTEGER NOT NULL DEFAULT 0,
  next_attempt_at   TEXT,
  -- When set, this row's next_attempt_at was computed from the bandwidth cap
  -- (see sync.worker.Worker._throttle_upload), not from a failure backoff, a
  -- halt or the schedule window. That deadline is only meaningful against the
  -- cap it was derived from, so changing the cap releases exactly these rows
  -- and leaves every other kind of deferral alone -- see
  -- AssetRepo.release_bandwidth_deferrals.
  bandwidth_deferred_at TEXT,
  claimed_at        TEXT,
  error_class       TEXT,
  last_error        TEXT,
  ineligible_reason TEXT,
  first_seen_at     TEXT NOT NULL,
  synced_at         TEXT
);
CREATE INDEX IF NOT EXISTS asset_queue ON asset(state, priority, next_attempt_at);
CREATE INDEX IF NOT EXISTS asset_checksum ON asset(checksum);

CREATE TABLE IF NOT EXISTS album_map (
  immich_album_id TEXT PRIMARY KEY,
  gp_album_id     TEXT NOT NULL,
  name            TEXT NOT NULL,
  item_count      INTEGER NOT NULL DEFAULT 0,
  overflow_of     TEXT
);
CREATE TABLE IF NOT EXISTS album_member (
  immich_album_id TEXT NOT NULL,
  immich_asset_id TEXT NOT NULL,
  state           TEXT NOT NULL,
  PRIMARY KEY (immich_album_id, immich_asset_id)
);
CREATE TABLE IF NOT EXISTS cursor (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS event (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       TEXT NOT NULL,
  level    TEXT NOT NULL,
  asset_id TEXT,
  message  TEXT NOT NULL
);
"""


# Columns added to `asset` after the first release. `CREATE TABLE IF NOT
# EXISTS` above leaves an already-created table exactly as it is, so a
# database written by an earlier version never grows a column just because
# SCHEMA gained one: every such column needs an entry here too. Each is
# (table, column, DDL) and is applied only when the column is actually
# missing, so re-opening an up-to-date database is a no-op.
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("asset", "bandwidth_deferred_at", "ALTER TABLE asset ADD COLUMN bandwidth_deferred_at TEXT"),
)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to SCHEMA. Safe to run on every open."""
    for table, column, ddl in COLUMN_MIGRATIONS:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(ddl)
