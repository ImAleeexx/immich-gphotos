from pathlib import Path

import pytest

from immich_gphotos.accounts import migrate
from immich_gphotos.accounts.control import CONTROL_DB_NAME, AccountRepo, connect_control
from immich_gphotos.accounts.migrate import (
    ACCOUNTS_DIRNAME,
    LEGACY_DB_NAME,
    account_dir,
    ensure_control_db,
)
from immich_gphotos.storage_keys import (
    GOOGLE_AUTH_KEY,
    LEGACY_WEBHOOK_ACCOUNT_KEY,
    PASSWORD_KEY,
    SESSION_COOKIE,
    SETTINGS_KEY,
)
from immich_gphotos.store.db import connect
from immich_gphotos.store.kv import SettingRepo

NOW = "2026-09-20T10:00:00Z"


def _legacy_install(data_dir, **settings):
    """A v1 layout: one database at the root holding credentials, the admin
    password, the session and the settings row."""
    data_dir.mkdir(parents=True, exist_ok=True)
    repo = SettingRepo(connect(data_dir / LEGACY_DB_NAME))
    repo.set(PASSWORD_KEY, "salt$digest")
    repo.set(SESSION_COOKIE, "live-token")
    repo.set(GOOGLE_AUTH_KEY, "auth-data")
    repo.set(SETTINGS_KEY, {"quality": "saver", "bandwidth_bytes_per_second": 1048576, **settings})
    return repo


def test_a_fresh_data_dir_has_nothing_to_adopt(tmp_path):
    assert ensure_control_db(tmp_path, now=NOW) is None
    assert not (tmp_path / CONTROL_DB_NAME).exists()


@pytest.mark.parametrize("account_id", ["../escape", "a/b", "a\\b", "", "."])
def test_account_dir_rejects_an_id_that_is_not_a_safe_path_segment(tmp_path, account_id):
    """`account_dir` builds a filesystem path straight out of an account id
    that, at the API boundary, comes off a URL path segment
    (`DELETE /api/accounts/{id}`). Every current caller happens to check the
    id against the registry first, but that is caller discipline, not a
    property of this function -- so the guard belongs here, structurally,
    rather than depending on every future caller remembering to check."""
    with pytest.raises(ValueError):
        account_dir(tmp_path, account_id)


def test_account_dir_accepts_the_ids_this_project_actually_produces(tmp_path):
    """Hex ids from `new_account_id()`, and the hand-written hyphenated ids
    a lot of tests use ("acct-1", "acct-boot") must keep working."""
    for account_id in ("a1b2c3d4e5f6", "acct-1", "acct-boot", "Default"):
        assert account_dir(tmp_path, account_id) == tmp_path / ACCOUNTS_DIRNAME / account_id


def test_a_v1_install_is_adopted_as_the_first_account(tmp_path):
    _legacy_install(tmp_path)
    account_id = ensure_control_db(tmp_path, now=NOW)
    assert account_id is not None
    assert (account_dir(tmp_path, account_id) / LEGACY_DB_NAME).exists()
    assert not (tmp_path / LEGACY_DB_NAME).exists()
    records = AccountRepo(connect_control(tmp_path / CONTROL_DB_NAME)).list()
    assert [r.id for r in records] == [account_id]


def test_the_password_session_and_global_settings_move_to_control(tmp_path):
    _legacy_install(tmp_path)
    account_id = ensure_control_db(tmp_path, now=NOW)
    control = SettingRepo(connect_control(tmp_path / CONTROL_DB_NAME))
    assert control.get(PASSWORD_KEY) == "salt$digest"
    assert control.get(SESSION_COOKIE) == "live-token"
    assert control.get(SETTINGS_KEY) == {"bandwidth_bytes_per_second": 1048576}
    assert control.get(LEGACY_WEBHOOK_ACCOUNT_KEY) == account_id
    account = SettingRepo(connect(account_dir(tmp_path, account_id) / LEGACY_DB_NAME))
    assert account.get(SETTINGS_KEY) == {"quality": "saver"}
    assert account.get(GOOGLE_AUTH_KEY) == "auth-data"


def test_migrating_twice_is_a_no_op(tmp_path):
    _legacy_install(tmp_path)
    first = ensure_control_db(tmp_path, now=NOW)
    assert ensure_control_db(tmp_path, now=NOW) is None
    assert [r.id for r in AccountRepo(connect_control(tmp_path / CONTROL_DB_NAME)).list()] == [first]


