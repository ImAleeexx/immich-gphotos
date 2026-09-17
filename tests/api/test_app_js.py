"""Tests for /static/app.js -- the shared confirmation dialog and the SSE
status subscription. Nothing in this suite runs a browser, so these are
static checks against the served source, in the same style as
test_css_coverage.py and test_template_scripts.py.
"""

import re


def _confirm_phrase_source(body: str) -> str:
    """Extract the confirmPhrase function body (up to the next top-level
    `function` declaration) so assertions are scoped to it rather than
    matching similar-looking text elsewhere in the file."""
    start = body.index("function confirmPhrase")
    end = body.index("\n  function copyButton", start)
    return body[start:end]


def test_enter_in_the_confirm_dialog_activates_confirm_not_cancel(http):
    """`<form method="dialog">` implicit submission uses the first submit
    button in tree order, which is Cancel (Cancel is visually before Confirm,
    correctly). Confirm being `disabled` does not stop the implicit
    submission from firing Cancel instead. Pressing Enter after typing the
    exact phrase must not be able to fall through to the button order --
    it has to be handled on keydown, checking whether Confirm is enabled."""
    body = http.get("/static/app.js").text
    fn = _confirm_phrase_source(body)

    assert 'addEventListener("keydown"' in fn, (
        "confirmPhrase must handle Enter on the phrase input directly -- "
        "relying on implicit form submission activates Cancel, the first "
        "submit button in tree order"
    )

    keydown_match = re.search(
        r'addEventListener\("keydown",\s*\(?event\)?\s*=>\s*{(.*?)\n\s*}\);',
        fn,
        re.DOTALL,
    )
    assert keydown_match, "could not find the keydown handler body"
    handler = keydown_match.group(1)

    assert '"Enter"' in handler or "'Enter'" in handler
    assert "preventDefault" in handler, (
        "the handler must preventDefault so implicit submission (-> Cancel) "
        "never runs, whether or not Confirm is enabled"
    )
    assert "confirm.disabled" in handler, (
        "the handler must check whether Confirm is enabled before activating it"
    )
    # Confirm must actually get activated when enabled -- not just silenced.
    assert "confirm.click()" in handler or "dialog.close(" in handler


def test_confirm_dialog_buttons_keep_cancel_before_confirm_visually(http):
    """The fix must be solved on keydown, not by reordering the buttons --
    Cancel-then-Confirm is the correct visual order."""
    body = http.get("/static/app.js").text
    cancel_index = body.index('value="cancel"')
    confirm_index = body.index('value="confirm"')
    assert cancel_index < confirm_index


def test_confirm_dialog_has_an_aria_labelledby_pointing_at_its_title(http):
    body = http.get("/static/app.js").text
    fn = _confirm_phrase_source(body)
    labelledby_match = re.search(r'aria-labelledby["\']?,\s*["\']([\w-]+)["\']', fn)
    assert labelledby_match, "confirmPhrase must set aria-labelledby on the dialog"
    title_id = labelledby_match.group(1)
    assert f'id="{title_id}"' in fn, "the referenced id must exist on the title element"
    assert 'class="dialog__title"' in fn


def test_subscribe_status_reports_when_the_stream_dies(http):
    """`/events` is not under `/api`, so a rotated session token 307s it to
    `/login`; EventSource then gets text/html instead of text/event-stream
    and, per spec, fails permanently without retrying. Without an onerror
    hook, the caller (the dashboard) has no way to know the stream is dead."""
    body = http.get("/static/app.js").text
    start = body.index("function subscribeStatus")
    end = body.index("\n  window.igp", start)
    fn = body[start:end]
    assert "onerror" in fn, "subscribeStatus must expose a way for the caller to react to stream failure"
