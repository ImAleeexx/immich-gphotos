from immich_gphotos.config import Filters
from immich_gphotos.models import Asset

RAW_EXTENSIONS = frozenset(
    {
        ".arw", ".cr2", ".cr3", ".dng", ".nef", ".nrw", ".orf",
        ".pef", ".raf", ".raw", ".rw2", ".sr2", ".srw", ".x3f",
    }
)

SYNCABLE_VISIBILITY = frozenset({"timeline", "archive"})


def check_eligibility(asset: Asset, filters: Filters) -> str | None:
    """Return None if the asset should be synced, else a stable reason string.

    Excluding every visibility except timeline/archive is what keeps extracted
    motion-photo and live-photo videos out: Immich marks those `hidden`, and the
    still image they belong to still carries the embedded video.
    """
    if asset.visibility not in SYNCABLE_VISIBILITY:
        return asset.visibility
    if asset.visibility == "archive" and not filters.include_archived:
        return "archived"
    if asset.is_trashed:
        return "trashed"
    if asset.is_offline:
        return "offline"
    if asset.type not in filters.allowed_types:
        return "type_excluded"
    if filters.max_size_bytes is not None and asset.size_bytes is not None:
        if asset.size_bytes > filters.max_size_bytes:
            return "too_large"
    if filters.skip_raw:
        _, dot, extension = asset.filename.lower().rpartition(".")
        if dot and f".{extension}" in RAW_EXTENSIONS:
            return "raw"
    if filters.excluded_tags and set(asset.tags) & filters.excluded_tags:
        return "tag_excluded"
    return None
