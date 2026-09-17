from dataclasses import replace

from immich_gphotos.config import Filters
from immich_gphotos.models import Asset
from immich_gphotos.sync.eligibility import check_eligibility

BASE = Asset(
    immich_id="a1",
    checksum="qvTGHdzF6KLavt4PO0gs2a6pQ00=",
    filename="IMG_0001.JPG",
    type="IMAGE",
    size_bytes=2_000_000,
    immich_updated_at="2026-09-17T10:00:00Z",
    original_path="/data/upload/a1.jpg",
    visibility="timeline",
    is_offline=False,
    is_trashed=False,
    tags=(),
)


def test_ordinary_timeline_asset_is_eligible():
    assert check_eligibility(BASE, Filters()) is None


def test_hidden_visibility_is_excluded():
    """This single rule excludes every extracted motion-photo and live-photo video."""
    assert check_eligibility(replace(BASE, visibility="hidden"), Filters()) == "hidden"


def test_locked_visibility_is_excluded():
    assert check_eligibility(replace(BASE, visibility="locked"), Filters()) == "locked"


def test_archived_is_included_by_default_and_excludable():
    archived = replace(BASE, visibility="archive")
    assert check_eligibility(archived, Filters()) is None
    assert check_eligibility(archived, Filters(include_archived=False)) == "archived"


def test_trashed_and_offline_are_excluded():
    assert check_eligibility(replace(BASE, is_trashed=True), Filters()) == "trashed"
    assert check_eligibility(replace(BASE, is_offline=True), Filters()) == "offline"


def test_size_cap():
    f = Filters(max_size_bytes=1_000_000)
    assert check_eligibility(BASE, f) == "too_large"
    assert check_eligibility(replace(BASE, size_bytes=None), f) is None  # unknown size passes


def test_type_filter():
    f = Filters(allowed_types=frozenset({"IMAGE"}))
    assert check_eligibility(replace(BASE, type="VIDEO"), f) == "type_excluded"


def test_raw_filter_is_case_insensitive():
    f = Filters(skip_raw=True)
    assert check_eligibility(replace(BASE, filename="DSC_1.arw"), f) == "raw"
    assert check_eligibility(replace(BASE, filename="DSC_1.CR2"), f) == "raw"
    assert check_eligibility(BASE, f) is None


def test_excluded_tag():
    f = Filters(excluded_tags=frozenset({"private"}))
    assert check_eligibility(replace(BASE, tags=("private",)), f) == "tag_excluded"


def test_reasons_are_stable_strings():
    """The UI groups by reason, so these must not drift."""
    assert check_eligibility(replace(BASE, visibility="hidden"), Filters()) == "hidden"
