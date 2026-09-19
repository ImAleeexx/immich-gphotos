"""Adopt a v1 (single-account) data directory into the v2 layout.

v1:  /data/immich-gphotos.db
v2:  /data/control.db  +  /data/accounts/<id>/immich-gphotos.db

The move happens first and the control database is renamed into place last,
so the rename is the commit point: a crash anywhere earlier leaves an account
directory with no control database, which the next boot adopts rather than
orphans. The password, session and settings rows are COPIED, not moved out of
the account database -- leaving the originals in place is what makes that
replay possible. They become dead rows; nothing reads them again.
"""

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


def account_dir(data_dir: Path, account_id: str) -> Path:
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

    account_conn = connect(account_dir(data_dir, primary) / LEGACY_DB_NAME)
    account = SettingRepo(account_conn)
    for key in _CARRIED_TO_CONTROL:
        value = account.get(key)
        if value is not None:
            control.set(key, value)

    stored = account.get(SETTINGS_KEY)
    if isinstance(stored, dict):
        control.set(SETTINGS_KEY, {k: v for k, v in stored.items() if k in GLOBAL_SETTING_KEYS})
        account.set(SETTINGS_KEY, {k: v for k, v in stored.items() if k not in GLOBAL_SETTING_KEYS})
    account_conn.close()

    control.set(LEGACY_WEBHOOK_ACCOUNT_KEY, primary)
    control_conn.close()
    # The commit point. Everything above is replayable; this is not.
    tmp.replace(data_dir / CONTROL_DB_NAME)
    return primary
