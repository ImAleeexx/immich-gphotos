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
from dataclasses import dataclass, field
from pathlib import Path

from immich_gphotos.accounts.build import build_account_services
from immich_gphotos.accounts.control import (
    CONTROL_DB_NAME,
    AccountRecord,
    AccountRepo,
    connect_control,
)
from immich_gphotos.accounts.migrate import account_dir, ensure_control_db
from immich_gphotos.clock import Clock, SystemClock
from immich_gphotos.logging import Redactor, configure_logging
from immich_gphotos.services import Services
from immich_gphotos.storage_keys import LEGACY_WEBHOOK_ACCOUNT_KEY
from immich_gphotos.store.kv import SettingRepo
from immich_gphotos.sync.loops import LoopsHandle

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
        self._load()

    def _load(self) -> None:
        for record in self.accounts_repo.list():
            services, loops = build_account_services(
                account_dir(self._data_dir, record.id),
                clock=self._clock,
                redactor=self._redactor,
                env=self._env,
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
