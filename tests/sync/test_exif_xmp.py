import shutil

from immich_gphotos.sync.exif_xmp import ExifToolMotionPhotoXmp, FakeMotionPhotoXmp


def test_fake_records_writes_and_returns_configured_values(tmp_path):
    fake = FakeMotionPhotoXmp(keyframe_us=42, original_xmp="<x/>")
    target = tmp_path / "a.heic"
    target.write_bytes(b"bytes")

    assert fake.keyframe_us(tmp_path / "v.mov") == 42
    assert fake.original_xmp(target) == "<x/>"
    fake.write_xmp(target, b"<packet/>")

    assert fake.writes == [(str(target), b"<packet/>")]
    assert target.read_bytes() == b"bytes"  # the fake never touches the file


def test_if_available_returns_none_without_the_binary(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert ExifToolMotionPhotoXmp.if_available() is None
