"""Tests for the rendered /wizard page: the resumable stepper markup, the
ADB shell switcher, and the removal of window.prompt for the one control
that can destroy data. The wizard's API routes (/api/wizard/*) are covered
in test_wizard.py; this file is about what actually reaches the browser.
"""


def test_the_wizard_keeps_the_bandwidth_floor_verbatim(http):
    """tests/api/test_settings_live_swap.py pins this exact string, attribute
    order included. Restated here so a wizard rewrite fails loudly and
    locally rather than in an unrelated module."""
    body = http.get("/wizard").text
    assert 'name="bandwidth_bytes_per_second" min="65536"' in body
    assert 'min="0"' not in body
    assert 'min="1"' not in body


def test_the_wizard_renders_a_step_rail(http):
    body = http.get("/wizard").text
    assert 'class="stepper"' in body
    assert body.count('class="step"') >= 4


def test_the_wizard_offers_both_shells_for_the_adb_command(http):
    body = http.get("/wizard").text
    assert "logcat" in body
    assert "FINDSTR" in body


def test_the_wizard_does_not_use_a_browser_prompt(http):
    """window.prompt is the wrong instrument for the one control in this
    application that can destroy data."""
    assert "window.prompt" not in http.get("/wizard").text
