import pytest

from immich_gphotos.clock import FakeClock
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset, Outcome, Priority
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.albums import GOOGLE_ALBUM_LIMIT, AlbumMirror


def asset(i: str) -> Asset:
    return Asset(
        immich_id=i,
        checksum=f"sum-{i}",
        filename=f"{i}.jpg",
        type="IMAGE",
        size_bytes=1,
        immich_updated_at="2026-09-17T10:00:00Z",
        original_path=None,
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
    )


@pytest.fixture
def rig(tmp_path):
    conn = connect(tmp_path / "t.db")
    clock = FakeClock()
    assets = AssetRepo(conn, clock)
    albums = AlbumRepo(conn)
    immich = FakeImmichClient(albums={"alb-1": ["a", "b"]})
    gphotos = FakeGooglePhotosClient()
    mirror = AlbumMirror(immich, gphotos, albums, assets)
    return mirror, assets, albums, immich, gphotos


def sync(assets: AssetRepo, asset_id: str, key: str) -> None:
    assets.upsert_pending(asset(asset_id), Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_synced(asset_id, key, Outcome.UPLOADED)


def test_creates_the_google_album_and_records_the_mapping(rig):
    mirror, assets, albums, _, gphotos = rig
    sync(assets, "a", "key-a")
    sync(assets, "b", "key-b")

    result = mirror.sync_once()

    assert result.added == 2
    mapping = albums.mapping("alb-1")
    assert mapping is not None
    assert gphotos.albums[mapping.gp_album_id] == ["key-a", "key-b"]


def test_second_run_adds_nothing_new(rig):
    mirror, assets, _, _, gphotos = rig
    sync(assets, "a", "key-a")
    sync(assets, "b", "key-b")
    mirror.sync_once()
    result = mirror.sync_once()
    assert result.added == 0


def test_assets_not_yet_synced_are_skipped_and_retried_later(rig):
    mirror, assets, albums, _, gphotos = rig
    sync(assets, "a", "key-a")  # "b" has no media key yet

    first = mirror.sync_once()
    assert first.added == 1
    assert albums.is_member_added("alb-1", "b") is False

    sync(assets, "b", "key-b")
    second = mirror.sync_once()
    assert second.added == 1
    mapping = albums.mapping("alb-1")
    assert gphotos.albums[mapping.gp_album_id] == ["key-a", "key-b"]


def test_overflow_creates_a_second_google_album(rig):
    mirror, assets, albums, _, gphotos = rig
    sync(assets, "a", "key-a")
    sync(assets, "b", "key-b")
    # Pretend the first album is one item short of Google's hard cap.
    mirror.sync_once()
    mapping = albums.mapping("alb-1")
    albums.bump("alb-1", GOOGLE_ALBUM_LIMIT - mapping.item_count - 1)

    sync(assets, "c", "key-c")
    sync(assets, "d", "key-d")
    rig[3].albums["alb-1"] = ["a", "b", "c", "d"]
    mirror.sync_once()

    overflow = albums.mapping("alb-1#2")
    assert overflow is not None
    assert overflow.name.endswith("(2)")
    assert overflow.overflow_of == "alb-1"


def test_empty_album_is_left_alone(rig):
    mirror, _, albums, immich, gphotos = rig
    immich.albums["alb-empty"] = []
    mirror.sync_once()
    assert albums.mapping("alb-empty") is None
    assert "album-Album alb-empty" not in gphotos.albums
