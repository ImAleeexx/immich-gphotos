"""Opt-in smoke test against a REAL Google Photos account.

Skipped unless all of these are set (never in CI):

    IGP_GOOGLE_SMOKE=1
    IGP_GOOGLE_AUTH_DATA=<gpmc auth string of a throwaway account>
    IGP_GOOGLE_SMOKE_STILL=/path/to/IMG_xxxx.JPG   (a real Live Photo still)
    IGP_GOOGLE_SMOKE_VIDEO=/path/to/IMG_xxxx.MOV   (its paired clip)

It builds a derived JPEG Motion Photo from the pair in a temp dir (the inputs
are only read), uploads it, checks Google reports it by the derived SHA-1 and
NOT by the original still's SHA-1, stars and unstars it, then moves it to
the Google trash and deletes it permanently from there -- in a `finally`,
so also when an assertion fails. If an item with the same bytes already
exists the test skips rather than touch it. Whether
Google *plays* the motion still has to be checked by eye in the app; this
test cannot assert that.
"""

import os
from pathlib import Path

import pytest

from immich_gphotos.sync import livephoto

REQUIRED = ("IGP_GOOGLE_AUTH_DATA", "IGP_GOOGLE_SMOKE_STILL", "IGP_GOOGLE_SMOKE_VIDEO")

pytestmark = pytest.mark.skipif(
    os.environ.get("IGP_GOOGLE_SMOKE") != "1" or not all(os.environ.get(k) for k in REQUIRED),
    reason="real-Google smoke test; set IGP_GOOGLE_SMOKE=1 and " + ", ".join(REQUIRED),
)


def test_derived_motion_photo_round_trips_by_its_own_hash(tmp_path):
    from immich_gphotos.gphotos.client import GpmcClient

    still = Path(os.environ["IGP_GOOGLE_SMOKE_STILL"])
    video = Path(os.environ["IGP_GOOGLE_SMOKE_VIDEO"])
    before = (still.stat().st_mtime_ns, video.stat().st_mtime_ns)
    derived = tmp_path / ("MP_" + still.name)
    derived_sha1 = livephoto.write_motion_photo_jpeg(still, video, derived, "video/quicktime")
    still_sha1 = livephoto.sha1_b64_of_file(still)
    assert derived_sha1 != still_sha1

    client = GpmcClient(os.environ["IGP_GOOGLE_AUTH_DATA"])
    if client.exists(derived_sha1):
        # Never trash something this test did not create.
        pytest.skip("an item with these exact bytes is already in the account; not touching it")
    media_key = client.upload(derived, checksum=derived_sha1, filename=derived.name)
    try:
        assert client.exists(derived_sha1) == media_key
        assert client.exists(still_sha1) != media_key  # the derived item is not the original
        # Star and unstar the item this test created (never any other item).
        client.set_favorite(media_key, True)
        client.set_favorite(media_key, False)
    finally:
        # Cleanup, also when an assertion above failed: trash the one item
        # this test created, then delete it permanently from the trash
        # (gpmc's delete_remote_media_permanently, keyed by the same
        # dedup key move_to_trash uses) so nothing is left in the account.
        client.trash([derived_sha1])
        from gpmc import utils as gpmc_utils

        client._client.api.delete_remote_media_permanently([gpmc_utils.urlsafe_base64(derived_sha1)])
    assert (still.stat().st_mtime_ns, video.stat().st_mtime_ns) == before