def test_a_crash_before_the_control_rename_replays(tmp_path):
    """The move happens before the control database is committed, so a crash
    in between leaves an account directory and no control.db. The next boot
    must adopt that directory rather than orphan it."""
    _legacy_install(tmp_path)
    account_id = ensure_control_db(tmp_path, now=NOW)
    (tmp_path / CONTROL_DB_NAME).unlink()
    replayed = ensure_control_db(tmp_path, now=NOW)
    assert replayed == account_id
    assert [r.id for r in AccountRepo(connect_control(tmp_path / CONTROL_DB_NAME)).list()] == [account_id]


def test_a_crash_before_the_rename_does_not_lose_the_bandwidth_cap(tmp_path, monkeypatch):
    """Regression test for R8. Nothing may write the trimmed settings row to
    the account database before control.db is durably in place: drive the
    real code path by making the rename itself fail, and check the crash
    left the account's global keys untouched rather than already stripped."""
    _legacy_install(tmp_path)

    real_replace = Path.replace

    def _boom(self, target):
        if self.name == f"{CONTROL_DB_NAME}.tmp":
            raise OSError("simulated crash during the control-db rename")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _boom, raising=True)
    with pytest.raises(OSError):
        ensure_control_db(tmp_path, now=NOW)

    # The rename never happened, so exactly one account directory exists and
    # no control.db does -- the same state `ensure_control_db` must adopt on
    # the next boot.
    assert not (tmp_path / CONTROL_DB_NAME).exists()
    (account_id,) = [d.name for d in (tmp_path / "accounts").iterdir()]

    # The bug this guards against: if the account's settings row had already
    # been stripped of its global keys before the failed rename, they would
    # be gone for good here.
    account = SettingRepo(connect(account_dir(tmp_path, account_id) / LEGACY_DB_NAME))
    assert account.get(SETTINGS_KEY) == {"quality": "saver", "bandwidth_bytes_per_second": 1048576}

    monkeypatch.undo()
    replayed = ensure_control_db(tmp_path, now=NOW)
    assert replayed == account_id
    control = SettingRepo(connect_control(tmp_path / CONTROL_DB_NAME))
    assert control.get(SETTINGS_KEY) == {"bandwidth_bytes_per_second": 1048576}
    account = SettingRepo(connect(account_dir(tmp_path, account_id) / LEGACY_DB_NAME))
    assert account.get(SETTINGS_KEY) == {"quality": "saver"}


class _SpyConnection:
    """Wraps a real connection and records whether `close()` was called,
    without changing how it behaves for everything else (`SettingRepo` reads
    `.lock` and runs queries straight through the wrapped connection)."""

    def __init__(self, real):
        self._real = real
        self.closed = False

    def close(self):
        self.closed = True
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_the_account_connection_is_closed_even_when_the_rename_fails(tmp_path, monkeypatch):
    """Regression test: the R8 fix kept the account connection open across
    the rename so the settings write could happen after it, which means the
    rename raising must not skip closing it. Pin that by spying on the
    connection `ensure_control_db` opens for the account database and
    checking `close()` actually ran once the simulated rename failure has
    propagated out."""
    _legacy_install(tmp_path)

    spies: list[_SpyConnection] = []
    real_connect = migrate.connect

    def _spying_connect(path, *args, **kwargs):
        conn = real_connect(path, *args, **kwargs)
        # Only the account database connection is under test here -- the
        # legacy checkpoint connection in _adopt_legacy_database closes
        # itself immediately, well before the rename this test fails.
        if ACCOUNTS_DIRNAME in path.parts:
            spy = _SpyConnection(conn)
            spies.append(spy)
            return spy
        return conn

    monkeypatch.setattr(migrate, "connect", _spying_connect)

    real_replace = Path.replace

    def _boom(self, target):
        if self.name == f"{CONTROL_DB_NAME}.tmp":
            raise OSError("simulated crash during the control-db rename")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _boom, raising=True)

    with pytest.raises(OSError):
        ensure_control_db(tmp_path, now=NOW)

    assert len(spies) == 1
    assert spies[0].closed is True


def test_the_wal_sidecars_travel_with_the_database(tmp_path):
    repo = _legacy_install(tmp_path)
    repo.set("keep", "me")  # leaves a -wal file behind
    account_id = ensure_control_db(tmp_path, now=NOW)
    assert not list(tmp_path.glob("immich-gphotos.db-*"))
    moved = SettingRepo(connect(account_dir(tmp_path, account_id) / LEGACY_DB_NAME))
    assert moved.get("keep") == "me"
