import pytest

from immich_gphotos.accounts.control import CONTROL_DB_NAME
from immich_gphotos.accounts.migrate import LEGACY_DB_NAME, account_dir
from immich_gphotos.accounts.registry import AccountRegistry
from immich_gphotos.config import Settings
from immich_gphotos.immich.protocol import ImmichError
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


# --- Task 8: create/remove -------------------------------------------------


def test_create_makes_a_directory_a_database_and_a_running_thread(tmp_path):
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    registry.start_all()
    assert account.label == "Mum"
    assert (account_dir(tmp_path, account.id) / LEGACY_DB_NAME).is_file()
    assert account.thread.is_alive()
    assert [a.label for a in registry.all()] == ["Mum"]
    registry.stop_all()


def test_remove_stops_the_thread_and_forgets_the_account(tmp_path):
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    registry.start_all()
    thread = account.thread
    registry.remove(account.id, delete_data=False)
    assert not thread.is_alive()
    assert registry.get(account.id) is None
    assert registry.all() == []


def test_remove_keeps_the_data_unless_asked_to_delete_it(tmp_path):
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    registry.remove(account.id, delete_data=False)
    assert account_dir(tmp_path, account.id).exists()

    other = registry.create("Dad")
    registry.remove(other.id, delete_data=True)
    assert not account_dir(tmp_path, other.id).exists()


def test_remove_leaves_a_dead_legacy_webhook_key_solvable_by_task_7(tmp_path):
    """`LEGACY_WEBHOOK_ACCOUNT_KEY` (Task 7's problem, not this task's) must
    not be left in a state Task 7 cannot recover from: `remove` does not
    touch the key at all, so it keeps naming the id it always named --
    now-dead, but still a real string a caller can look up and fall back
    from, exactly the way `/hooks/immich` already falls back to
    `registry.default()` when `registry.get(legacy_id)` is `None`."""
    from immich_gphotos.storage_keys import LEGACY_WEBHOOK_ACCOUNT_KEY

    registry = AccountRegistry(tmp_path, env={})
    legacy = registry.create("Alex")
    registry.settings.set(LEGACY_WEBHOOK_ACCOUNT_KEY, legacy.id)

    registry.remove(legacy.id, delete_data=False)

    assert registry.legacy_account_id() == legacy.id
    assert registry.get(registry.legacy_account_id()) is None


def test_remove_never_calls_google(tmp_path):
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    calls = []
    account.services.gphotos = type("Spy", (), {"__getattr__": lambda s, n: calls.append(n)})()
    registry.remove(account.id, delete_data=True)
    assert calls == []


def test_remove_reports_a_workflow_that_could_not_be_deleted(tmp_path):
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    account.services.workflow_id = "wf-1"

    class Broken:
        def delete_workflow(self, workflow_id):
            raise ImmichError("gone")

    account.services.immich = Broken()
    warning = registry.remove(account.id, delete_data=False)
    assert "wf-1" in warning


def test_remove_deletes_the_workflow_in_immich_when_it_can(tmp_path):
    """The success path alongside `test_remove_reports_a_workflow_that_could
    _not_be_deleted` above: a working client actually gets asked to delete
    the right workflow id, and a clean deletion reports no warning at all."""
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    account.services.workflow_id = "wf-1"
    fake_immich = account.services.immich  # FakeImmichClient: no Immich URL/key stored yet

    warning = registry.remove(account.id, delete_data=False)

    assert warning is None
    assert fake_immich.deleted_workflows == ["wf-1"]


def test_remove_reports_a_missing_workflow_client_rather_than_silently_skipping(tmp_path):
    """`hasattr(immich, "delete_workflow")` guards `remove` against a test
    double or a `None` client that predates the method -- but that guard
    must not silently do nothing when a `workflow_id` was actually recorded:
    a workflow may really still exist in Immich, and the whole point of the
    warning mechanism is that a failure to clean it up is surfaced rather
    than swallowed."""
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    account.services.workflow_id = "wf-1"
    account.services.immich = object()  # no delete_workflow at all

    warning = registry.remove(account.id, delete_data=False)

    assert warning is not None
    assert "wf-1" in warning


# --- Ruling R14: a close-phase failure must not undo, or fail to report,
# a removal that actually completed underneath it. -------------------------


