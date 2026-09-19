"""Filling in the fields gpmc insists on but Google does not need.

`gpmc.api._get_auth_token` builds its auth request by indexing a fixed set of
keys straight out of the parsed `auth_data` -- including `oauth2_foreground`,
which is a cosmetic "was this request made in the foreground" flag. A logcat
line that does not carry it is a perfectly good credential, but gpmc raises
`KeyError('oauth2_foreground')` on it, which classifies as AUTH_INVALID and
sends the user back to their phone to re-extract a credential that was never
the problem.
"""

from urllib.parse import parse_qsl

import pytest

from immich_gphotos.gphotos.client import GpmcClient, missing_auth_field, normalize_auth_data

# A captured line with everything gpmc indexes except oauth2_foreground.
WITHOUT_FOREGROUND = (
    "androidId=3a1f9c2b&app=com.google.android.apps.photos&client_sig=38918a45&callerSig=38918a45"
    "&device_country=us&Email=someone%40gmail.com&google_play_services_version=244433022&lang=en_GB"
    "&sdk_version=34&service=oauth2%3Ahttps%3A%2F%2Fwww.googleapis.com%2Fauth%2Fphotos.native"
    "&Token=aas_et%2FAKppINY-token"
)


def _fields(auth_data: str) -> dict[str, str]:
    return dict(parse_qsl(auth_data, keep_blank_values=True))


def test_a_missing_oauth2_foreground_is_supplied():
    assert "oauth2_foreground" not in _fields(WITHOUT_FOREGROUND)
    assert _fields(normalize_auth_data(WITHOUT_FOREGROUND))["oauth2_foreground"] == "1"


def test_a_credential_that_already_carries_the_flag_is_untouched():
    already = WITHOUT_FOREGROUND + "&oauth2_foreground=0"
    assert normalize_auth_data(already) == already


def test_the_captured_string_is_never_re_encoded():
    """Token and service are already percent-encoded. Round-tripping them
    through urlencode would double-encode the `%` and hand Google a different
    credential than the phone emitted."""
    out = normalize_auth_data(WITHOUT_FOREGROUND)
    assert out.startswith(WITHOUT_FOREGROUND)
    assert "aas_et%2FAKppINY-token" in out
    assert "%252F" not in out


def test_the_users_own_language_and_country_survive():
    assert _fields(normalize_auth_data(WITHOUT_FOREGROUND))["lang"] == "en_GB"


def test_language_and_country_are_supplied_when_absent():
    sparse = "androidId=3a1f9c2b&Email=a%40b.com&Token=aas_et%2Fxyz"
    out = _fields(normalize_auth_data(sparse))
    assert out["lang"] == "en_US"
    assert out["device_country"] == "us"
    assert out["oauth2_foreground"] == "1"


def test_normalizing_twice_adds_nothing_the_second_time():
    once = normalize_auth_data(WITHOUT_FOREGROUND)
    assert normalize_auth_data(once) == once


def test_surrounding_whitespace_is_stripped():
    assert normalize_auth_data("  " + WITHOUT_FOREGROUND + "\n  ").startswith("androidId=")


def test_the_client_hands_gpmc_the_normalized_credential(monkeypatch):
    seen = {}

    def capture(auth_data, **_kwargs):
        seen["auth_data"] = auth_data
        return object()

    monkeypatch.setattr("gpmc.Client", capture)
    assert GpmcClient(auth_data=WITHOUT_FOREGROUND)._client is not None

    assert _fields(seen["auth_data"])["oauth2_foreground"] == "1"


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (KeyError("client_sig"), "client_sig"),
        (KeyError("Token"), "Token"),
        (ValueError("something else entirely"), None),
    ],
)
def test_a_field_gpmc_needs_and_we_cannot_invent_is_named(exc, expected):
    """We can default a foreground flag. We cannot invent a device's signing
    key -- but we can say which field is missing instead of surfacing a bare
    KeyError."""
    assert missing_auth_field(exc) == expected


def test_the_missing_field_reaches_the_user_by_name(monkeypatch):
    def raise_missing(**_kwargs):
        raise KeyError("client_sig")

    monkeypatch.setattr("gpmc.Client", raise_missing)
    client = GpmcClient(auth_data=WITHOUT_FOREGROUND)

    with pytest.raises(Exception) as exc:  # noqa: B017 - GPhotosError, asserted below
        client.exists("deadbeef")

    message = str(exc.value)
    assert "client_sig" in message
    assert "auth_data" in message
