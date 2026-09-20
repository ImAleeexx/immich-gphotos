"""Task 5: the settings row is split by scope. `quality`, `albums_enabled`
and `deletions_enabled` are per Google account and live in that account's own
database; `bandwidth_bytes_per_second` and `worker_threads` are global (one
uplink, one machine, shared by the whole process) and live in the control
database instead -- see `storage_keys.GLOBAL_SETTING_KEYS`.
"""

from fastapi.testclient import TestClient

from immich_gphotos.storage_keys import SETTINGS_KEY


def test_a_global_key_is_stored_in_the_control_database(http, rig_registry):
    assert http.put("/api/settings", json={"bandwidth_bytes_per_second": 1048576}).status_code == 200
    assert rig_registry.settings.get(SETTINGS_KEY) == {"bandwidth_bytes_per_second": 1048576}
    account = rig_registry.default()
    assert "bandwidth_bytes_per_second" not in (account.services.settings_repo.get(SETTINGS_KEY) or {})


def test_an_account_key_is_stored_in_the_account_database(http, rig_registry):
    assert http.put("/api/settings", json={"quality": "saver"}).status_code == 200
    account = rig_registry.default()
    assert account.services.settings_repo.get(SETTINGS_KEY) == {"quality": "saver"}
    assert rig_registry.settings.get(SETTINGS_KEY) in (None, {})


def test_a_global_setting_survives_a_restart_and_wins_over_an_account_copy():
    """A pre-split database (or a hand-edited account row) may still carry a
    copy of a global key -- the migration deliberately leaves it there (see
    `accounts.migrate.ensure_control_db`). Once a real global row exists, it
    must always win: the account's copy is dead weight, not a second vote."""
    from immich_gphotos.accounts.build import _merged_settings

    settings = _merged_settings(
        "http://immich:2283",
        {"quality": "saver", "bandwidth_bytes_per_second": 99},
        {"bandwidth_bytes_per_second": 1048576},
    )
    assert settings.quality == "saver"
    assert settings.bandwidth_bytes_per_second == 1048576


def test_an_account_copy_of_a_global_key_is_used_as_a_fallback_when_no_global_row_exists():
    """The other half of the precedence rule: on a database that has not
    been through the split (stored_global absent/empty), the account's copy
    still applies -- this is exactly what keeps a single account built with
    no control database at all (`build_account_services` called directly,
    with `global_settings=None`) reading worker_threads/
    bandwidth_bytes_per_second back correctly."""
    from immich_gphotos.accounts.build import _merged_settings

    settings = _merged_settings("http://immich:2283", {"worker_threads": 5}, None)
    assert settings.worker_threads == 5


def test_an_invalid_global_value_is_ignored_key_by_key_not_raised():
    """The control database's copy of the row is just as user-writable as
    the account's, so it gets the same defensive treatment: a bad value for
    one global key must not crash the merge or take down an unrelated key."""
    from immich_gphotos.accounts.build import _merged_settings

    settings = _merged_settings(
        "http://immich:2283",
        {},
        {"worker_threads": 999, "bandwidth_bytes_per_second": 2_000_000},
    )
    assert settings.worker_threads == 2  # dataclass default; 999 is out of bounds
    assert settings.bandwidth_bytes_per_second == 2_000_000


def test_a_patch_touching_both_scopes_writes_and_rebuilds_both(http, rig_registry):
    """A single PUT can carry an account-scoped and a global-scoped key at
    once. Both halves must be written to the right database and the running
    account must end up reflecting both -- not just whichever branch a naive
    if/elif happened to take."""
    response = http.put("/api/settings", json={"quality": "saver", "bandwidth_bytes_per_second": 1048576})
    assert response.status_code == 200
    assert response.json()["quality"] == "saver"
    assert response.json()["bandwidth_bytes_per_second"] == 1048576

    account = rig_registry.default()
    assert account.services.settings.quality == "saver"
    assert account.services.settings.bandwidth_bytes_per_second == 1048576
    assert rig_registry.settings.get(SETTINGS_KEY) == {"bandwidth_bytes_per_second": 1048576}
    assert account.services.settings_repo.get(SETTINGS_KEY) == {"quality": "saver"}


def test_a_patch_touching_both_scopes_rebuilds_the_current_account_exactly_once(
    http, rig_registry, monkeypatch
):
    """Regression guard: routing a combined patch naively (write the account
    half + rebuild_runtime for it, *then* call apply_global_settings, which
    rebuilds every account including this one again) rebuilds the current
    account's graph twice for one request. Count calls to rebuild_runtime
    instead of asserting on a side effect, since a double rebuild is
    otherwise not wrong, just wasteful -- and wasteful is exactly what this
    guards against."""
    import immich_gphotos.api.routes as routes_module

    calls: list[object] = []
    original = routes_module.rebuild_runtime

    def counting_rebuild(services, **kwargs):
        calls.append(services)
        return original(services, **kwargs)

    monkeypatch.setattr(routes_module, "rebuild_runtime", counting_rebuild)

    response = http.put("/api/settings", json={"quality": "saver", "bandwidth_bytes_per_second": 1048576})
    assert response.status_code == 200

    account = rig_registry.default()
    assert calls.count(account.services) == 1