def test_remove_survives_a_runtime_close_failure_and_warns_instead_of_raising(tmp_path):
    """By the time `Runtime.close()` runs, the account is already popped
    from the registry and its thread already stopped -- the removal has, in
    every observable way, already happened. A raise from `close()` must not
    turn that into an exception the caller has to handle as if nothing was
    removed: it is folded into the warning channel, and the removal itself
    -- registry, control database, and (with delete_data=True) the
    directory -- must still go all the way through."""
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")

    class BrokenRuntime:
        def close(self):
            raise RuntimeError("pool wedged")

    account.services.runtime = BrokenRuntime()

    warning = registry.remove(account.id, delete_data=True)

    assert warning is not None
    assert "pool wedged" in warning
    assert registry.get(account.id) is None
    assert registry.accounts_repo.get(account.id) is None
    assert not account_dir(tmp_path, account.id).exists()


def test_remove_survives_an_outgoing_client_close_failure_and_warns_instead_of_raising(tmp_path):
    """Same as above, one step later: `_close_outgoing_immich_client` (via
    the outgoing client's own `close()`) is the second thing that can raise
    in the same best-effort window, and must be handled exactly the same
    way."""
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")

    class BrokenClient:
        def close(self):
            raise RuntimeError("socket already closed")

    account.services.immich = BrokenClient()

    warning = registry.remove(account.id, delete_data=True)

    assert warning is not None
    assert "socket already closed" in warning
    assert registry.get(account.id) is None
    assert registry.accounts_repo.get(account.id) is None
    assert not account_dir(tmp_path, account.id).exists()


def test_remove_reports_both_a_workflow_and_a_close_failure_without_losing_either(tmp_path):
    """When the workflow deletion *and* a close both fail, the admin must
    see both -- one must not silently overwrite the other."""
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    account.services.workflow_id = "wf-1"

    class Broken:
        def delete_workflow(self, workflow_id):
            raise ImmichError("gone")

        def close(self):
            raise RuntimeError("socket already closed")

    account.services.immich = Broken()

    warning = registry.remove(account.id, delete_data=False)

    assert warning is not None
    assert "wf-1" in warning
    assert "socket already closed" in warning


def test_remove_lets_a_control_database_failure_propagate_but_still_deletes_the_directory(tmp_path):
    """`accounts_repo.remove` is the one failure in this method that is NOT
    folded into a warning: if it raises, the control-database row genuinely
    still exists, so the removal genuinely did not complete, and a raised
    exception (a 500 at the route layer) is the honest outcome -- unlike the
    close failures above, this one must not be silently absorbed. Its
    `delete_data` `rmtree` must still run regardless, in its own nested
    `finally`, so a locked control database does not also cancel a directory
    deletion the admin explicitly asked for."""
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")

    def boom(account_id):
        raise RuntimeError("database is locked")

    registry.accounts_repo.remove = boom

    with pytest.raises(RuntimeError, match="database is locked"):
        registry.remove(account.id, delete_data=True)

    assert not account_dir(tmp_path, account.id).exists()


def test_create_gives_a_new_account_the_same_shared_bucket_and_gate_as_boot(tmp_path):
    """Pins the hazard called out in the task brief: `create` is a second
    construction path alongside `_load`, and if it ever stops routing
    through the shared `_bandwidth`/`_gate` instances, a newly created
    account gets a private, uncapped worker instead of sharing the one
    process-wide cap every other account respects. Comparing the *live*
    Worker's limiter, not just `Services.bandwidth`/`gate`, is what would
    have caught a `_build` that forgot to pass `bandwidth=`/`gate=` through
    to `build_account_services` even though the fields on `Services` still
    got set some other way.
    """
    data_dir = tmp_path / "data"
    setup = AccountRegistry(data_dir, env={})
    setup.accounts_repo.add(account_id="acct-boot", label="Boot", created_at="2026-09-20T10:00:00Z")
    setup.settings.set(SETTINGS_KEY, {"bandwidth_bytes_per_second": 1048576, "worker_threads": 3})

    registry = AccountRegistry(data_dir, env={})  # a fresh boot, with acct-boot already on disk
    booted = registry.default()
    assert booted is not None

    created = registry.create("Mum")

    assert created.services.bandwidth is booted.services.bandwidth
    assert created.services.gate is booted.services.gate
    assert created.services.runtime._worker._bandwidth is booted.services.runtime._worker._bandwidth
    assert created.services.runtime._worker._gate is booted.services.runtime._worker._gate
    assert created.services.bandwidth is not None
    assert created.services.gate is not None
    registry.stop_all()
