import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset (
  immich_id         TEXT PRIMARY KEY,
  checksum          TEXT NOT NULL,
  filename          TEXT NOT NULL,
  type              TEXT NOT NULL,
  size_bytes        INTEGER,
  immich_updated_at TEXT NOT NULL,
  taken_at          TEXT,
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
  synced_at         TEXT,
  -- What Immich currently says about starring (`Asset.is_favorite`), versus
  -- what has actually been pushed to Google (`favorite_synced`: NULL means
  -- never pushed, 0/1 the last value sent). The sweeper pushes wherever the
  -- two disagree -- see `sync.favorites.FavoriteSyncer`.
  is_favorite       INTEGER NOT NULL DEFAULT 0,
  favorite_synced   INTEGER,
  -- For an Apple Live Photo still, the Immich id of the companion `.MOV`
  -- (`Asset.live_photo_video_id`). The still and its motion are muxed into
  -- one Google Motion Photo before upload -- see `sync.livephoto`.
  live_photo_video_id TEXT,
  -- The base64 SHA-1 of the bytes actually described to Google when this
  -- row synced -- the hash every Google-side operation keys on (dedup
  -- lookup, X-Goog-Hash, trash-by-dedup-key). Immich's own `checksum`
  -- column is that hash only for internal uploads; External Library rows
  -- carry a virtual sha1(path) checksum, so the delete path sends this
  -- column when set and falls back to `checksum` otherwise (for which it
  -- is exact). See sync.bytes and sync.deletions.
  upload_sha1 TEXT,
  -- Non-fatal diagnostics recorded when the row last synced, as a
  -- comma-separated list of stable codes (e.g. `timestamp_unverified`: the
  -- capture date on the uploaded bytes could not be confirmed). NULL means
  -- none. Never a reason to retry -- the bytes are safe -- only a reason not
  -- to claim something was verified when it was not.
  sync_notes TEXT
);
CREATE INDEX IF NOT EXISTS asset_queue ON asset(state, priority, next_attempt_at);
CREATE INDEX IF NOT EXISTS asset_checksum ON asset(checksum);
CREATE INDEX IF NOT EXISTS asset_media_key ON asset(media_key);

-- What this service has told Google about one remote item's star, keyed by
-- Google media key rather than by Immich asset: several Immich assets can
-- dedup onto the same Google item, so starring is reconciled per item, from
-- the merged intent of every row that maps to it (see sync.favorites).
--   pushed     last value a set_favorite call confirmed (NULL: never sent)
--   managed    1 once this service's own star is provably what is on the
--              item (it starred an item it uploaded itself). Only managed
--              items are ever unstarred: an item that was already in Google
--              may carry a star the user set there by hand.
--   intent     the merged target last settled for this item (NULL: never
--              settled, or held because the source set was incomplete)
--   evaluated_at  when the sweeper last looked, so held items rotate
--              instead of starving the rest of the sweep.
CREATE TABLE IF NOT EXISTS favorite_remote (
  media_key    TEXT PRIMARY KEY,
  pushed       INTEGER,
  managed      INTEGER NOT NULL DEFAULT 0,
  intent       INTEGER,
  evaluated_at TEXT
);

-- One row per Apple Live Photo still this service has handled: whether its
-- motion is backed up, under which `live_photo_mode`, and the separate
-- identities involved. The still's own asset row keeps describing whatever
-- was uploaded *for the still* (media_key / upload_sha1); this table keeps
-- the companion and derived identities apart from it:
--   still_sha1          content SHA-1 of the original still's bytes
--   video_sha1 / video_media_key      the original .MOV backed up as its own
--                       item (original_pair)
--   derived_sha1 / derived_media_key  the Motion Photo built in scratch from
--                       both originals (derived_motion) -- never inferred
--                       from the still's hash
--   status              not_configured | pending | complete | failed | deferred
--                       (see sync.livephoto); `complete` only once the motion
--                       is verifiably in Google, never for a still-only upload
--   reason              why it is not complete (companion_missing, rejected:...)
--   attempts / next_attempt_at        retry bookkeeping for `pending`
CREATE TABLE IF NOT EXISTS live_photo (
  still_id          TEXT PRIMARY KEY,
  video_id          TEXT NOT NULL,
  mode              TEXT NOT NULL,
  status            TEXT NOT NULL,
  reason            TEXT,
  still_sha1        TEXT,
  video_sha1        TEXT,
  video_media_key   TEXT,
  derived_sha1      TEXT,
  derived_media_key TEXT,
  attempts          INTEGER NOT NULL DEFAULT 0,
  next_attempt_at   TEXT,
  updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS live_photo_status ON live_photo(mode, status);

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
    ("asset", "taken_at", "ALTER TABLE asset ADD COLUMN taken_at TEXT"),
    ("asset", "is_favorite", "ALTER TABLE asset ADD COLUMN is_favorite INTEGER NOT NULL DEFAULT 0"),
    ("asset", "favorite_synced", "ALTER TABLE asset ADD COLUMN favorite_synced INTEGER"),
    ("asset", "live_photo_video_id", "ALTER TABLE asset ADD COLUMN live_photo_video_id TEXT"),
    ("asset", "upload_sha1", "ALTER TABLE asset ADD COLUMN upload_sha1 TEXT"),
    ("asset", "sync_notes", "ALTER TABLE asset ADD COLUMN sync_notes TEXT"),
)


def apply_migrations(
    conn: sqlite3.Connection, migrations: tuple[tuple[str, str, str], ...] = COLUMN_MIGRATIONS
) -> None:
    """Bring an existing database up to SCHEMA. Safe to run on every open.

    This is the one place in the codebase that runs an execute-then-read pair
    on the shared connection WITHOUT holding `conn.lock`, and that is only
    safe because of when it runs: `connect()` has not returned yet, so no
    other thread can hold a reference to this connection. Every repository
    method must take the lock (a shared `sqlite3.Connection` interleaves
    cursors across threads and corrupts results -- see the carry-forward note
    in the project ledger). A future migration that backfills a column by
    reading data rows must not copy this shape to anywhere the connection is
    already published.

    The table_info check and the ALTER are not atomic together, so two
    processes opening a pre-migration database at the same instant can both
    see the column missing and the loser raises "duplicate column name",
    failing that start. Left as-is deliberately: there is a single
    `connect()` call site and uvicorn runs one process here, the window
    exists only on the first upgrading open, and the next start succeeds
    because the column now exists. Two containers sharing one data volume
    have worse problems than this. Each ALTER is its own atomic transaction
    (`isolation_level=None`), so a death mid-migration leaves no half-state.
    """
    for table, column, ddl in migrations:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(ddl)
