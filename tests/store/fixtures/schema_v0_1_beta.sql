-- Verbatim SCHEMA from tag v0.1-beta (src/immich_gphotos/store/schema.py).
-- Frozen on purpose: migration tests must open a database a real old
-- release created, not one built from today's SCHEMA.
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
