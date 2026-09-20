"""Task 9: the account switcher and the accounts management page.

`two_account_http` (two real accounts, "Alex" and "Mum") and `empty_http`
(zero accounts, R5's starting state) come from tests/api/conftest.py.
"""

import json


def test_the_switcher_lists_every_account_and_marks_the_current_one(two_account_http):
    client, first, second = two_account_http
    body = client.get("/").text
    assert first.label in body and second.label in body
    assert f'value="{second.id}"' in body


def test_the_accounts_page_renders_each_account(two_account_http):
    client, _, _ = two_account_http
    body = client.get("/accounts").text
    assert "Alex" in body and "Mum" in body
    assert "does not delete anything from Google Photos" in body


def test_the_remove_confirmation_copy_is_verbatim(two_account_http):
    """The exact sentence the brief requires, word for word -- not just the
    tail end of it."""
    client, _, _ = two_account_http
    body = client.get("/accounts").text
    assert (
        "Removing an account stops its backups and forgets its queue. "
        "It does not delete anything from Google Photos." in body
    )


def test_every_page_names_the_account_it_is_showing(two_account_http):
    client, _, second = two_account_http
    client.cookies.set("igp_account", second.id)
    assert "Mum" in client.get("/failures").text
    assert "Mum" in client.get("/").text
    assert "Mum" in client.get("/diagnostics").text


def test_accounts_page_reports_immich_host_and_state(two_account_http):
    client, first, _ = two_account_http
    body = client.get("/accounts").text
    # Neither account has been through the wizard yet.
    assert "Setup needed" in body


def test_zero_account_install_renders_a_useful_accounts_page(empty_http):
    """Ruling R5: this is the page a fresh install's browser has been 307ed
    to since Task 4 -- it must render something that gets the admin to "add
    an account", not 404 and not crash on a `None` current_account."""
    response = empty_http.get("/accounts")
    assert response.status_code == 200
    assert "/accounts/add" in response.text
    assert "No accounts yet" in response.text


def test_add_account_page_creates_selects_and_lands_in_the_wizard(empty_http):
    """RULING R13: the page-route add flow -- distinct from the JSON-only
    `POST /api/accounts` -- creates the account, sets the cookie, and 303s
    into the wizard in one request, since a plain HTML form can only issue
    one POST."""
    response = empty_http.post("/accounts/add", data={"label": "Grandma"})
    assert response.status_code == 303
    assert response.headers["location"] == "/wizard"

    accounts = empty_http.get("/api/accounts").json()
    assert len(accounts) == 1
    assert accounts[0]["label"] == "Grandma"
    assert response.cookies["igp_account"] == accounts[0]["id"]


def test_add_account_page_rejects_a_blank_label(empty_http):
    response = empty_http.post("/accounts/add", data={"label": "   "})
    assert response.status_code == 422
    assert empty_http.get("/api/status").status_code == 409  # still zero accounts


def test_api_accounts_still_does_not_set_the_cookie(http):
    """R13's other half: the JSON API create route must stay exactly as
    Task 8 left it -- no cookie -- so this task's new page route is the only
    place selection-on-create happens."""
    response = http.post("/api/accounts", json={"label": "Dad"})
    assert response.status_code == 200
    assert "igp_account" not in response.cookies


def test_settings_page_splits_into_this_account_and_all_accounts(two_account_http):
    client, _, _ = two_account_http
    body = client.get("/settings").text
    assert "This account" in body
    assert "All accounts" in body
    # The note explaining why the second fieldset is shared.
    assert "every account" in body


def test_wizard_prefill_is_scoped_to_the_current_account(two_account_http):
    """Every account's id legitimately appears somewhere on the page (the
    switcher lists all of them), so this pins down the one line that
    matters -- the webhook_url prefill -- rather than searching the whole
    body for `first.id`."""
    client, first, second = two_account_http
    client.cookies.set("igp_account", second.id)
    body = client.get("/wizard").text
    webhook_line = next(line for line in body.splitlines() if "location.origin" in line)
    assert '"/hooks/immich/"' in webhook_line
    assert json.dumps(second.id) in webhook_line
    assert json.dumps(first.id) not in webhook_line
