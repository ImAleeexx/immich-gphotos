"""Service accounts that back up into the same Google account agree on stars
through the control database's ledger (sync.favorite_ledger)."""

from dataclasses import replace

from immich_gphotos.accounts.control import connect_control
from immich_gphotos.accounts.registry import AccountRegistry
from immich_gphotos.clock import FakeClock
from immich_gphotos.gphotos.client import GpmcClient
from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.models import Asset, Outcome, Priority
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.store.db import connect
from immich_gphotos.sync.favorite_ledger import SharedFavoriteLedger, google_identity
from immich_gphotos.sync.favorites import FavoriteSyncer

BASE = Asset(
    immich_id="x",
    checksum="sum-x",
    filename="IMG.JPG",
    type="IMAGE",
    size_bytes=3,
    immich_updated_at="2026-09-17T10:00:00Z",
    original_path=None,
    visibility="timeline",
    is_offline=False,
    is_trashed=False,
)
GOOGLE = "family@example.com"


def _account(tmp_path, name, ledger, gphotos):
    assets = AssetRepo(connect(tmp_path / f"{name}.db"), FakeClock())
    syncer = FavoriteSyncer(gphotos, assets, ledger=ledger, google_account=GOOGLE, account_id=name)
    return assets, syncer


def _synced(assets, immich_id, key, *, fav, outcome):
    asset = replace(BASE, immich_id=immich_id, checksum=f"sum-{immich_id}", is_favorite=fav)
    assets.upsert_pending(asset, Priority.WEBHOOK)
    assets.claim_next(limit=1)
    assets.mark_synced(immich_id, key, outcome)
    return asset


def _star(assets, asset, fav):
    assets.upsert_pending(replace(asset, is_favorite=fav), Priority.RECONCILE)


def _rig(tmp_path):
    ledger = SharedFavoriteLedger(connect_control(tmp_path / "control.db"))
    gphotos = FakeGooglePhotosClient()  # one Google account behind both
    a_assets, a = _account(tmp_path, "acct-a", ledger, gphotos)
    b_assets, b = _account(tmp_path, "acct-b", ledger, gphotos)
    return ledger, gphotos, (a_assets, a), (b_assets, b)


def test_google_identity_is_the_auth_email_normalised():
    assert google_identity("androidId=1&Email=Family%40Example.com&Token=t") == GOOGLE
    assert google_identity("Token=t") is None
    assert google_identity(None) is None
    assert GpmcClient("Email=Family%40Example.com&Token=t").google_account == GOOGLE


def test_one_account_unstarring_never_removes_a_star_another_account_wants(tmp_path):
    _, gphotos, (a_assets, a), (b_assets, b) = _rig(tmp_path)
    # B uploaded the item (so a star on it would be "its own"); A's copy
    # deduped onto it and is starred in A's Immich.
    b_photo = _synced(b_assets, "b1", "shared", fav=False, outcome=Outcome.UPLOADED)
    _synced(a_assets, "a1", "shared", fav=True, outcome=Outcome.ALREADY_PRESENT)
    a.sync_once()
    assert gphotos.favorites == {"shared": True}

    # B's user stars and then unstars their copy.
    _star(b_assets, b_photo, True)
    b.sync_once()
    _star(b_assets, b_photo, False)
    for _ in range(3):
        b.sync_once()
        a.sync_once()
    assert gphotos.favorites == {"shared": True}
    assert gphotos.favorite_calls == [("shared", True)]  # no flapping, no unstar


def test_the_star_goes_once_no_account_wants_it(tmp_path):
    _, gphotos, (a_assets, a), (b_assets, b) = _rig(tmp_path)
    a_photo = _synced(a_assets, "a1", "shared", fav=True, outcome=Outcome.UPLOADED)
    _synced(b_assets, "b1", "shared", fav=False, outcome=Outcome.ALREADY_PRESENT)
    a.sync_once()
    b.sync_once()
    _star(a_assets, a_photo, False)
    a.sync_once()
    assert gphotos.favorite_calls == [("shared", True), ("shared", False)]


def test_another_account_in_flux_holds_an_unstar(tmp_path):
    _, gphotos, (a_assets, a), (b_assets, b) = _rig(tmp_path)
    a_photo = _synced(a_assets, "a1", "shared", fav=True, outcome=Outcome.UPLOADED)
    _synced(b_assets, "b1", "shared", fav=False, outcome=Outcome.ALREADY_PRESENT)
    a.sync_once()
    # B has a second copy on the same item that is not settled (failed
    # mid-sync), so B cannot vouch for "unstarred" and publishes "held".
    _synced(b_assets, "b2", "shared", fav=False, outcome=Outcome.ALREADY_PRESENT)
    with b_assets._conn.lock:
        b_assets._conn.execute("UPDATE asset SET state = 'failed' WHERE immich_id = 'b2'")
    assert b.reconcile_key("shared") == "held"
    _star(a_assets, a_photo, False)
    assert a.reconcile_key("shared") == "held"
    assert gphotos.favorites == {"shared": True}


def test_a_star_set_by_hand_in_google_stays_protected_across_accounts(tmp_path):
    _, gphotos, (a_assets, a), (b_assets, b) = _rig(tmp_path)
    # Neither account uploaded the item: it was already in Google, and a
    # star there may be the user's own.
    a_photo = _synced(a_assets, "a1", "shared", fav=True, outcome=Outcome.ALREADY_PRESENT)
    _synced(b_assets, "b1", "shared", fav=False, outcome=Outcome.ALREADY_PRESENT)
    a.sync_once()
    _star(a_assets, a_photo, False)
    assert a.reconcile_key("shared") == "protected"
    assert gphotos.favorite_calls == [("shared", True)]


def test_different_google_accounts_do_not_share(tmp_path):
    ledger = SharedFavoriteLedger(connect_control(tmp_path / "control.db"))
    g1, g2 = FakeGooglePhotosClient(), FakeGooglePhotosClient()
    a_assets = AssetRepo(connect(tmp_path / "a.db"), FakeClock())
    b_assets = AssetRepo(connect(tmp_path / "b.db"), FakeClock())
    a = FavoriteSyncer(g1, a_assets, ledger=ledger, google_account="one@example.com", account_id="a")
    b = FavoriteSyncer(g2, b_assets, ledger=ledger, google_account="two@example.com", account_id="b")
    _synced(a_assets, "a1", "k", fav=True, outcome=Outcome.UPLOADED)
    b_photo = _synced(b_assets, "b1", "k", fav=True, outcome=Outcome.UPLOADED)
    a.sync_once()
    b.sync_once()
    _star(b_assets, b_photo, False)
    b.sync_once()
    assert g2.favorites == {"k": False}
    assert g1.favorites == {"k": True}


def test_the_registry_shares_one_ledger_and_forgets_removed_accounts(tmp_path):
    registry = AccountRegistry(tmp_path, env={})
    first = registry.create("Mum")
    second = registry.create("Dad")
    assert first.services.favorite_ledger is registry.favorite_ledger
    assert second.services.favorite_ledger is registry.favorite_ledger
    assert (first.services.account_id, second.services.account_id) == (first.id, second.id)
    registry.favorite_ledger.publish(GOOGLE, "k", second.id, intent=True, managed=True)
    registry.remove(second.id, delete_data=False)
    assert registry.favorite_ledger.accounts_sharing(GOOGLE) == set()
