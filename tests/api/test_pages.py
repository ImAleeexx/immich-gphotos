"""The rendered shell: the things every page must carry."""

import pytest

# `http` comes from tests/api/conftest.py -- see Step 1a. It is shared because
# Tasks 4-7 each need an authenticated client for their own page's tests.


@pytest.mark.parametrize("path", ["/", "/failures", "/settings", "/diagnostics", "/wizard"])
def test_every_page_links_the_stylesheet_and_favicon(http, path):
    body = http.get(path).text
    assert '/static/app.css' in body
    assert '/static/favicon.svg' in body


@pytest.mark.parametrize("path", ["/", "/failures", "/settings", "/diagnostics", "/wizard"])
def test_no_page_carries_an_inline_style_block(http, path):
    """All styling lives in app.css. An inline <style> block is how the old
    shell worked and is what this redesign removes -- it defeats caching and
    puts the design system out of reach of every other page."""
    assert "<style>" not in http.get(path).text


@pytest.mark.parametrize("path", ["/", "/failures", "/settings", "/diagnostics"])
def test_every_page_has_a_skip_link_and_a_main_landmark(http, path):
    body = http.get(path).text
    assert 'href="#main"' in body
    assert '<main id="main"' in body


def test_the_current_page_is_marked_in_the_navigation(http):
    assert 'aria-current="page"' in http.get("/").text
    assert 'aria-current="page"' in http.get("/settings").text


def test_an_unknown_path_renders_the_branded_404_for_a_browser(http):
    """The Accept header is load-bearing: the handler only renders HTML for a
    navigation. TestClient sends `*/*` by default, so it must be set
    explicitly or this asserts the API branch by accident."""
    response = http.get("/no-such-page", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert "text/html" in response.headers["content-type"]
    assert "/static/app.css" in response.text


def test_an_unknown_api_path_still_returns_json(http):
    response = http.get("/api/no-such-thing", headers={"accept": "text/html"})
    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


def test_a_routes_own_404_detail_survives_the_global_handler(http):
    """POST /api/failures/<unknown-id>/retry raises its own HTTPException(404,
    detail=...). The global 404 handler must not clobber that specific
    message with the generic "not found" meant only for unmatched routes."""
    response = http.post("/api/failures/no-such-asset/retry")
    assert response.status_code == 404
    assert response.json() == {"detail": "no quarantined asset with that id"}
