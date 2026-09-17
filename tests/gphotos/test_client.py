"""Tests for GpmcClient's construction-time error handling.

Live testing against the real Google Photos API found that `self._client` (a
property that constructs and authenticates gpmc's `Client`) was evaluated as
an argument expression *before* `_guard`'s try, so its exceptions (raw
`ValueError`, `KeyError`, ...) escaped the facade entirely. These tests use
only synthetic exceptions and malformed `auth_data` strings — never the real
Google API or real credentials.
"""

from pathlib import Path

import pytest

from immich_gphotos.gphotos.client import GpmcClient
from immich_gphotos.gphotos.protocol import GPhotosError
from immich_gphotos.models import ErrorClass

# Missing every "key=value" field gpmc's auth_data parser looks for, so
# constructing a gpmc `Client` from it fails immediately and locally — no
# network call involved. This is the same shape of failure the live evidence
# showed: `ValueError: No email value in auth_data`.
MALFORMED_AUTH_DATA = "garbage-no-fields"


def _call_exists(client: GpmcClient, tmp_path: Path) -> object:
    return client.exists("checksum")


def _call_upload(client: GpmcClient, tmp_path: Path) -> object:
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"data")
    return client.upload(path, checksum="checksum", filename="photo.jpg")


def _call_create_album(client: GpmcClient, tmp_path: Path) -> object:
    return client.create_album("Album", ["k1"])


def _call_add_to_album(client: GpmcClient, tmp_path: Path) -> object:
    return client.add_to_album("album-id", ["k1"])


def _call_trash(client: GpmcClient, tmp_path: Path) -> object:
    return client.trash(["checksum"])


ALL_PUBLIC_METHODS = pytest.mark.parametrize(
    "call",
    [_call_exists, _call_upload, _call_create_album, _call_add_to_album, _call_trash],
    ids=["exists", "upload", "create_album", "add_to_album", "trash"],
)


@ALL_PUBLIC_METHODS
def test_a_construction_time_failure_surfaces_as_gphotos_error_from_every_method(call, tmp_path):
    """If `self._client` were evaluated outside `_guard`'s try, this would
    raise a raw ValueError instead — pytest.raises(GPhotosError) would then
    fail with the unhandled ValueError propagating out of it."""
    client = GpmcClient(auth_data=MALFORMED_AUTH_DATA)

    with pytest.raises(GPhotosError) as excinfo:
        call(client, tmp_path)

    assert excinfo.value.error_class is ErrorClass.AUTH_INVALID
    assert excinfo.value.error_class.halts_transfer() is True


@ALL_PUBLIC_METHODS
def test_a_construction_time_failure_halts_transfer_from_every_method(call, tmp_path):
    client = GpmcClient(auth_data=MALFORMED_AUTH_DATA)
    with pytest.raises(GPhotosError) as excinfo:
        call(client, tmp_path)
    assert excinfo.value.error_class.halts_transfer() is True


def test_a_connection_error_during_construction_is_transient_not_auth_invalid(monkeypatch):
    """Construction also performs (in principle) a network call to fetch the
    auth token. A passing network blip during that call must not be forced to
    AUTH_INVALID — that would halt the whole service and demand a needless
    re-extraction of a credential from an Android device."""

    def raise_connection_error(*args, **kwargs):
        raise ConnectionError("connection reset by peer")

    monkeypatch.setattr("gpmc.Client", raise_connection_error)

    client = GpmcClient(auth_data="irrelevant, gpmc.Client is patched to fail")

    with pytest.raises(GPhotosError) as excinfo:
        client.exists("checksum")

    assert excinfo.value.error_class is ErrorClass.TRANSIENT
    assert excinfo.value.error_class.halts_transfer() is False
