"""Task 8: the account-management routes -- list, create, remove, select.

`http` (a single, unconfigured account named "Default") and `two_account_http`
(two real, on-disk accounts named "Alex" and "Mum", built through
`registry.create`) come from `tests/api/conftest.py`.
"""


def test_listing_accounts_reports_label_and_state(two_account_http):
    client, first, _ = two_account_http
    body = client.get("/api/accounts").json()
    assert [a["label"] for a in body] == ["Alex", "Mum"]
    assert body[0]["id"] == first.id
    assert body[0]["state"] == "setup_needed"


def test_listing_accounts_never_reports_a_credential(two_account_http):
    """GET /api/accounts reports immich_url, never an API key or the Google
    auth blob -- credentials never reach a response body."""
    client, _, _ = two_account_http
    body = client.get("/api/accounts").json()
    for account in body:
        assert set(account) == {"id", "label", "immich_url", "state", "synced", "paused_reason"}


def test_creating_an_account_returns_its_id_and_it_is_listed(http):
    """Ruling R13: `POST /api/accounts` stays JSON-only and does NOT select
    the new account (no cookie is set here) -- that belongs to a page route
    Task 9 adds (`POST /accounts/add`: create, set the cookie, 303 to
    `/wizard`), the same split `/login`/`/logout`/`/accounts/select` already
    follow between page form posts and `/api/*` JSON endpoints. A plain HTML
    form with no JavaScript can only issue one POST, and a JSON 200 carrying
    a cookie has nothing to redirect it, so selection-on-create cannot live
    here."""
    created = http.post("/api/accounts", json={"label": "Dad"})
    assert created.status_code == 200
    assert "id" in created.json()
    assert http.get("/api/accounts").json()[-1]["label"] == "Dad"


def test_creating_an_account_requires_a_label(http):
    assert http.post("/api/accounts", json={"label": "   "}).status_code == 422


def test_creating_an_account_still_requires_a_session(tmp_path):
    """The `/api/accounts` exemption added to `api.app`'s account-required
    gate (so the zero-account bootstrap case below can work) sits *below*
    the session check in that middleware, not instead of it -- it only
    skips the "no account configured" 409, never authentication itself. An
    unauthenticated POST must still 401, whether or not any account exists
    yet, the same as any other /api route."""
    from fastapi.testclient import TestClient

    from immich_gphotos.accounts.registry import AccountRegistry
    from immich_gphotos.api.app import create_app
    from immich_gphotos.api.auth import PASSWORD_KEY, hash_password

    registry = AccountRegistry(tmp_path / "no-session", env={})
    registry.settings.set(PASSWORD_KEY, hash_password("test-password"))
    client = TestClient(create_app(registry), follow_redirects=False)

    assert client.post("/api/accounts", json={"label": "Nope"}).status_code == 401


def test_creating_the_first_account_works_on_a_zero_account_install(empty_http):
    """The account-required gate in `api.app`'s middleware 409s every route
    when zero accounts exist and the path isn't exempted -- `/api/accounts`
    must be exempted, or POST /api/accounts (the only way to create the very
    first account) would be unreachable on exactly the install that needs it
    most."""
    created = empty_http.post("/api/accounts", json={"label": "First"})
    assert created.status_code == 200
    assert empty_http.get("/api/accounts").json()[0]["label"] == "First"


def test_deleting_an_account_requires_the_label_typed_back(two_account_http):
    client, _, second = two_account_http
    assert (
        client.request(
            "DELETE", f"/api/accounts/{second.id}", json={"confirm_label": "wrong", "delete_data": True}
        ).status_code
        == 422
    )
    assert (
        client.request(
            "DELETE", f"/api/accounts/{second.id}", json={"confirm_label": "Mum", "delete_data": True}
        ).status_code
        == 200
    )


def test_selecting_an_account_sets_the_cookie(two_account_http):
    client, _, second = two_account_http
    response = client.post("/accounts/select", data={"account_id": second.id})
    assert response.status_code == 303
    assert response.cookies["igp_account"] == second.id


def test_removing_the_account_the_cookie_names_falls_back_to_the_default(two_account_http):
    """Task 4's `resolve_account` falls back to the first account when the
    cookie names one that no longer exists. This confirms that still holds
    after a *real* removal through the registry, not merely against a
    fabricated stale cookie."""
    client, first, second = two_account_http
    select = client.post("/accounts/select", data={"account_id": second.id})
    assert select.cookies["igp_account"] == second.id

    removed = client.request(
        "DELETE", f"/api/accounts/{second.id}", json={"confirm_label": "Mum", "delete_data": True}
    )
    assert removed.status_code == 200

    # The browser still holds the now-dead cookie in its jar (TestClient
    # persists Set-Cookie responses the same way a real browser does). The
    # dashboard must still render against the remaining (first) account
    # rather than 409ing.
    assert client.cookies.get("igp_account") == second.id
    dashboard = client.get("/")
    assert dashboard.status_code == 200
