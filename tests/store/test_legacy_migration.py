"""Opening databases written by older builds (issue #8 / PR #9).

Every test starts from a database created by a frozen historical schema
(tests/store/fixtures/schema_v0_1_beta.sql, verbatim from tag v0.1-beta),
never from today's SCHEMA, so a migration that only works on fresh
databases cannot pass here.
"""

import sqlite3
from pathlib import Path

import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.models import AssetState
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.deletions import DeletionSweeper

V0_1_SCHEMA = (Path(__file__).with_name("fixtures") / "schema_v0_1_beta.sql").read_text()
NOW = "2026-09-17T10:00:00+00:00"


def _legacy_row(conn: sqlite3.Connection, immich_id: str, state: str, **extra) -> None:
    row = {
        "immich_id": immich_id,
        "checksum": f"sum-{immich_id}",
        "filename": f"{immich_id}.jpg",
        "type": "IMAGE",
        "immich_updated_at": NOW,
        "visibility": "timeline",
        "state": state,
        "priority": 1,
        "first_seen_at": NOW,
        **extra,
    }
    cols = ", ".join(row)
    conn.execute(f"INSERT INTO asset ({cols}) VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def _make_legacy_db(path: Path, *, drop_columns: tuple[str, ...] = ()) -> None:
    schema = V0_1_SCHEMA
    for column in drop_columns:
        # Simulate a database from before that column's ALTER migration.
        schema = "\n".join(line for line in schema.splitlines() if not line.strip().startswith(column + " "))
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    _legacy_row(
        conn,
        "trashed-synced",
        "trashed",
        media_key="key-trashed",
        is_trashed=1,
        outcome="uploaded",
        synced_at=NOW,
    )
    _legacy_row(conn, "trashed-never-uploaded", "trashed", is_trashed=1)
    _legacy_row(conn, "live-synced", "synced", media_key="key-live", outcome="uploaded", synced_at=NOW)
    _legacy_row(conn, "still-pending", "pending")
    conn.commit()
    conn.close()


def _columns(conn, table: str = "asset") -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


class NoCloud:
    """A Google client that fails the test on any call at all."""

    def __getattr__(self, name):
        def refuse(*args, **kwargs):
            raise AssertionError(f"migration path called Google: {name}{args}")

        return refuse


@pytest.mark.parametrize("dropped", [(), ("bandwidth_deferred_at",)], ids=["v0.1-beta", "pre-bandwidth"])
def test_trashed_rows_migrate_and_keep_their_google_identity(tmp_path, dropped):
    path = tmp_path / "legacy.db"
    _make_legacy_db(path, drop_columns=dropped)

    conn = connect(path)
    repo = AssetRepo(conn, FakeClock())

    migrated = repo.get("trashed-synced")
    assert migrated.state is AssetState.INELIGIBLE
    assert migrated.ineligible_reason == "deleted_from_immich"
    # The Google mapping is kept, exactly like mark_deleted keeps it, so a
    # restored asset resolves to the item already in Google.
    assert migrated.media_key == "key-trashed"
    assert migrated.asset.checksum == "sum-trashed-synced"
    assert repo.get("trashed-never-uploaded").state is AssetState.INELIGIBLE
    # Rows in a current state are untouched.
    assert repo.get("live-synced").state is AssetState.SYNCED
    assert repo.get("live-synced").media_key == "key-live"
    assert repo.get("still-pending").state is AssetState.PENDING
    # Every row now parses; nothing is left in the retired state.
    assert "trashed" not in repo.counts_by_state()
    # And the old file gained the columns today's code reads.
    assert {"bandwidth_deferred_at", "taken_at"} <= _columns(conn)


def test_reopening_is_idempotent(tmp_path):
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)
    connect(path).close()

    def snapshot():
        raw = sqlite3.connect(path)
        try:
            rows = raw.execute("SELECT * FROM asset ORDER BY immich_id").fetchall()
            schema = raw.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name").fetchall()
        finally:
            raw.close()
        return rows, schema

    first = snapshot()
    for _ in range(3):
        connect(path).close()
    assert snapshot() == first


def test_a_partially_migrated_database_finishes_migrating(tmp_path):
    """A crash between the column ALTERs and the state UPDATE (or a mix of
    already-migrated and legacy rows) must still converge on the next open."""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path, drop_columns=("bandwidth_deferred_at",))
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE asset ADD COLUMN taken_at TEXT")
    conn.execute(
        "UPDATE asset SET state = 'ineligible', ineligible_reason = 'deleted_from_immich'"
        " WHERE immich_id = 'trashed-never-uploaded'"
    )
    conn.commit()
    conn.close()

    repo = AssetRepo(connect(path), FakeClock())
    for immich_id in ("trashed-synced", "trashed-never-uploaded"):
        stored = repo.get(immich_id)
        assert (stored.state, stored.ineligible_reason) == (AssetState.INELIGIBLE, "deleted_from_immich")


def test_migration_triggers_no_cloud_action(tmp_path):
    """The migrated rows are terminal: the deletion sweeper must not plan a
    Google trash for them (whether they were ever trashed in Google is
    unknown, so doing nothing is the only safe answer), and nothing in the
    open path may talk to Google."""
    path = tmp_path / "legacy.db"
    _make_legacy_db(path)
    repo = AssetRepo(connect(path), FakeClock())

    sweeper = DeletionSweeper(NoCloud(), repo, Settings(deletions_enabled=True))
    plan = sweeper.plan()
    assert not plan.checksums
    assert not plan.asset_ids
    assert not plan.blocked


def test_no_cloud_guard_really_refuses():
    with pytest.raises(AssertionError):
        NoCloud().trash(["x"])


def test_a_database_without_an_asset_table_still_opens(tmp_path):
    """The control database shares connect(); the legacy UPDATE must not
    turn into "no such table: asset" there (that would stop the app from
    starting at all)."""
    from immich_gphotos.accounts.control import CONTROL_SCHEMA

    for _ in range(2):
        connect(tmp_path / "control.db", schema=CONTROL_SCHEMA, migrations=()).close()
