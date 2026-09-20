"""The registry: owns the control database, every account's runtime graph,
and the one background-loop thread each account runs while the process is up.

`main.py` builds exactly one of these at boot. Everything that used to be a
single global (`Services`, the loop thread, the `Redactor`) now lives per
account inside it, except the `Redactor`, which stays one instance shared by
every account -- see the class docstring below for why.
"""

import logging
import os
import shutil
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from immich_gphotos.accounts.build import _validated, build_account_services
from immich_gphotos.accounts.control import (
    CONTROL_DB_NAME,
    AccountRecord,
    AccountRepo,
    connect_control,
    new_account_id,
)
from immich_gphotos.accounts.migrate import account_dir, ensure_control_db
from immich_gphotos.clock import Clock, SystemClock
from immich_gphotos.composition import _close_outgoing_immich_client, rebuild_runtime
from immich_gphotos.config import Settings
from immich_gphotos.logging import Redactor, configure_logging
from immich_gphotos.services import Services
from immich_gphotos.storage_keys import LEGACY_WEBHOOK_ACCOUNT_KEY, SETTINGS_KEY
from immich_gphotos.store.kv import SettingRepo
from immich_gphotos.sync.loops import LoopsHandle
from immich_gphotos.sync.throttle import TokenBucket

logger = logging.getLogger(__name__)


@dataclass
class Account:
    record: AccountRecord
    services: Services
    loops: LoopsHandle
    thread: threading.Thread | None = None
    stop: threading.Event = field(default_factory=threading.Event)

    @property
    def id(self) -> str:
        return self.record.id

    @property
    def label(self) -> str:
        return self.record.label


