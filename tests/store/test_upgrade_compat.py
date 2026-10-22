"""Upgrading a database written by v0.1-beta to this branch (#5/#6/#7).

The file is created from the frozen v0.1-beta SCHEMA (not today's), filled
with rows the old build could have written, then opened by current code.
Nothing on the upgrade path may touch Google on its own.
"""

import sqlite3
from pathlib import Path

from immich_gphotos.clock import FakeClock
from immich_gphotos.config import Settings
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import AssetState
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.deletions import DeletionSweeper
from immich_gphotos.sync.favorites import FavoriteSyncer
from immich_gphotos.sync.livephoto_backup import LivePhotoCompleter

V0_1_SCHEMA = (Path(__file__).with_name("fixtures") / "schema_v0_1_beta.sql").read_text()
NOW = "2026-09-17T10:00:00+00:00"
LIVE_ROWS = 30


def _insert(conn, immich_id, **extra):
    row = {
        "immich_id": immich_id,
        "checksum": f"sum-{immich_id}",
        "filename": f"{immich_id}.jpg",
        "type": "IMAGE",
        "immich_updated_at": NOW,
        "visibility": "timeline",
        "state": "synced",
        "outcome": "uploaded",
        "media_key": f"key-{immich_id}",
        "priority": 1,
        "first_seen_at": NOW,
        "synced_at": NOW,
        **extra,
    }
    conn.execute(
        f"INSERT INTO asset ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", tuple(row.values())
    )


def _old_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript(V0_1_SCHEMA)
    for i in range(LIVE_ROWS):
        _insert(conn, f"live-{i}")
    _insert(conn, "gone", is_trashed=1)
    conn.commit()
    conn.close()
    return path


def _open(path):
    clock = FakeClock()
    return AssetRepo(connect(path), clock), clock


def test_old_database_opens_with_every_new_column_and_table_and_reopens_cleanly(tmp_path):
    path = _old_db(tmp_path / "old.db")
    repo, _ = _open(path)
    assert repo.get("live-0").state is AssetState.SYNCED
    raw = sqlite3.connect(path)
    cols = {r[1] for r in raw.execute("PRAGMA table_info(asset)")}
    tables = {r[0] for r in raw.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {
        "taken_at",
        "is_favorite",
        "favorite_synced",
        "live_photo_video_id",
        "upload_sha1",
        "sync_notes",
    } <= cols
    assert {"favorite_remote", "live_photo"} <= tables
    before = raw.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall()
    raw.close()

    for _ in range(3):
        connect(path).close()
    raw = sqlite3.connect(path)
    assert raw.execute("SELECT sql FROM sqlite_master ORDER BY name").fetchall() == before
    raw.close()


def test_upgrade_makes_no_google_calls_from_favorites_or_live_photos(tmp_path):
    repo, clock = _open(_old_db(tmp_path / "old.db"))
    gphotos = FakeGooglePhotosClient()

    result = FavoriteSyncer(gphotos, repo).sync_once()
    assert gphotos.favorite_calls == []  # never unstars what an old build did not star
    assert result.synced == 0

    resolver = ByteResolver(
        FakeImmichClient(contents={}), scratch=tmp_path / "scratch", direct_roots=[tmp_path]
    )
    sweep = LivePhotoCompleter(repo, gphotos, resolver, clock, "still_only_legacy").sweep()
    assert (sweep.completed, sweep.pending, sweep.failed) == (0, 0, 0)
    assert gphotos.uploads == []
    assert repo.live_photo_summary()["untracked_synced_stills"] == 0


def test_a_legacy_deletion_without_an_upload_hash_fails_closed_unless_google_confirms(tmp_path):
    repo, _ = _open(_old_db(tmp_path / "old.db"))
    settings = Settings(deletions_enabled=True)

    unknown = FakeGooglePhotosClient()
    plan = DeletionSweeper(unknown, repo, settings).plan()
    assert (plan.checksums, plan.asset_ids, plan.unverified_ids) == ([], [], ["gone"])

    confirmed = FakeGooglePhotosClient(present={"sum-gone": "key-gone"})
    sweeper = DeletionSweeper(confirmed, repo, settings)
    plan = sweeper.plan()
    assert (plan.checksums, plan.asset_ids) == (["sum-gone"], ["gone"])
    sweeper.execute(plan)
    assert confirmed.trashed == ["sum-gone"]
    assert repo.get("gone").ineligible_reason == "deleted_from_immich"
    assert repo.get("live-0").state is AssetState.SYNCED
