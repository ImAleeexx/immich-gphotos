"""Opt-in, READ-ONLY checks against a real Immich server. Never run in CI.

Skipped unless:

    IGP_IMMICH_LIVE=1
    IGP_IMMICH_URL=https://immich.example.com      (no trailing /api)
    IGP_IMMICH_API_KEY=<API key with at least asset.read + asset.download>

Optional:

    IGP_IMMICH_LIVE_PHOTO_STILL_ID=<asset id of an Apple Live Photo still>

Only GET requests and the metadata search POST (a read) are made; nothing
is created, modified or deleted in Immich. Downloads land in pytest's
tmp_path, which pytest removes on its own.
"""

import os

import pytest

from immich_gphotos.immich.client import HttpImmichClient, _to_asset
from immich_gphotos.sync import livephoto

pytestmark = pytest.mark.skipif(
    os.environ.get("IGP_IMMICH_LIVE") != "1"
    or not os.environ.get("IGP_IMMICH_URL")
    or not os.environ.get("IGP_IMMICH_API_KEY"),
    reason="real-Immich read-only test; set IGP_IMMICH_LIVE=1, IGP_IMMICH_URL, IGP_IMMICH_API_KEY",
)


@pytest.fixture
def immich():
    client = HttpImmichClient(os.environ["IGP_IMMICH_URL"], os.environ["IGP_IMMICH_API_KEY"])
    yield client
    client.close()


def test_server_answers_and_search_parses(immich):
    major, _minor, _patch = immich.server_version()
    assert major >= 1
    page = immich.search_assets(updated_after=None, page=1, size=5)
    for asset in page.assets:
        assert asset.immich_id and asset.checksum and asset.filename


@pytest.mark.skipif(
    not os.environ.get("IGP_IMMICH_LIVE_PHOTO_STILL_ID"), reason="set IGP_IMMICH_LIVE_PHOTO_STILL_ID"
)
def test_a_real_live_photo_pair_downloads_and_muxes_in_scratch(immich, tmp_path):
    still_id = os.environ["IGP_IMMICH_LIVE_PHOTO_STILL_ID"]
    still = _to_asset(immich._request("GET", f"/assets/{still_id}").json())
    assert still.live_photo_video_id, "that asset is not a Live Photo still"
    still_path, video_path = tmp_path / "still", tmp_path / "video"
    immich.download_original(still.immich_id, still_path)
    immich.download_original(still.live_photo_video_id, video_path)
    kind = livephoto.still_kind(still_path)
    assert kind in {"jpeg", "heic", "avif"}
    if kind == "jpeg":
        out = tmp_path / "motion.jpg"
        livephoto.write_motion_photo_jpeg(still_path, video_path, out, "video/quicktime")
        video = video_path.read_bytes()
        built = out.read_bytes()
        assert built[:2] == b"\xff\xd8"
        assert built.endswith(video)  # the clip is appended byte-for-byte
        assert len(built) > still_path.stat().st_size + len(video)  # plus the Motion Photo XMP
