from immich_gphotos.accounts.control import CONTROL_DB_NAME
from immich_gphotos.accounts.migrate import LEGACY_DB_NAME, account_dir
from immich_gphotos.accounts.registry import AccountRegistry
from immich_gphotos.config import Settings
from immich_gphotos.storage_keys import SECRET_KEY, SETTINGS_KEY
from immich_gphotos.store.db import connect
from immich_gphotos.store.kv import SettingRepo


def test_a_fresh_data_dir_has_no_accounts(tmp_path):
    registry = AccountRegistry(tmp_path, env={})
    assert registry.all() == []
    assert registry.default() is None
    assert (tmp_path / CONTROL_DB_NAME).exists()


def test_a_v1_install_is_adopted_and_usable(tmp_path):
    SettingRepo(connect(tmp_path / LEGACY_DB_NAME)).set("ui_password", "salt$digest")
    registry = AccountRegistry(tmp_path, env={})
    account = registry.default()
    assert account is not None
    assert registry.settings.get("ui_password") == "salt$digest"
    assert account.services.assets.counts_by_state() == {}
    assert registry.legacy_account_id() == account.id


def test_each_account_generates_its_own_webhook_secret(tmp_path):
    SettingRepo(connect(tmp_path / LEGACY_DB_NAME)).set("ui_password", "x")
    registry = AccountRegistry(tmp_path, env={})
    account = registry.default()
    stored = SettingRepo(connect(account_dir(tmp_path, account.id) / LEGACY_DB_NAME)).get(SECRET_KEY)
    assert account.services.webhook_secret == stored
    assert stored


def test_start_all_runs_one_named_thread_per_account_and_stop_all_joins_them(tmp_path):
    SettingRepo(connect(tmp_path / LEGACY_DB_NAME)).set("ui_password", "x")
    registry = AccountRegistry(tmp_path, env={})
    registry.start_all()
    account = registry.default()
    assert account.thread is not None
    assert account.thread.is_alive()
    assert account.thread.name == f"igp-loop-{account.id}"
    registry.stop_all()
    assert not account.thread.is_alive()


def test_get_returns_none_for_an_unknown_account(tmp_path):
    assert AccountRegistry(tmp_path, env={}).get("nope") is None


# --- Task 6: AccountRegistry._load is the production path that builds the
# shared bandwidth bucket / upload gate and hands the same instances to
# every account. `tests/api/conftest.py`'s `two_account_registry` fixture
# bypasses this entirely (it registers accounts built directly via
# `build_account_services`, with no limiters passed at all), so the
# settings-scope tests built on that fixture only ever prove the limiters
# work *after* a save -- the boot path itself, which is what a real
# container restart actually runs, was unverified before these two tests.


def test_every_account_gets_the_same_shared_bucket_and_gate_at_boot(tmp_path):
    """Two already-registered accounts, with a global cap already on disk,
    loaded through a fresh `AccountRegistry(...)` construction -- the actual
    `__init__`/`_load` path a container restart takes. Both accounts' live
    Workers (not just their `Services` fields) must meter and gate against
    the exact same `TokenBucket`/`Semaphore` instances, not merely equal
    ones."""
    data_dir = tmp_path / "data"
    setup = AccountRegistry(data_dir, env={})
    for account_id in ("acct-1", "acct-2"):
        setup.accounts_repo.add(account_id=account_id, label=account_id, created_at="2026-09-20T10:00:00Z")
    setup.settings.set(SETTINGS_KEY, {"bandwidth_bytes_per_second": 1048576, "worker_threads": 3})

    registry = AccountRegistry(data_dir, env={})  # a fresh boot, as a restart would be

    accounts = registry.all()
    assert len(accounts) == 2
    buckets = {account.services.runtime._worker._bandwidth for account in accounts}
    gates = {account.services.runtime._worker._gate for account in accounts}
    assert len(buckets) == 1
    assert len(gates) == 1
    shared_bucket = next(iter(buckets))
    shared_gate = next(iter(gates))
    assert shared_bucket is not None
    assert shared_gate is not None
    for account in accounts:
        # Not just the live Worker -- the Services fields rebuild_runtime
        # would forward on the next settings save must already agree too.
        assert account.services.bandwidth is shared_bucket
        assert account.services.gate is shared_gate


def test_an_invalid_global_row_at_boot_does_not_crash_and_falls_back_to_defaults(tmp_path):
    """`_build_limiters` validates the control row's global keys the same way
    `accounts.build._merged_settings` validates every other stored value: the
    control row is just as user-writable JSON as the account row, so a
    hand-edited (or pre-migration-shaped) value must not crash the whole
    process at boot. An invalid rate means no cap -- not a `TokenBucket`
    constructed with a value it would itself reject (`TokenBucket.__init__`
    raises `ValueError` for anything <= 0) -- and an out-of-bounds
    `worker_threads` falls back to `Settings().worker_threads` (2), the same
    default `_merged_settings` would use, not the stored, out-of-bounds
    value."""
    data_dir = tmp_path / "data"
    setup = AccountRegistry(data_dir, env={})
    setup.accounts_repo.add(account_id="acct-1", label="acct-1", created_at="2026-09-20T10:00:00Z")
    setup.settings.set(SETTINGS_KEY, {"bandwidth_bytes_per_second": -5, "worker_threads": 999})

    registry = AccountRegistry(data_dir, env={})  # must not raise

    account = registry.default()
    assert account is not None
    assert account.services.runtime._worker._bandwidth is None

    # Count the gate's permits without relying on Semaphore's private
    # internals: drain it non-blocking, then put every permit back.
    gate = account.services.gate
    permits = 0
    while gate.acquire(blocking=False):
        permits += 1
    for _ in range(permits):
        gate.release()
    assert permits == Settings().worker_threads  # falls back to 2, not 999
