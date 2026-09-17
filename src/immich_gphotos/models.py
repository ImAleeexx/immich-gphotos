from dataclasses import dataclass
from enum import IntEnum, StrEnum


class AssetState(StrEnum):
    PENDING = "pending"
    UPLOADING = "uploading"
    SYNCED = "synced"
    INELIGIBLE = "ineligible"
    FAILED = "failed"

    def is_terminal(self) -> bool:
        """Terminal means the reconciler will not re-enqueue it."""
        return self in {AssetState.SYNCED, AssetState.INELIGIBLE}


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
