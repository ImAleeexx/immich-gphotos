from pathlib import Path

from immich_gphotos.gphotos.fake import FakeGooglePhotosClient


def test_exists_reports_preloaded_hashes():
    fake = FakeGooglePhotosClient(present={"sum-a": "key-a"})
    assert fake.exists("sum-a") == "key-a"
    assert fake.exists("sum-b") is None


def test_upload_records_and_returns_a_media_key(tmp_path: Path):
    fake = FakeGooglePhotosClient()
    path = tmp_path / "x.jpg"
    path.write_bytes(b"x")
    key = fake.upload(path, checksum="sum-b", filename="orig.jpg")
    assert fake.uploads == [("sum-b", "orig.jpg")]
    assert fake.exists("sum-b") == key


def test_album_calls_are_recorded():
    fake = FakeGooglePhotosClient()
    album = fake.create_album("Holiday", ["k1"])
    fake.add_to_album(album, ["k2", "k3"])
    assert fake.albums[album] == ["k1", "k2", "k3"]


def test_trash_records_checksums():
    fake = FakeGooglePhotosClient(present={"sum-a": "key-a"})
    fake.trash(["sum-a"])
    assert fake.trashed == ["sum-a"]
    assert fake.exists("sum-a") is None
