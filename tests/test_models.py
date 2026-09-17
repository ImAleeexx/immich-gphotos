from immich_gphotos.models import Asset, AssetState, ErrorClass, Outcome, Priority


def test_priorities_order_webhook_first():
    assert Priority.WEBHOOK < Priority.RECONCILE < Priority.BACKFILL


def test_terminal_states():
    assert AssetState.SYNCED.is_terminal() is True
    assert AssetState.INELIGIBLE.is_terminal() is True
    assert AssetState.PENDING.is_terminal() is False
    assert AssetState.FAILED.is_terminal() is False  # quarantined, but retryable by hand


def test_error_classes_that_halt_all_transfer():
    assert ErrorClass.AUTH_INVALID.halts_transfer() is True
    assert ErrorClass.QUOTA_EXHAUSTED.halts_transfer() is True
    assert ErrorClass.TRANSIENT.halts_transfer() is False


def test_asset_is_hashable_and_frozen():
    a = Asset(
        immich_id="a1",
        checksum="QUJD",
        filename="IMG_0001.JPG",
        type="IMAGE",
        size_bytes=1024,
        immich_updated_at="2026-09-17T10:00:00Z",
        original_path="/data/upload/a1.jpg",
        visibility="timeline",
        is_offline=False,
        is_trashed=False,
        tags=("holiday",),
    )
    assert a.immich_id == "a1"
    assert Outcome.ALREADY_PRESENT.value == "already_present"
