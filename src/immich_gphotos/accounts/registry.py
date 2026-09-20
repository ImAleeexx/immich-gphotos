"""The registry: owns the control database, every account's runtime graph,
and the one background-loop thread each account runs while the process is up.

`main.py` builds exactly one of these at boot. Everything that used to be a
single global (`Services`, the loop thread, the `Redactor`) now lives per
account inside it, except the `Redactor`, which stays one instance shared by
every account -- see the class docstring below for why.
"""

import logging
import os
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
)
from immich_gphotos.accounts.migrate import account_dir, ensure_control_db
from immich_gphotos.clock import Clock, SystemClock
from immich_gphotos.composition import rebuild_runtime
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

    def _load(self) -> None:
        # Read once and passed to every account: the global half of the
        # settings row lives in this registry's own control database, not in
        # any one account's, so each account's `_merged_settings` needs the
        # same copy of it.
        global_settings = self.settings.get(SETTINGS_KEY)
        self._bandwidth, self._gate = self._build_limiters(global_settings)
        for record in self.accounts_repo.list():
            services, loops = build_account_services(
                account_dir(self._data_dir, record.id),
                clock=self._clock,
                redactor=self._redactor,
                env=self._env,
                global_settings=global_settings,
                bandwidth=self._bandwidth,
                gate=self._gate,
            )
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
