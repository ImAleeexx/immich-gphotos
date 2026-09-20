from dataclasses import dataclass
from enum import IntEnum, StrEnum


class AssetState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    SYNCED = "synced"
    INELIGIBLE = "ineligible"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        """Terminal means the reconciler will not re-enqueue it.

        This is true of every INELIGIBLE row *except* one whose
        `ineligible_reason` is `ALBUM_EXCLUDED_REASON` -- see that constant
        and `store.assets.AssetRepo.upsert_pending`, which is where that
        distinction is actually enforced (this method only ever sees a
        bare `AssetState`, not the reason, so it cannot make the
        distinction itself).
        """
        return self in {AssetState.SYNCED, AssetState.INELIGIBLE}


# `sync.eligibility.check_eligibility` returns this as the ineligibility
# reason when `filters.album_allowlist` is set and the asset is not (yet, or
# any longer) a member of an allowed album. Album membership is mutable and
# resolved fresh every tick, so -- unlike every other ineligibility reason --
# this one must not stick forever the way plain INELIGIBLE otherwise does:
# `store.assets.AssetRepo.upsert_pending` reopens a row carrying this exact
# reason back to PENDING instead of leaving it terminal, which is what lets a
# later webhook or reconciler pass actually re-evaluate membership. Shared
# between the two modules (rather than each hardcoding the string "album_
# excluded") so the producer and the one place that must special-case it can
# never drift apart.
ALBUM_EXCLUDED_REASON = "album_excluded"


class Outcome(StrEnum):
    UPLOADED = "uploaded"
    ALREADY_PRESENT = "already_present"


class ErrorClass(StrEnum):
    TRANSIENT = "transient"
    RATE_LIMITED = "rate_limited"
    ASSET_UNAVAILABLE = "asset_unavailable"
    UNSUPPORTED_MEDIA = "unsupported_media"
    AUTH_INVALID = "auth_invalid"
    QUOTA_EXHAUSTED = "quota_exhausted"
    UNKNOWN = "unknown"

    def halts_transfer(self) -> bool:
        """These need a human. Retrying them burns attempts and helps nobody."""
        return self in {ErrorClass.AUTH_INVALID, ErrorClass.QUOTA_EXHAUSTED}


class Priority(IntEnum):
    WEBHOOK = 0
    RECONCILE = 1
    BACKFILL = 2


@dataclass(frozen=True)
class Asset:
    """An Immich asset as seen from either the webhook payload or the search API."""

    immich_id: str
    checksum: str  # base64 SHA-1
    filename: str
    type: str  # "IMAGE" | "VIDEO"
    size_bytes: int | None
    immich_updated_at: str
    original_path: str | None
    visibility: str  # timeline | archive | hidden | locked
    is_offline: bool
    is_trashed: bool
    tags: tuple[str, ...] = ()
    # Immich's `fileCreatedAt`: when the photo/video was taken, as opposed to
    # `immich_updated_at`, which is when the row last changed. Carried purely
    # so `sync.bytes.ByteResolver` can stamp it onto a downloaded scratch file
    # -- gpmc reads the file's mtime and sends it to Google as the capture
    # timestamp, which Google honours for any file whose bytes hold no date of
    # their own. Optional: a webhook payload or an Immich version that omits
    # the field simply leaves the upload undated, as before.
    taken_at: str | None = None


@dataclass(frozen=True)
class StoredAsset:
    """A row as persisted, including sync bookkeeping."""

    asset: Asset
    state: AssetState
    outcome: Outcome | None
    media_key: str | None
    priority: Priority
    attempts: int
    next_attempt_at: str | None
    error_class: ErrorClass | None
    last_error: str | None
    ineligible_reason: str | None
