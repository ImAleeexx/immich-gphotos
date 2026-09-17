"""Tests for the dashboard template."""


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
