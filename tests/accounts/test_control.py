from immich_gphotos.accounts.control import (
    AccountRepo,
    connect_control,
    new_account_id,
)


def test_accounts_come_back_in_position_order(tmp_path):
    repo = AccountRepo(connect_control(tmp_path / "control.db"))
    repo.add(account_id="aaa", label="Alex", created_at="2026-09-20T10:00:00Z")
    repo.add(account_id="bbb", label="Mum", created_at="2026-09-20T10:01:00Z")
    assert [a.label for a in repo.list()] == ["Alex", "Mum"]
    assert [a.position for a in repo.list()] == [0, 1]


def test_get_returns_none_for_an_unknown_id(tmp_path):
    repo = AccountRepo(connect_control(tmp_path / "control.db"))
    assert repo.get("nope") is None


def test_rename_keeps_the_id_and_position(tmp_path):
    repo = AccountRepo(connect_control(tmp_path / "control.db"))
    repo.add(account_id="aaa", label="Alex", created_at="2026-09-20T10:00:00Z")
    repo.rename("aaa", "Alexandra")
    record = repo.get("aaa")
    assert (record.id, record.label, record.position) == ("aaa", "Alexandra", 0)


def test_remove_drops_only_that_account(tmp_path):
    repo = AccountRepo(connect_control(tmp_path / "control.db"))
    repo.add(account_id="aaa", label="Alex", created_at="2026-09-20T10:00:00Z")
    repo.add(account_id="bbb", label="Mum", created_at="2026-09-20T10:01:00Z")
    repo.remove("aaa")
    assert [a.id for a in repo.list()] == ["bbb"]


def test_the_control_database_does_not_run_the_asset_column_migrations(tmp_path):
    """Regression: `apply_migrations` ALTERs the `asset` table, which the
    control database does not have. Running them here raised
    `sqlite3.OperationalError: no such table: asset` on every boot."""
    conn = connect_control(tmp_path / "control.db")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"account", "setting"}


def test_account_ids_are_opaque_and_unique():
    assert new_account_id() != new_account_id()
    assert len(new_account_id()) == 12
    assert int(new_account_id(), 16) >= 0
