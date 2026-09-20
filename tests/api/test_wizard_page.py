"""Tests for the rendered /wizard page: the resumable stepper markup, the
ADB shell switcher, and the removal of window.prompt for the one control
that can destroy data. The wizard's API routes (/api/wizard/*) are covered
in test_wizard.py; this file is about what actually reaches the browser.
"""


def test_the_wizard_does_not_offer_the_two_global_settings(http):
    """FINDING I4 / Ruling R16: `worker_threads` and
    `bandwidth_bytes_per_second` are global -- one uplink, one machine,
    shared by every account -- and the wizard configures exactly one
    account. Rendering them here told an admin adding account #3 that they
    were configuring account #3's transfer speed, which was never true; and
    because the page submitted a blank bandwidth field as an explicit null,
    finishing the wizard also cleared whatever cap the container was
    already running under. Both fields belong to /settings, under "All
    accounts" (see tests/api/test_settings_page.py, which pins the floors
    there).

    This replaces the two tests that used to pin those fields' floors in
    this file: the correct floor for a field that must not exist is no
    field.
    """
    body = http.get("/wizard").text
    assert 'name="bandwidth_bytes_per_second"' not in body
    assert 'name="worker_threads"' not in body
    # ...and the user is told where they did go, rather than left to guess.
    assert 'href="/settings"' in body


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
