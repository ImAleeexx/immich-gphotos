from immich_gphotos.accounts.control import CONTROL_DB_NAME
from immich_gphotos.accounts.migrate import LEGACY_DB_NAME, account_dir
from immich_gphotos.accounts.registry import AccountRegistry
from immich_gphotos.storage_keys import SECRET_KEY
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