def test_apply_global_settings_rebuilds_every_account(tmp_path):
    from immich_gphotos.accounts.build import build_account_services
    from immich_gphotos.accounts.registry import Account, AccountRegistry

    registry = AccountRegistry(tmp_path / "registry", env={})
    for account_id in ("acct-1", "acct-2"):
        record = registry.accounts_repo.add(
            account_id=account_id, label=account_id, created_at="2026-09-20T10:00:00Z"
        )
        services, loops = build_account_services(tmp_path / account_id, env={})
        registry.register(Account(record=record, services=services, loops=loops))

    registry.apply_global_settings({"worker_threads": 9})

    for account in registry.all():
        assert account.services.settings.worker_threads == 9
        assert account.services.runtime._settings.worker_threads == 9


def test_a_combined_patch_through_the_real_route_reaches_the_other_account_correctly(
    two_account_registry, monkeypatch
):
    """Every other test in this file drives `PUT /api/settings` against a
    single-account registry (`rig_registry`), so the `for account in
    registry.all(): if account.services is not services` branch in
    `put_settings` -- the one piece of this task's routing that only matters
    once a second account exists -- never actually runs anywhere else. This
    is the one test that puts a second, real account behind the route and
    checks all three things that branch has to get right at once:

    1. the *other* account picks up the global half (bandwidth_bytes_per_second);
    2. the *other* account does NOT pick up the account-scoped half (quality) --
       proving the loop passes only `global_updates`, not the whole patch;
    3. every account is rebuilt exactly once for this one request -- proving
       there is no double rebuild of the current account. This assertion is
       the one that actually distinguishes this routing from the brief's
       literal (and double-rebuilding) version: without it, a version that
       rebuilds the current account twice would still pass 1 and 2.
    """
    import immich_gphotos.accounts.registry as registry_module
    import immich_gphotos.api.routes as routes_module
    from immich_gphotos.api.app import create_app
    from immich_gphotos.composition import rebuild_runtime as original

    # `put_settings` calls `rebuild_runtime` directly for the account-scoped
    # path; `apply_global_settings` (in accounts/registry.py) calls its own
    # `from ... import rebuild_runtime` for the global path. Both modules
    # hold their own bound reference to the same underlying function, copied
    # in at import time, so both must be patched to the same counting
    # wrapper -- patching only one would miss any rebuild that goes through
    # the other, exactly the gap that let this regression through review the
    # first time.
    calls: list[object] = []

    def counting_rebuild(services, **kwargs):
        calls.append(services)
        return original(services, **kwargs)

    monkeypatch.setattr(routes_module, "rebuild_runtime", counting_rebuild)
    monkeypatch.setattr(registry_module, "rebuild_runtime", counting_rebuild)

    client = TestClient(create_app(two_account_registry), follow_redirects=False)
    assert client.post("/login", data={"password": "test-password"}).status_code == 303

    current = two_account_registry.default()
    other = two_account_registry.get("acct-2")
    assert current.id == "acct-1"
    assert other is not None and other.services is not current.services

    response = client.put("/api/settings", json={"quality": "saver", "bandwidth_bytes_per_second": 1048576})
    assert response.status_code == 200

    # The account the request was looking at: both halves land.
    assert current.services.settings.quality == "saver"
    assert current.services.settings.bandwidth_bytes_per_second == 1048576

    # The other account: the global half reaches it, the account-scoped half
    # never does -- it keeps its own default, not the current account's value.
    assert other.services.settings.bandwidth_bytes_per_second == 1048576
    assert other.services.settings.quality == "original"

    # Neither account was skipped, and the current account was not rebuilt
    # twice (once for its own account-scoped change, again inside a
    # rebuild-every-account pass for the global change).
    assert calls.count(current.services) == 1
    assert calls.count(other.services) == 1


# --- RULING R6, the deeper hazard: a global cap change must build the new
# shared bucket/gate and assign them onto *every* account's Services before
# any account is rebuilt -- current account included. Get the order wrong
# and the account the request is looking at rebuilds against the *old*
# bucket while every other account gets the new one: the cap then applies
# inconsistently, and it is nearly invisible, since every account still has
# *a* bucket, just not the same one.