class AccountRegistry:
    """Owns the control database, every account's graph, and their threads.

    One `Redactor` is shared by every account and by the log handler, so a
    credential belonging to any account is scrubbed out of the one log
    stream they all write to. Constructing a fresh `Redactor` per account
    would mean account B's log lines never got account A's secrets scrubbed
    from them (or vice versa), even though both land in the same stream --
    `configure_logging` is called exactly once, here, with the one instance
    every account's `build_account_services` call then grows via
    `add_secret` rather than replacing.
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        clock: Clock | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._env = env if env is not None else os.environ
        self._clock = clock if clock is not None else SystemClock()
        self._redactor = Redactor([])
        configure_logging(self._env.get("IGP_LOG_LEVEL", "INFO"), redactor=self._redactor)

        self._data_dir.mkdir(parents=True, exist_ok=True)
        # Idempotent and cheap once control.db exists; adopts a v1 install on
        # the first boot after an upgrade, otherwise a no-op.
        ensure_control_db(self._data_dir, now=self._clock.now().isoformat())
        self._conn = connect_control(self._data_dir / CONTROL_DB_NAME)
        self.accounts_repo = AccountRepo(self._conn)
        self.settings = SettingRepo(self._conn)
        self._accounts: dict[str, Account] = {}
        # The process-wide shared limiters (Task 6) -- one TokenBucket and
        # one upload-slot Semaphore for every account's Worker, built once
        # here from the global settings row rather than once per account
        # inside `build_runtime_graph`. See `_build_limiters` and
        # `rebuild_shared_limiters` for how they are (re)built and kept in
        # sync with a changed global setting.
        self._bandwidth: TokenBucket | None = None
        self._gate: threading.Semaphore | None = None
        self._load()

    def _build_limiters(self, stored_global: object) -> tuple[TokenBucket | None, threading.Semaphore]:
        """Build the one bandwidth bucket and one upload gate for the whole
        process, from the control database's copy of the global settings row.

        Validated the same way `accounts.build._merged_settings` validates
        every other stored value (`_validated`): the control row is just as
        user-writable JSON as the account row, so a hand-edited or
        pre-migration-shaped value here must be ignored rather than raised or
        passed straight to `TokenBucket`/`Semaphore`, which would crash the
        whole process on the very first account build. An invalid or absent
        `worker_threads` falls back to `Settings().worker_threads` (the same
        default `_merged_settings` would use), not `MIN_WORKER_THREADS`,
        because that default is the pool size every account already runs at
        when nothing overrides it -- a smaller gate than that would throttle
        concurrency below what a plain, unconfigured install already permits.
        """
        stored = stored_global if isinstance(stored_global, dict) else {}
        rate = _validated("bandwidth_bytes_per_second", stored.get("bandwidth_bytes_per_second"))
        bandwidth = TokenBucket(rate, self._clock) if rate is not None else None
        threads = _validated("worker_threads", stored.get("worker_threads"))
        gate = threading.Semaphore(threads if threads is not None else Settings().worker_threads)
        return bandwidth, gate

    def _build(self, record: AccountRecord) -> tuple[Services, LoopsHandle]:
        """Build one account's runtime graph -- the single call site shared by
        `_load` (every account already on disk at boot) and `create` (a new
        one added while the process is running).

        This is where Task 6's hazard actually gets closed for a *second*
        construction path: `create` used to be free to build an account's
        graph by calling `build_account_services` directly, and it would have
        been easy to do so without `bandwidth=self._bandwidth, gate=self._gate`
        and the current global settings row -- exactly the private,
        uncapped-worker defect Task 6 exists to prevent, just reachable
        through "add an account" instead of a container restart. Routing both
        paths through this one method means there is only one place that can
        forget to pass them, and both callers automatically pick up a fix
        made here.

        `global_settings` is re-read from `self.settings` on every call
        (rather than cached from `_load`) so a `create` long after boot still
        sees whatever the control row currently holds -- it, not a snapshot
        from startup, is what `_merged_settings` must merge the new account's
        row against.
        """
        return build_account_services(
            account_dir(self._data_dir, record.id),
            clock=self._clock,
            redactor=self._redactor,
            env=self._env,
            global_settings=self.settings.get(SETTINGS_KEY),
            bandwidth=self._bandwidth,
            gate=self._gate,
        )

    def _load(self) -> None:
        # Read once and passed to `_build_limiters`: the global half of the
        # settings row lives in this registry's own control database, not in
        # any one account's, so the shared bucket/gate are built from the
        # same copy of it every account's own `_build` call will also read.
        global_settings = self.settings.get(SETTINGS_KEY)
        self._bandwidth, self._gate = self._build_limiters(global_settings)
        for record in self.accounts_repo.list():
            services, loops = self._build(record)
            self.register(Account(record=record, services=services, loops=loops))

    def register(self, account: Account) -> None:
        """Add an already-built `Account` to the registry.

        Split out from `_load` so Task 8's "add account" flow can build one
        account's graph and register it without reloading every account
        already running.
        """
        self._accounts[account.id] = account

    def all(self) -> list[Account]:
        return [self._accounts[r.id] for r in self.accounts_repo.list() if r.id in self._accounts]

    def get(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def default(self) -> Account | None:
        accounts = self.all()
        return accounts[0] if accounts else None

    def legacy_account_id(self) -> str | None:
        value = self.settings.get(LEGACY_WEBHOOK_ACCOUNT_KEY)
        return str(value) if value else None

    def start_all(self) -> None:
        """Start one named, daemon thread per account that doesn't already
        have one running. Safe to call repeatedly: an account already
        running is left alone, which is what lets Task 8 call this again
        after adding an account and have it start only that one.

        The `is not None` guard actually means "ever had a thread," not
        "currently running": `stop_all` deliberately leaves a finished
        thread's `Account.thread` set rather than resetting it to `None`
        (Ruling R9 -- `stop_all` is shutdown-only, restart-in-place is
        unsupported), so this method will not restart an account that was
        stopped. Re-nulling `account.thread` in `stop_all` so this guard
        would restart it is exactly what R9 fixed; don't reintroduce it.
        """
        for account in self.all():
            if account.thread is not None:
                continue
            self._start(account)

    @staticmethod
    def _start(account: Account) -> None:
        """Start one account's named, daemon background-loop thread.

        Split out of `start_all` so `create` can start exactly the one
        account it just added without also touching every other account
        already in the registry (see `create`'s docstring for why calling
        `start_all` there instead is not the same thing).
        """
        account.thread = threading.Thread(
            target=account.loops.run_forever,
            args=(account.stop,),
            name=f"igp-loop-{account.id}",
            daemon=True,
        )
        account.thread.start()

    def stop_all(self, timeout: float = 5.0) -> None:
        """Signal every account's loop to stop, then join them.

        The stop events are all set in one pass before any join, so N
        accounts' loops wind down in parallel -- each sees its own event and
        finishes its current iteration while the others are doing the same
        -- rather than this method blocking on account 1's `timeout` before
        even telling account 2 to stop.

        `account.thread` is left set to the now-finished `Thread` rather than
        reset to `None`: callers (and tests) that already hold a reference to
        the `Account` -- `registry.default()` returns the same object stored
        here, not a copy -- can still ask it `is_alive()` afterwards.
        """
        for account in self.all():
            account.stop.set()
        for account in self.all():
            if account.thread is not None:
                account.thread.join(timeout=timeout)

    def rebuild_shared_limiters(self, updates: dict) -> None:
        """Rebuild whichever shared limiter(s) `updates` actually touches, and
        assign the result onto *every* registered account's `Services` --
        before anything calls `rebuild_runtime` for any of them.

        This is the fix for the hazard Task 6 exists to prevent, one level
        deeper than sharing the bucket/gate in the first place: when a global
        cap changes, a *new* `TokenBucket` (or `Semaphore`, for a changed
        `worker_threads`) has to be built -- the old one's rate/size is
        wrong now, and neither type supports resizing in place. But
        `rebuild_runtime` reads the limiters to hand to `build_runtime_graph`
        off `services.bandwidth`/`services.gate` -- it does not ask this
        registry for the latest ones itself (see Ruling R6 in
        `composition.rebuild_runtime`). So if the new instances were built
        here but only *some* accounts' `Services` got them reassigned before
        their own `rebuild_runtime` call ran -- in particular, if the account
        the request that changed the setting happens to be looking at were
        rebuilt first, or via a different code path (see
        `api.routes.put_settings`'s combined-scope branch, which rebuilds the
        current account itself rather than through `apply_global_settings`)
        -- that account would rebuild against the *old* bucket while every
        other account picked up the new one. The cap would then apply
        inconsistently, and nothing about that failure is visible: every
        account still has *a* bucket, just not the same one.

        Hence the two-phase order this method enforces: build the new
        limiter(s) first, assign them onto every account's `Services` next,
        and only then may any caller rebuild anyone's runtime graph --
        `apply_global_settings` below does exactly that for the common case;
        `put_settings`'s combined-scope branch calls this method directly for
        the same reason before it does its own two-part rebuild.

        Rebuilds only the limiter(s) whose underlying key is actually present
        in `updates` -- replacing the other for no reason would reset its
        in-flight state (an accruing `TokenBucket` balance, or threads
        already queued on the old `Semaphore`) on a save that never touched
        it.
        """
        if "bandwidth_bytes_per_second" in updates:
            rate = _validated("bandwidth_bytes_per_second", updates.get("bandwidth_bytes_per_second"))
            self._bandwidth = TokenBucket(rate, self._clock) if rate is not None else None
        if "worker_threads" in updates:
            threads = _validated("worker_threads", updates.get("worker_threads"))
            self._gate = threading.Semaphore(threads if threads is not None else Settings().worker_threads)
        for account in self.all():
            account.services.bandwidth = self._bandwidth
            account.services.gate = self._gate

    def apply_global_settings(self, updates: dict) -> None:
        """Rebuild every account's graph against a changed global setting.

        `bandwidth_bytes_per_second` and `worker_threads` are global because
        the resource they govern (one uplink, one machine) is shared by the
        whole process, not owned by one account -- so a change to either one
        has to reach every account's runtime graph, not just the account the
        request that changed it happened to be looking at.

        `rebuild_shared_limiters` runs first, and unconditionally: it is what
        keeps every account's `Services.bandwidth`/`Services.gate` pointed at
        the same, current instances before any of the rebuilds below can read
        them (see that method's docstring for the hazard this order closes).

        `rebuild_runtime` already carries each account's halt and pause state
        across its own swap (see its docstring), so this cannot clear
        someone's AUTH_INVALID, and each account's bandwidth deferrals are
        released inside its own rebuild -- both exactly as they would be for
        a single account's own settings save, just repeated once per account
        here.
        """
        self.rebuild_shared_limiters(updates)
        for account in self.all():
            rebuild_runtime(account.services, settings=replace(account.services.settings, **updates))

    def create(self, label: str) -> Account:
        """Add a new account: a control-database row, its own on-disk
        directory and database, and a running background-loop thread -- the
        admin's "add account" flow (Task 8).

        Goes through `_build`, the exact same construction `_load` uses for
        every account already on disk at boot, so the new account shares the
        one process-wide bandwidth bucket and upload gate with every other
        account rather than getting a private, uncapped pair of its own (see
        `_build`'s docstring for the hazard this closes).

        Starts only this account's own thread via `_start`, rather than
        calling `start_all()` -- which would also try to start any *other*
        registered account whose `thread` happens to still be `None`. In a
        real boot that never happens (`main.py` calls `start_all()` once
        right after building the registry, before this method can ever run),
        but a handful of tests build a registry with a hand-registered stub
        account that is deliberately never started (`loops=None`, no
        background thread wanted) -- `start_all()` would crash on
        `account.loops.run_forever` for that account the moment any test
        calls `create()` against such a registry. Starting only the account
        this call just built sidesteps that entirely and is exactly as
        correct in production, where the distinction never has an observable
        effect (see Ruling R9's "skip if already started" guard, which
        `start_all` still relies on for the boot-time case).
        """
        record = self.accounts_repo.add(
            account_id=new_account_id(), label=label, created_at=self._clock.now().isoformat()
        )
        services, loops = self._build(record)
        account = Account(record=record, services=services, loops=loops)
        self.register(account)
        self._start(account)
        return account

    def remove(self, account_id: str, *, delete_data: bool) -> str | None:
        """Stop an account and forget it. Returns a warning to surface to the
        admin, or `None` when nothing went wrong.

        Order matters here, but not for the reason it might look like at
        first glance: dropping the control-database row before deleting the
        directory does NOT protect against `_load` being unable to recover
        from a crash between the two. `store.db.connect` (called from
        `_build`) does `path.parent.mkdir(parents=True, exist_ok=True)`
        before ever opening the database file, so a boot that finds a row
        with no directory behind it does not crash at all -- it silently
        recreates an empty directory and an empty database and the account
        just comes back looking freshly created, data gone. (`create`
        produces the mirror image of this exact state on every call, on
        purpose: it commits the `accounts_repo.add` row before `_build` ever
        creates the directory, so a `_build` failure there leaves precisely
        a row with no directory yet -- recovered by `_load` the same way.
        The two are not in tension; they are the same state reached from
        opposite directions.)

        The real reason to drop the row before the directory, then, is not
        crash-safety -- both orders are equally recoverable -- it is which
        leftover is less confusing to whoever finds it later. A crash after
        the row is gone leaves orphaned bytes on disk that nothing ever
        looks at again: inert clutter, cleaned up by hand whenever someone
        notices. A crash after the directory is gone but before the row is
        would instead resurrect an empty, freshly-"created" account at the
        next boot, with no data and no error -- confusing for an admin who
        watched it get removed. Between the two, "some orphaned bytes on
        disk" is the strictly less surprising thing to leave behind, so the
        row goes first.

        RULING R14 -- the two failure modes past this point are not treated
        the same, on purpose. By the time `Runtime.close()`/the outgoing
        client close run, the account is already popped from `self._accounts`
        and its thread already stopped: the removal has, for all practical
        purposes, already happened. A raise from either close is therefore
        folded into the same `warning` string the failed-workflow-deletion
        branch above already uses (appended to it, not overwriting it, if
        both happened) rather than propagated -- an admin who asked to
        remove an account and got a 500 for it would either retry a delete
        that is now a 404, or conclude the account still exists, and neither
        would be true. `accounts_repo.remove`, immediately after, is
        different: if *that* raises, the control-database row genuinely
        still exists, so the removal genuinely did not complete, and letting
        it propagate is the honest outcome. Its own `delete_data` `rmtree`
        still runs regardless, in a `finally` nested one level deeper, so a
        raise there does not silently cancel a directory deletion the admin
        explicitly asked for.

        Popping the account out of `self._accounts` up front (rather than
        only once the database row is dropped) is a separate, narrower
        thing: it stops `get`/`all`/`resolve_account` from finding this
        account *the moment removal starts*, so a request that arrives
        mid-teardown falls back to the default account instead of touching
        a runtime that is in the middle of being torn down.

        Deleting the account's workflow inside Immich is best effort: a dead
        API key or an unreachable server must not block the removal (the
        admin asked to remove an account specifically *because* something
        about it is broken, often), but a workflow left behind keeps POSTing
        into what is now a 401 forever in that person's Immich instance --
        invisible to us but not to them -- so the failure is reported back
        rather than silently swallowed. Nothing here ever calls the Google
        client: Google Photos deletion is explicitly out of scope forever
        (see the task brief) -- there is no "best effort" version of that,
        because there is no version of that at all.
        """
        account = self._accounts.pop(account_id, None)
        if account is None:
            return None

        warning = None
        workflow_id = account.services.workflow_id
        immich = account.services.immich
        if workflow_id:
            if immich is not None and hasattr(immich, "delete_workflow"):
                try:
                    immich.delete_workflow(workflow_id)
                except Exception as exc:  # noqa: BLE001 - best effort by design, see docstring
                    warning = (
                        f"Could not delete workflow {workflow_id} in Immich ({exc}). "
                        "Remove it there by hand, or it will keep firing into a rejected webhook."
                    )
            else:
                # `ImmichClient` mandates `delete_workflow` now, so a real
                # client (fake or HTTP) always has it -- this only fires for
                # `immich is None` or a minimal hand-built test double that
                # predates the method. Either way a `workflow_id` was
                # recorded, so a workflow may genuinely still exist in
                # Immich: warn exactly as a failed delete would rather than
                # silently doing nothing, which is the one outcome the
                # warning mechanism exists to prevent.
                warning = (
                    f"Could not delete workflow {workflow_id} in Immich (no client available). "
                    "Remove it there by hand, or it will keep firing into a rejected webhook."
                )

        account.stop.set()
        if account.thread is not None:
            account.thread.join(timeout=5.0)
        try:
            close = getattr(account.services.runtime, "close", None)
            if close is not None:
                close()
            _close_outgoing_immich_client(immich, getattr(account.services.runtime, "_pool", None))
        except Exception as exc:  # noqa: BLE001 - see Ruling R14 above
            close_warning = (
                f"Removed the account, but closing its Immich connection failed ({exc}). "
                "This is harmless -- nothing will use it again -- but the connection may "
                "linger until the process restarts."
            )
            warning = f"{warning} {close_warning}" if warning else close_warning

        try:
            self.accounts_repo.remove(account_id)
        finally:
            if delete_data:
                shutil.rmtree(account_dir(self._data_dir, account_id), ignore_errors=True)
        return warning
