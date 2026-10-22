"""GpmcClient.upload drives gpmc's token/upload/commit steps itself so the
media item is only created for bytes that provably are the declared file.
gpmc's API object is replaced by a recorder -- no network, no credentials."""

import base64
import hashlib
import os

import pytest

from immich_gphotos.gphotos.client import GpmcClient
from immich_gphotos.gphotos.protocol import GPhotosError, SourceChangedDuringUpload
from immich_gphotos.models import ErrorClass


def _b64(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


class RecordingApi:
    def __init__(self, *, remote=None, during_upload=None, fail_commit=None):
        self.calls: list[tuple] = []
        self.remote = remote
        self.during_upload = during_upload
        self.fail_commit = fail_commit
        self.sent = b""

    def find_remote_media_by_hash(self, hash_bytes):
        self.calls.append(("find", hash_bytes))
        return self.remote

    def get_upload_token(self, sha_hash_b64, file_size):
        self.calls.append(("token", sha_hash_b64, file_size))
        return "token-1"

    def upload_file(self, file, upload_token):
        # Like requests streaming a file-like body: read until exhausted.
        first = file.read(2)
        if self.during_upload is not None:
            self.during_upload()
        rest = b"".join(iter(lambda: file.read(1 << 16), b""))
        self.sent = first + rest
        self.calls.append(("upload", upload_token, len(self.sent)))
        return {"decoded": True}

    def commit_upload(self, **kwargs):
        self.calls.append(("commit", kwargs))
        if self.fail_commit:
            raise self.fail_commit
        return "AF1QipCOMMITTED"


def _client(api, quality="original"):
    client = GpmcClient("Email=a%40b.c&Token=x", quality=quality)

    class Gpmc:
        pass

    gpmc = Gpmc()
    gpmc.api = api
    client._local.client = gpmc
    return client


def _photo(tmp_path, data=b"PHOTO-BYTES"):
    path = tmp_path / "p.jpg"
    path.write_bytes(data)
    os.utime(path, (1_600_000_000, 1_600_000_000))
    return path


def test_commits_the_exact_streamed_bytes_with_the_file_mtime(tmp_path):
    path = _photo(tmp_path)
    api = RecordingApi()
    key = _client(api).upload(path, checksum=_b64(b"PHOTO-BYTES"), filename="IMG.jpg")
    assert key == "AF1QipCOMMITTED"
    assert api.sent == b"PHOTO-BYTES"
    commit = [c for c in api.calls if c[0] == "commit"][0][1]
    assert commit["file_name"] == "IMG.jpg"
    assert commit["upload_timestamp"] == 1_600_000_000
    assert commit["sha1_hash"] == hashlib.sha1(b"PHOTO-BYTES").digest()
    assert (commit["model"], commit["quality"]) == ("Pixel XL", "original")


@pytest.mark.parametrize(
    ("quality", "expected"), [("saver", ("Pixel 2", "saver")), ("quota", ("Pixel 8", "original"))]
)
def test_quality_maps_like_gpmc(tmp_path, quality, expected):
    api = RecordingApi()
    _client(api, quality).upload(_photo(tmp_path), checksum=_b64(b"PHOTO-BYTES"), filename="x.jpg")
    commit = [c for c in api.calls if c[0] == "commit"][0][1]
    assert (commit["model"], commit["quality"]) == expected


def test_a_rewrite_during_the_byte_upload_creates_no_item(tmp_path):
    path = _photo(tmp_path)
    api = RecordingApi(during_upload=lambda: path.write_bytes(b"PHOTO-XXXXX"))
    with pytest.raises(SourceChangedDuringUpload) as excinfo:
        _client(api).upload(path, checksum=_b64(b"PHOTO-BYTES"), filename="x.jpg")
    assert excinfo.value.error_class is ErrorClass.ASSET_UNAVAILABLE
    assert [c[0] for c in api.calls] == ["find", "token", "upload"]  # never "commit"


def test_bytes_appended_during_upload_are_not_sent_and_nothing_is_committed(tmp_path):
    path = _photo(tmp_path)

    def append():
        with open(path, "ab") as handle:
            handle.write(b"TAIL")

    api = RecordingApi(during_upload=append)
    with pytest.raises(SourceChangedDuringUpload):
        _client(api).upload(path, checksum=_b64(b"PHOTO-BYTES"), filename="x.jpg")
    assert api.sent == b"PHOTO-BYTES"
    assert "commit" not in [c[0] for c in api.calls]


def test_a_file_that_no_longer_matches_the_declared_hash_is_not_even_uploaded(tmp_path):
    api = RecordingApi()
    with pytest.raises(SourceChangedDuringUpload):
        _client(api).upload(_photo(tmp_path), checksum=_b64(b"SOMETHING ELSE"), filename="x.jpg")
    assert api.calls == []


def test_a_remote_hash_hit_reuses_the_existing_item(tmp_path):
    api = RecordingApi(remote="AF1QipEXISTING")
    key = _client(api).upload(_photo(tmp_path), checksum=_b64(b"PHOTO-BYTES"), filename="x.jpg")
    assert key == "AF1QipEXISTING"
    assert [c[0] for c in api.calls] == ["find"]


def test_gpmc_failures_are_still_classified(tmp_path):
    api = RecordingApi(fail_commit=RuntimeError("429 Too Many Requests"))
    with pytest.raises(GPhotosError) as excinfo:
        _client(api).upload(_photo(tmp_path), checksum=_b64(b"PHOTO-BYTES"), filename="x.jpg")
    assert excinfo.value.error_class is ErrorClass.RATE_LIMITED
