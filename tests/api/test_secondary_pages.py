"""Failures, diagnostics and login pages get real states: skeletons instead
of the word 'Loading...', composed empty/error states, and a login page that
is a designed panel rather than a bare form.
"""

from fastapi.testclient import TestClient

from immich_gphotos.api.app import create_app


def test_failures_renders_a_skeleton_rather_than_the_word_loading(http):
    body = http.get("/failures").text
    assert "Loading…" not in body
    assert "skeleton" in body


def test_the_first_run_login_page_still_offers_to_set_a_password(rig_services):
    """Pinned by tests/api/test_auth.py; restated here because this task
    rewrites the template that satisfies it."""
    client = TestClient(create_app(rig_services), follow_redirects=False)
    assert "set a password" in client.get("/login").text.lower()


def test_diagnostics_has_an_empty_state_for_each_table(http):
    body = http.get("/diagnostics").text
    assert "No events yet" in body
