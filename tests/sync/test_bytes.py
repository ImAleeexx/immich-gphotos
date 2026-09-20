import os
from dataclasses import replace
from datetime import UTC, datetime

from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.immich.protocol import ImmichError
from immich_gphotos.models import Asset
from immich_gphotos.sync.bytes import ByteResolver

ASSET = Asset(
    immich_id="a1",
    checksum="sum-a",
    filename="a.jpg",
    type="IMAGE",
    size_bytes=3,
    immich_updated_at="2026-09-17T10:00:00Z",
    original_path="/nowhere/a.jpg",
    visibility="timeline",
    is_offline=False,
    is_trashed=False,
)


def test_direct_read_is_used_when_the_original_path_exists(tmp_path):
    original = tmp_path / "library" / "a.jpg"
    original.parent.mkdir()
    original.write_bytes(b"ABC")
    immich = FakeImmichClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    resolved = resolver.resolve(replace(ASSET, original_path=str(original)))
    assert resolved.path == original
    assert resolved.temporary is False
    assert immich.downloads == []


def test_falls_back_to_api_download_when_the_path_is_not_readable(tmp_path):
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    resolved = resolver.resolve(ASSET)
    assert resolved.temporary is True
    assert resolved.path.read_bytes() == b"XYZ"
    assert immich.downloads == ["a1"]


def test_direct_read_can_be_disabled(tmp_path):
    original = tmp_path / "a.jpg"
    original.write_bytes(b"ABC")
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch", allow_direct=False)
    resolved = resolver.resolve(replace(ASSET, original_path=str(original)))
    assert resolved.temporary is True
    assert immich.downloads == ["a1"]


def test_release_removes_only_temporary_files(tmp_path):
    original = tmp_path / "a.jpg"
    original.write_bytes(b"ABC")
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")

    direct = resolver.resolve(replace(ASSET, original_path=str(original)))
    resolver.release(direct)
    assert original.exists()

    downloaded = resolver.resolve(ASSET)
    path = downloaded.path
    resolver.release(downloaded)
    assert not path.exists()


def test_release_is_safe_to_call_twice(tmp_path):
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    resolved = resolver.resolve(ASSET)
    resolver.release(resolved)
    resolver.release(resolved)


def test_concurrent_resolutions_of_the_same_asset_use_different_paths(tmp_path):
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    first = resolver.resolve(ASSET)
    second = resolver.resolve(ASSET)
    assert first.path != second.path
    resolver.release(first)
    assert second.path.exists()


def test_filename_with_path_separator_lands_flat_in_scratch(tmp_path):
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    tricky = replace(ASSET, filename="../../etc/a.jpg")
    resolved = resolver.resolve(tricky)
    assert resolved.path.parent == tmp_path / "scratch"


def test_downloaded_file_carries_the_capture_date_as_its_mtime(tmp_path):
    """gpmc sends `int(path.stat().st_mtime)` as the Google Photos capture
    timestamp, and Google uses it for any file whose bytes carry no embedded
    date (WhatsApp videos, screenshots, anything stripped of EXIF). A freshly
    downloaded scratch file's mtime is "now", which is why those assets landed
    in Google Photos dated today instead of when they were taken.
    """
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")
    resolved = resolver.resolve(replace(ASSET, taken_at="2022-07-05T12:00:00.000Z"))

    assert resolved.temporary is True
    assert datetime.fromtimestamp(resolved.path.stat().st_mtime, UTC) == datetime(
        2022, 7, 5, 12, 0, tzinfo=UTC
    )


def test_direct_read_never_touches_the_mtime_of_immichs_own_file(tmp_path):
    """Immich already stores its originals with the capture date as the mtime,
    and the library is mounted read-only; only the scratch copy is ours to
    stamp."""
    original = tmp_path / "a.jpg"
    original.write_bytes(b"ABC")
    os.utime(original, (1_000_000_000, 1_000_000_000))
    immich = FakeImmichClient()
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")

    resolved = resolver.resolve(replace(ASSET, original_path=str(original), taken_at="2022-07-05T12:00:00Z"))

    assert resolved.temporary is False
    assert original.stat().st_mtime == 1_000_000_000


def test_an_unusable_capture_date_leaves_the_download_alone(tmp_path):
    """A missing or malformed date must never fail the upload -- the asset
    still syncs, it just falls back to Google's own dating."""
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")

    for bad in (None, "", "not-a-date"):
        resolved = resolver.resolve(replace(ASSET, taken_at=bad))
        assert resolved.path.read_bytes() == b"XYZ"


def test_a_row_without_a_capture_date_is_looked_up_before_stamping(tmp_path):
    """Rows queued before `taken_at` existed carry NULL, and nothing re-reads
    them before the worker gets there -- a reconcile or backfill pass only
    refreshes a row it happens to walk. Without this lookup the whole backlog
    present at upgrade time would still land in Google Photos dated today.
    """
    immich = FakeImmichClient(contents={"a1": b"XYZ"}, taken_at={"a1": "2022-07-05T12:00:00.000Z"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")

    resolved = resolver.resolve(replace(ASSET, taken_at=None))

    assert immich.taken_at_calls == ["a1"]
    assert datetime.fromtimestamp(resolved.path.stat().st_mtime, UTC) == datetime(
        2022, 7, 5, 12, 0, tzinfo=UTC
    )


def test_a_row_that_already_has_the_date_costs_no_extra_request(tmp_path):
    immich = FakeImmichClient(contents={"a1": b"XYZ"}, taken_at={"a1": "1999-01-01T00:00:00Z"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")

    resolved = resolver.resolve(replace(ASSET, taken_at="2022-07-05T12:00:00.000Z"))

    assert immich.taken_at_calls == []
    assert datetime.fromtimestamp(resolved.path.stat().st_mtime, UTC) == datetime(
        2022, 7, 5, 12, 0, tzinfo=UTC
    )


def test_direct_read_never_pays_for_the_lookup(tmp_path):
    original = tmp_path / "a.jpg"
    original.write_bytes(b"ABC")
    immich = FakeImmichClient(taken_at={"a1": "2022-07-05T12:00:00Z"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")

    resolver.resolve(replace(ASSET, original_path=str(original), taken_at=None))

    assert immich.taken_at_calls == []


def test_a_failing_lookup_still_yields_the_downloaded_bytes(tmp_path):
    """The bytes are already on disk and the upload is the valuable part; a
    metadata call that fails must cost the date, not the backup."""

    def boom(asset_id: str) -> str | None:
        raise ImmichError("immich fell over")

    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    immich.asset_taken_at = boom
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")

    resolved = resolver.resolve(replace(ASSET, taken_at=None))

    assert resolved.path.read_bytes() == b"XYZ"


def test_an_asset_immich_has_no_date_for_is_left_unstamped(tmp_path):
    immich = FakeImmichClient(contents={"a1": b"XYZ"})
    resolver = ByteResolver(immich, scratch=tmp_path / "scratch")

    resolved = resolver.resolve(replace(ASSET, taken_at=None))

    assert immich.taken_at_calls == ["a1"]
    assert resolved.path.read_bytes() == b"XYZ"
