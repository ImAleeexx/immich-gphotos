"""Tests for the dashboard template."""

import re


def test_the_dashboard_does_not_render_a_dead_quarantined_counter(http):
    """AssetState has no `quarantined` member, so `counts.quarantined` was
    always undefined and the tile always read 0. Quarantine is reaching
    max_attempts, which writes AssetState.FAILED -- so the tile also
    duplicated Failed."""
    body = http.get("/").text
    assert "quarantined" not in body.lower()
    assert 'id="c-ineligible"' in body


def test_the_dashboard_shows_the_readiness_panel(http):
    assert 'id="readiness"' in http.get("/").text


def test_the_dashboard_shows_a_disconnected_state_when_the_sse_stream_dies(http):
    """A rotated session token 307s /events to /login; EventSource then gets
    text/html back and, per spec, closes the stream for good with no further
    retries. subscribeStatus's onError must be wired up, and it must give the
    hero pill an honest disconnected state rather than leaving it on its last
    frame forever."""
    body = http.get("/").text
    assert re.search(r"igp\.subscribeStatus\(render,\s*function", body), (
        "subscribeStatus must be called with an onError handler"
    )
    assert "EventSource.CLOSED" in body
    assert "Disconnected" in body


def test_poll_readiness_does_not_feed_an_error_body_to_the_renderer(http):
    """A 401 body ({"detail": "unauthenticated"}) has no `overall` and no
    `checks`; feeding it to renderReadiness un-hides the panel and renders
    an empty "Before this is working" list. A non-OK response must be
    treated as an explicit failure before renderReadiness ever sees it."""
    body = http.get("/").text
    match = re.search(r"function pollReadiness\(\) {(.*?)\n  }", body, re.DOTALL)
    assert match, "could not find pollReadiness in the rendered page"
    fn = match.group(1)
    assert "response.ok" in fn, "pollReadiness must check response.ok before parsing the body"