def test_a_global_cap_change_gives_every_account_the_same_new_bucket_instance(two_account_registry):
    """The common (global-only patch) path: `put_settings` routes this
    through `AccountRegistry.apply_global_settings`, which must call
    `rebuild_shared_limiters` before its rebuild loop.

    Captures the pre-patch bucket and asserts the post-patch one is a
    *different* instance, not just non-None: `two_account_registry` happens
    to start with `services.bandwidth is None` (it registers accounts built
    directly via `build_account_services`, bypassing `AccountRegistry._load`
    entirely -- see `tests/accounts/test_registry.py` for the boot-path
    coverage that fixture skips), so an `is not None` check alone would keep
    passing even if `rebuild_shared_limiters` were deleted outright, as long
    as this fixture never changes. Asserting against the captured `before`
    value does not have that blind spot."""
    from immich_gphotos.api.app import create_app

    client = TestClient(create_app(two_account_registry), follow_redirects=False)
    assert client.post("/login", data={"password": "test-password"}).status_code == 303

    current = two_account_registry.default()
    other = two_account_registry.get("acct-2")
    assert current.id == "acct-1" and other is not None
    before = current.services.bandwidth

    response = client.put("/api/settings", json={"bandwidth_bytes_per_second": 1048576})
    assert response.status_code == 200

    new_bucket = current.services.runtime._worker._bandwidth
    assert new_bucket is not None
    assert new_bucket is not before
    # Every account's live Worker -- not just its Services field -- meters
    # against the exact same TokenBucket instance.
    assert other.services.runtime._worker._bandwidth is new_bucket
    assert current.services.bandwidth is new_bucket
    assert other.services.bandwidth is new_bucket


def test_a_combined_scope_patch_also_gives_every_account_the_same_new_bucket_instance(
    two_account_registry,
):
    """The branch `apply_global_settings` never runs for: a patch combining
    an account-scoped key (here, `quality`) with a global one is routed by
    `put_settings` itself, which rebuilds the *current* account directly
    rather than through `apply_global_settings` (see
    `test_a_patch_touching_both_scopes_rebuilds_the_current_account_exactly_once`).
    That branch must call `rebuild_shared_limiters` too, and before its own
    rebuild of the current account, not only before the other accounts' --
    otherwise this is exactly the account that would end up metering against
    a stale bucket, since it is the one branch that does not go through
    `apply_global_settings` at all."""
    from immich_gphotos.api.app import create_app

    client = TestClient(create_app(two_account_registry), follow_redirects=False)
    assert client.post("/login", data={"password": "test-password"}).status_code == 303

    current = two_account_registry.default()
    other = two_account_registry.get("acct-2")
    assert current.id == "acct-1" and other is not None
    before = current.services.bandwidth

    response = client.put("/api/settings", json={"quality": "saver", "bandwidth_bytes_per_second": 1048576})
    assert response.status_code == 200

    new_bucket = current.services.runtime._worker._bandwidth
    assert new_bucket is not None
    assert new_bucket is not before
    assert other.services.runtime._worker._bandwidth is new_bucket
    assert current.services.bandwidth is new_bucket
    assert other.services.bandwidth is new_bucket


def test_the_wizard_options_route_routes_a_global_key_exactly_as_put_settings_does(
    two_account_registry,
):
    """RULING R16 / finding C1. `POST /api/wizard/options` takes
    `WizardOptions`, which extends `SettingsPatch` and therefore carries the
    two global keys whatever the wizard page renders. It used to write
    `updates` wholesale into the *account's* settings row and call
    `rebuild_runtime` with whatever limiters that account already held --
    so a bandwidth cap set through the wizard landed in the wrong database,
    was never built into a `TokenBucket`, was never enforced by anyone, and
    was still reported back as applied by `GET /api/settings` (because
    `_merged_settings` falls back to the account copy when no global row
    exists). It did not self-heal on restart either: `_build_limiters`
    reads only the control row.

    Both routes now go through `routes.apply_settings_updates`, so this
    asserts the same four things `PUT /api/settings` is held to: the control
    row holds the key, the account row does not, the registry actually built
    a bucket, and every account's live Worker meters against that one
    instance.
    """
    from immich_gphotos.api.app import create_app

    client = TestClient(create_app(two_account_registry), follow_redirects=False)
    assert client.post("/login", data={"password": "test-password"}).status_code == 303

    current = two_account_registry.default()
    other = two_account_registry.get("acct-2")
    assert current.id == "acct-1" and other is not None

    response = client.post("/api/wizard/options", json={"bandwidth_bytes_per_second": 1048576})
    assert response.status_code == 200

    assert two_account_registry.settings.get(SETTINGS_KEY) == {"bandwidth_bytes_per_second": 1048576}
    assert "bandwidth_bytes_per_second" not in (current.services.settings_repo.get(SETTINGS_KEY) or {})

    bucket = two_account_registry._bandwidth
    assert bucket is not None
    assert current.services.runtime._worker._bandwidth is bucket
    assert other.services.runtime._worker._bandwidth is bucket


def test_the_wizard_options_route_still_writes_an_account_key_to_the_account(two_account_registry):
    """The other half of R16's split, through the same route: an
    account-scoped key must stay in this account's own database and must not
    reach the control database or any other account."""
    from immich_gphotos.api.app import create_app

    client = TestClient(create_app(two_account_registry), follow_redirects=False)
    assert client.post("/login", data={"password": "test-password"}).status_code == 303

    current = two_account_registry.default()
    other = two_account_registry.get("acct-2")

    assert client.post("/api/wizard/options", json={"quality": "saver"}).status_code == 200

    assert current.services.settings_repo.get(SETTINGS_KEY) == {"quality": "saver"}
    assert two_account_registry.settings.get(SETTINGS_KEY) in (None, {})
    assert current.services.settings.quality == "saver"
    assert other.services.settings.quality == "original"
