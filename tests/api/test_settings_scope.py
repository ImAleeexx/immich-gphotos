"""Task 5: the settings row is split by scope. `quality`, `albums_enabled`
and `deletions_enabled` are per Google account and live in that account's own
database; `bandwidth_bytes_per_second` and `worker_threads` are global (one
uplink, one machine, shared by the whole process) and live in the control
database instead -- see `storage_keys.GLOBAL_SETTING_KEYS`.
"""

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
    still applies -- this is exactly what keeps `build_services` (the
    single-account entry point with no control database at all) reading
    worker_threads/bandwidth_bytes_per_second back correctly."""
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
