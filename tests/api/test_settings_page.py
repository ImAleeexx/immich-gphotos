"""The settings page is the one screen with a control that can destroy user
data (deletion propagation). These tests guard the two hard constraints on
its markup: the bandwidth floor stays verbatim (see
MIN_BANDWIDTH_BYTES_PER_SECOND in routes.py -- a lower or absent floor can
wedge the background loop for weeks on a single upload), and neither
window.prompt nor window.alert are used any more -- both are replaced by
igp.confirmPhrase and igp.toast (see app.js, from Task 2).
"""


def test_settings_keeps_the_bandwidth_floor_verbatim(http):
    # Scoped to the bandwidth field specifically: worker_threads legitimately
    # renders min="1" (MIN_WORKER_THREADS), so a bare 'min="1"' substring
    # check across the whole page would be a false positive on that field.
    body = http.get("/settings").text
    assert 'name="bandwidth_bytes_per_second" min="65536"' in body
    assert 'name="bandwidth_bytes_per_second" min="0"' not in body
    assert 'name="bandwidth_bytes_per_second" min="1"' not in body


def test_settings_uses_neither_a_browser_prompt_nor_an_alert(http):
    body = http.get("/settings").text
    assert "window.prompt" not in body
    assert "alert(" not in body


def test_settings_groups_fields_into_transfer_and_scope_panels(http):
    body = http.get("/settings").text
    assert body.count('class="panel') == 2
    assert "<h2>Transfer</h2>" in body
    assert "<h2>Scope</h2>" in body


def test_settings_uses_the_shared_confirmation_dialog_for_enabling_deletions(http):
    body = http.get("/settings").text
    assert "igp.confirmPhrase" in body
    assert "ENABLE DELETIONS" in body
    # The comment tying the phrase to the server-side constant must survive
    # the rewrite -- it is the only thing telling a future editor these two
    # strings must never drift apart.
    assert "DELETIONS_ENABLE_PHRASE" in body


def test_settings_reports_a_failed_save_with_an_inline_error_and_a_toast(http):
    body = http.get("/settings").text
    assert "field__error" in body
    assert "igp.toast" in body
    assert 'tone: "danger"' in body


def test_settings_success_copy_is_confident_not_loud(http):
    body = http.get("/settings").text
    assert "Settings saved." in body
    assert "Settings saved!" not in body


def test_deletions_pill_toggles_its_modifier_class_not_just_text(http):
    """updateDeletionsPill previously rewrote only textContent, never the
    pill's modifier class, so #deletions-pill rendered pill--danger (red) for
    both states: a red "Off" (the safe state) and a red "On". The function
    must toggle the class alongside the text."""
    body = http.get("/settings").text
    start = body.index("function updateDeletionsPill")
    end = body.index("\n  }", start)
    fn = body[start:end]
    assert "pill--ok" in fn and "pill--danger" in fn, (
        "updateDeletionsPill must set a different modifier class per state, "
        "not leave the pill on a single hardcoded color"
    )


def test_settings_load_reports_a_failed_fetch_instead_of_a_silently_blank_form(http):
    """load() previously had no error path, unlike failures.html's real error
    state -- a failed GET /api/settings left the form silently blank, and
    `quality` then resolved to "" which 422s on save."""
    body = http.get("/settings").text
    start = body.index("async function load()")
    end = body.index("\n  }", start)
    fn = body[start:end]
    assert "catch" in fn, "load() must have an error path for a failed fetch"
    assert "formError" in fn or "form-error" in fn, (
        "a failed load must surface an inline error, consistent with the rest of the branch"
    )
