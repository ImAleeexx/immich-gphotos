import sqlite3
import threading
import time

import pytest

from immich_gphotos.accounts.control import CONTROL_DB_NAME
from immich_gphotos.accounts.migrate import LEGACY_DB_NAME, account_dir
from immich_gphotos.accounts.registry import Account, AccountRegistry
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
    """`LEGACY_WEBHOOK_ACCOUNT_KEY` must not be left in a state
    `/hooks/immich` cannot resolve deterministically: `remove` does not
    touch the key at all, so it keeps naming the id it always named --
    now-dead, but still a real string a caller can look up and decide about.

    What `/hooks/immich` decides is a clean 401, not a fallback. RULING R15
    is explicit that there is no `or registry.default()` there, for a dead
    legacy id or an absent one: a fallback would manufacture a standing
    alias from the bare path to whichever account happens to be first, which
    silently retargets to a different library the moment that account is
    removed. (An earlier version of this docstring claimed the opposite --
    that the route "already falls back to registry.default()". It never did,
    and asserting it here, in the file a reader consults to understand R15,
    was worse than merely wrong. See `api.hooks.receive_legacy`.)"""
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


# --- FINDING I3: the connection `_build` opened, and an rmtree that could
# race the account's own still-running loop thread. ------------------------


def test_remove_closes_the_accounts_database_connection(tmp_path):
    """`_build` opens one sqlite connection per account and nothing ever
    closed it: every removal leaked that connection plus its WAL sidecars
    for the life of the container, and the `rmtree` that follows was
    unlinking files this process still had open."""
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    conn = account.services.conn
    assert conn is not None

    registry.remove(account.id, delete_data=False)

    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_remove_refuses_to_delete_data_under_a_loop_thread_that_would_not_stop(tmp_path):
    """`thread.join(timeout=5.0)` is best effort: `LoopsHandle.run_forever`
    only checks `stop` between iterations, and one iteration can contain a
    multi-minute upload. Deleting the directory out from under that live
    writer does not do what it looks like it does -- on Linux its writes go
    to an unlinked inode and vanish, and `ByteResolver.resolve` mkdirs the
    scratch directory on every call, so the still-running loop *recreates*
    accounts/<id>/scratch right after the admin asked for it to be deleted;
    on macOS/Windows the failed unlink is swallowed by `ignore_errors=True`
    and nothing is deleted, silently. Either way the admin was told the data
    was gone when it was not. So the delete is gated on the thread having
    actually stopped, and a timed-out join returns a warning saying so."""
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")

    class NeverStops:
        """Stands in for a loop thread still inside a long upload."""

        def is_alive(self):
            return True

        def join(self, timeout=None):  # noqa: ANN001 - matches threading.Thread
            return None

    real_thread, account.thread = account.thread, NeverStops()

    warning = registry.remove(account.id, delete_data=True)

    assert warning is not None
    assert "NOT deleted" in warning
    assert str(account_dir(tmp_path, account.id)) in warning
    assert account_dir(tmp_path, account.id).exists()
    # The removal itself still completed: only the deletion was held back.
    assert registry.get(account.id) is None
    assert registry.accounts_repo.get(account.id) is None

    real_thread.join(timeout=5.0)


def test_remove_reports_both_a_workflow_failure_and_an_undeleted_directory(tmp_path):
    """The R14 warning channel is append-only: an undeleted data directory
    must not overwrite a workflow that could not be deleted either."""
    registry = AccountRegistry(tmp_path, env={})
    account = registry.create("Mum")
    account.services.workflow_id = "wf-1"
    account.services.immich = object()  # no delete_workflow -> warns

    class NeverStops:
        def is_alive(self):
            return True

        def join(self, timeout=None):  # noqa: ANN001 - matches threading.Thread
            return None

    real_thread, account.thread = account.thread, NeverStops()

    warning = registry.remove(account.id, delete_data=True)

    assert "wf-1" in warning
    assert "NOT deleted" in warning

    real_thread.join(timeout=5.0)


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


# --- FINDING I2: the registry had no lock, and finding M7: stop_all's
# per-thread timeout. ------------------------------------------------------


def test_create_cannot_interleave_with_a_global_cap_change(tmp_path, monkeypatch):
    """FINDING I2. `create` reads `self._bandwidth`/`self._gate` inside
    `_build` and registers the account only afterwards, while
    `rebuild_shared_limiters` replaces both fields and then walks
    `self.all()`. Interleaved across two request threads -- and uvicorn does
    serve requests on a thread pool -- the account being created is built
    against the old bucket and then missed by the walk that would have fixed
    it, leaving one account metering against a bucket nobody else shares.
    That is the R6 defect, reached by a race rather than by a forgotten
    argument, and it is invisible: every account still has *a* bucket.

    The build is stalled here at exactly the point the race needs, and a
    second thread changes the global cap while it is stalled. With the lock
    that thread waits; without it, it runs the walk before the new account
    exists.
    """
    import immich_gphotos.accounts.registry as registry_module

    registry = AccountRegistry(tmp_path, env={})
    registry.create("Alex")

    building = threading.Event()
    release = threading.Event()
    real_build = registry_module.build_account_services

    def stalled_build(*args, **kwargs):
        building.set()
        release.wait(5.0)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(registry_module, "build_account_services", stalled_build)

    creator = threading.Thread(target=registry.create, args=("Mum",))
    creator.start()
    assert building.wait(5.0)

    changer = threading.Thread(
        target=registry.rebuild_shared_limiters, args=({"bandwidth_bytes_per_second": 1048576},)
    )
    changer.start()
    # Long enough for the walk to have run if nothing were stopping it.
    time.sleep(0.2)
    release.set()
    creator.join(timeout=5.0)
    changer.join(timeout=5.0)

    try:
        buckets = {id(account.services.bandwidth) for account in registry.all()}
        assert len(registry.all()) == 2
        assert buckets == {id(registry._bandwidth)}
        assert registry._bandwidth is not None
    finally:
        registry.stop_all()


def test_stop_all_joins_every_thread_against_one_shared_deadline(tmp_path):
    """FINDING M7. `timeout` is the budget for the whole shutdown, not for
    each thread in turn: N x 5 s is past Docker's default 10 s stop grace
    with three accounts, so `docker stop` would SIGKILL the container
    mid-wind-down -- and the docstring's promise that the loops wind down in
    parallel would be false."""
    registry = AccountRegistry(tmp_path, env={})
    requested = []

    class SlowThread:
        """A thread that takes 0.2 s to join however long it is given."""

        def join(self, timeout=None):  # noqa: ANN001 - matches threading.Thread
            requested.append(timeout)
            time.sleep(0.2)

        def is_alive(self):
            return False

    for account_id in ("acct-1", "acct-2", "acct-3"):
        record = registry.accounts_repo.add(
            account_id=account_id, label=account_id, created_at="2026-09-20T10:00:00Z"
        )
        registry.register(Account(record=record, services=None, loops=None, thread=SlowThread()))

    started = time.monotonic()
    registry.stop_all(timeout=0.5)
    elapsed = time.monotonic() - started

    assert len(requested) == 3
    # Each join gets only what is left of the one deadline, never a fresh 0.5.
    assert requested[0] > requested[1] > requested[2]
    assert requested[2] < 0.2
    # And the whole thing is bounded by the deadline plus the last join's own
    # work, not by 3 x 0.5.
    assert elapsed < 1.0
