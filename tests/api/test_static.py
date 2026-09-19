"""Static assets must be reachable *without* a session.

`is_open` was an exact-match frozenset lookup, so `/static/app.css` counted
as an authenticated path -- meaning an unauthenticated browser on `/login`
would be redirected for the login page's own stylesheet and render it
unstyled. The allowance is deliberately scoped to the `/static/` prefix:
that directory holds a stylesheet, a script, two fonts and an icon, and no
user data or configuration.
"""

import pytest
from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app
from immich_gphotos.api.auth import is_open


@pytest.fixture
def http(rig_registry):
    return TestClient(create_app(rig_registry), follow_redirects=False)


def test_stylesheet_loads_without_a_session(http):
    response = http.get("/static/app.css")
    assert response.status_code == 200
    assert "text/css" in response.headers["content-type"]


def test_fonts_load_without_a_session(http):
    for name in ("geist-latin.woff2", "geist-mono-latin.woff2"):
        assert http.get(f"/static/fonts/{name}").status_code == 200


def test_the_allowance_is_scoped_to_static_only():
    """Guard against the prefix check being loosened into a general
    `startswith`, which would open far more than intended."""
    assert is_open("/static/app.css") is True
    assert is_open("/static/fonts/geist-latin.woff2") is True
    assert is_open("/staticky") is False
    assert is_open("/static") is False
    assert is_open("/api/settings") is False
    assert is_open("/") is False


def test_authenticated_paths_still_require_a_session(http):
    assert http.get("/api/status").status_code == 401
    assert http.get("/").status_code == 307
