"""Adopt a v1 (single-account) data directory into the v2 layout.

v1:  /data/immich-gphotos.db
v2:  /data/control.db  +  /data/accounts/<id>/immich-gphotos.db

The move happens first and the control database is renamed into place last,
so the rename is the commit point: a crash anywhere earlier leaves an account
directory with no control database, which the next boot adopts rather than
orphans. Nothing writes to the account database before that point either --
the password, session and settings rows are only READ until control.db exists,
so a crash before the rename leaves the account database exactly as it was
and the next boot's replay sees real data, not already-stripped leftovers.
The one write the migration does make to the account database (trimming the
global keys out of its settings row) happens after the rename, once there is
a durable control.db to hold them; see the comment at that call.
"""

import contextlib
import re
import shutil
from pathlib import Path

from immich_gphotos.accounts.control import (
    CONTROL_DB_NAME,
    AccountRepo,
    connect_control,
    new_account_id,
)
from immich_gphotos.storage_keys import (
    GLOBAL_SETTING_KEYS,
    LEGACY_WEBHOOK_ACCOUNT_KEY,
    PASSWORD_KEY,
    SESSION_COOKIE,
    SETTINGS_KEY,
)
from immich_gphotos.store.db import connect
from immich_gphotos.store.kv import SettingRepo

LEGACY_DB_NAME = "immich-gphotos.db"
ACCOUNTS_DIRNAME = "accounts"
_CARRIED_TO_CONTROL = (PASSWORD_KEY, SESSION_COOKIE)

# `new_account_id()` only ever hands out lowercase hex, but ids also come
# from hand-written test fixtures ("acct-1", "acct-boot") and, at the API
# boundary, straight off a URL path segment (`DELETE /api/accounts/{id}`) --
# so this is deliberately a little wider than "hex" to keep those working,
# not an attempt to describe every id this project has ever produced. What it
# actually guards against is a `..` or a `/` reaching `shutil.rmtree` (see
# `AccountRegistry.remove`) or a bare filesystem join anywhere else: every
# caller of `account_dir` currently happens to check the id against the
# registry first, but that is caller discipline, not a property of this
# function -- and a directory-traversal id has no legitimate use, so it is
# rejected here once rather than trusted wherever this is called from next.
_SAFE_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def account_dir(data_dir: Path, account_id: str) -> Path:
    if not _SAFE_ACCOUNT_ID.fullmatch(account_id):
        raise ValueError(f"unsafe account id: {account_id!r}")
    return Path(data_dir) / ACCOUNTS_DIRNAME / account_id


def _existing_account_ids(data_dir: Path) -> list[str]:
    root = Path(data_dir) / ACCOUNTS_DIRNAME
    if not root.is_dir():
        return []
    return sorted(d.name for d in root.iterdir() if (d / LEGACY_DB_NAME).is_file())


def _adopt_legacy_database(data_dir: Path) -> str | None:
    """Move /data/immich-gphotos.db into its own account directory."""
    legacy = Path(data_dir) / LEGACY_DB_NAME
    if not legacy.is_file():
        return None
    # Fold the WAL back into the main file before moving it. Moving a
    # database while a -wal exists and leaving the sidecar behind loses every
    # write still in it.
    conn = connect(legacy)
    with conn.lock:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    account_id = new_account_id()
    target = account_dir(data_dir, account_id)
    target.mkdir(parents=True, exist_ok=True)
    for path in (legacy, *legacy.parent.glob(f"{LEGACY_DB_NAME}-*")):
        shutil.move(str(path), str(target / path.name))
    return account_id


def ensure_control_db(data_dir: Path, *, now: str) -> str | None:
    """Bring `data_dir` up to the v2 layout. Returns the adopted account id.

    Safe and cheap to call on every boot: it returns immediately once
    `control.db` exists.
    """
    data_dir = Path(data_dir)
    if (data_dir / CONTROL_DB_NAME).exists():
        return None

    adopted = _adopt_legacy_database(data_dir)
    account_ids = _existing_account_ids(data_dir)
    if not account_ids:
        return None
    primary = adopted or account_ids[0]

    tmp = data_dir / f"{CONTROL_DB_NAME}.tmp"
    tmp.unlink(missing_ok=True)
    control_conn = connect_control(tmp)
    accounts = AccountRepo(control_conn)
    control = SettingRepo(control_conn)

    for index, account_id in enumerate(account_ids):
        accounts.add(
            account_id=account_id,
            label="Default" if account_id == primary else f"Account {index + 1}",
            created_at=now,
        )

    # `closing()` guarantees the account connection is released even if the
    # rename below raises -- the exact failure this function exists to
    # survive. A bare `account_conn.close()` after the rename would leak the
    # connection (and, since `store.db.connect` opens the file in WAL mode,
    # leave its `-wal`/`-shm` sidecars open) on that path.
    with contextlib.closing(connect(account_dir(data_dir, primary) / LEGACY_DB_NAME)) as account_conn:
        account = SettingRepo(account_conn)
        for key in _CARRIED_TO_CONTROL:
            value = account.get(key)
            if value is not None:
                control.set(key, value)

        stored = account.get(SETTINGS_KEY)
        account_settings = None
        if isinstance(stored, dict):
            control.set(SETTINGS_KEY, {k: v for k, v in stored.items() if k in GLOBAL_SETTING_KEYS})
            account_settings = {k: v for k, v in stored.items() if k not in GLOBAL_SETTING_KEYS}

        control.set(LEGACY_WEBHOOK_ACCOUNT_KEY, primary)
        control_conn.close()
        # The commit point. Everything above is replayable; this is not.
        tmp.replace(data_dir / CONTROL_DB_NAME)

        # The one write this migration makes to the account database, and it
        # is deliberately on the far side of the commit point above.
        # `connect()` runs in autocommit mode, so writing this before the
        # rename would land on disk immediately -- and if the process then
        # crashed before the rename, the next boot's replay would read an
        # account settings row that had *already* lost
        # bandwidth_bytes_per_second/worker_threads, with no control.db
        # anywhere holding a copy of them. Gone for good, while everything
        # else about the migration (account id, password, session) still
        # recovers cleanly. Doing it here instead means that same crash just
        # leaves the global keys duplicated in the account row once
        # control.db already exists -- harmless, because whatever later
        # reads settings for use always prefers the control copy over the
        # account's.
        if account_settings is not None:
            account.set(SETTINGS_KEY, account_settings)
    return primary
