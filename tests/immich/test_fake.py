from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.models import Asset


def asset(i: str) -> Asset:
    return Asset(
        immich_id=i, checksum=f"sum-{i}", filename=f"{i}.jpg", type="IMAGE", size_bytes=10,
        immich_updated_at="2026-09-17T10:00:00Z", original_path=f"/u/{i}.jpg",
        visibility="timeline", is_offline=False, is_trashed=False,
    )


def test_fake_pages_assets():
    fake = FakeImmichClient(assets=[asset("a"), asset("b"), asset("c")])
    first = fake.search_assets(updated_after=None, page=1, size=2)
    assert [a.immich_id for a in first.assets] == ["a", "b"]
    assert first.next_page == 2
    second = fake.search_assets(updated_after=None, page=2, size=2)
    assert [a.immich_id for a in second.assets] == ["c"]
    assert second.next_page is None


def test_fake_download_writes_recorded_bytes(tmp_path):
    fake = FakeImmichClient(assets=[asset("a")], contents={"a": b"XYZ"})
    dest = tmp_path / "a.jpg"
    fake.download_original("a", dest)
    assert dest.read_bytes() == b"XYZ"
    assert fake.downloads == ["a"]
