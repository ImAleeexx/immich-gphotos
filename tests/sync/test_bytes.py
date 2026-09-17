from dataclasses import replace

from immich_gphotos.immich.fake import FakeImmichClient
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
