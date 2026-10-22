"""Back up the motion half of Apple Live Photos after the still has synced.

The worker handles the still itself (see `sync.worker`). This completes the
*motion* for stills already SYNCED, under the account's `live_photo_mode`:

- `original_pair`: the original companion `.MOV` is uploaded as its own
  Google item under the SHA-1 of its unmodified bytes, and its asset row is
  marked SYNCED to that item. Runs automatically for Live Photos the worker
  queued under this mode (`pending`), e.g. a companion whose row arrived
  after the still, or was unreadable for a while.
- `derived_motion`: a Motion Photo is built in scratch from both originals
  and uploaded as an *additional* item. Only ever reached through an
  explicit reprocess request (`AssetRepo.request_live_photo_reprocess`) or
  a reprocess-queued row, because the still is already in Google and doing
  this unasked would put a second copy of it there.

Every step is idempotent: before any upload the content SHA-1 is looked up
in Google (read-only), so a crash between upload and bookkeeping is resolved
by the dedup hit on the next attempt rather than by a second POST. Nothing
here deletes, replaces or re-uploads the still, and nothing is processed for
a row recorded under a different mode than the current one.
"""

import logging
from dataclasses import dataclass
from datetime import timedelta

from immich_gphotos.clock import Clock
from immich_gphotos.gphotos.protocol import GooglePhotosClient, GPhotosError
from immich_gphotos.models import AssetState, Outcome, StoredAsset
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.sync import livephoto
from immich_gphotos.sync.bytes import ByteResolver

logger = logging.getLogger(__name__)

MAX_COMPLETION_ATTEMPTS = 8
_RETRY_BASE = timedelta(minutes=15)
_PARKED_COMPANION_REASONS = frozenset({"hidden", livephoto.LIVE_PHOTO_PAIRED_REASON})


@dataclass(frozen=True)
class LivePhotoSweepResult:
    completed: int = 0
    pending: int = 0
    failed: int = 0


class LivePhotoCompleter:
    def __init__(
        self,
        assets: AssetRepo,
        gphotos: GooglePhotosClient,
        resolver: ByteResolver,
        clock: Clock,
        mode: str,
    ) -> None:
        self._assets = assets
        self._gphotos = gphotos
        self._resolver = resolver
        self._clock = clock
        self._mode = mode

    def sweep(self, limit: int = 50) -> LivePhotoSweepResult:
        completed = pending = failed = 0
        if self._mode == "still_only_legacy":
            return LivePhotoSweepResult()
        for row in self._assets.live_photos_due(self._mode, limit=limit):
            try:
                status = self.complete(row["still_id"])
            except GPhotosError as exc:
                if exc.error_class.halts_transfer() or exc.error_class.value == "rate_limited":
                    pending += 1
                    break
                status = livephoto.STATUS_PENDING
            if status == livephoto.STATUS_COMPLETE:
                completed += 1
            elif status == livephoto.STATUS_PENDING:
                pending += 1
            else:
                failed += 1
        return LivePhotoSweepResult(completed=completed, pending=pending, failed=failed)

    def _retry_later(self, row, reason: str) -> str:  # noqa: ANN001 - sqlite3.Row
        attempts = int(row["attempts"]) + 1
        if attempts >= MAX_COMPLETION_ATTEMPTS:
            self._assets.record_live_photo(
                row["still_id"],
                row["video_id"],
                mode=row["mode"],
                status=livephoto.STATUS_DEFERRED,
                reason=reason,
                attempts=attempts,
            )
            return livephoto.STATUS_DEFERRED
        self._assets.record_live_photo(
            row["still_id"],
            row["video_id"],
            mode=row["mode"],
            status=livephoto.STATUS_PENDING,
            reason=reason,
            attempts=attempts,
            next_attempt_at=self._clock.now() + _RETRY_BASE * (2 ** min(attempts - 1, 6)),
        )
        return livephoto.STATUS_PENDING

    def _fail(self, row, reason: str) -> str:  # noqa: ANN001 - sqlite3.Row
        self._assets.record_live_photo(
            row["still_id"], row["video_id"], mode=row["mode"], status=livephoto.STATUS_FAILED, reason=reason
        )
        return livephoto.STATUS_FAILED

    def complete(self, still_id: str) -> str:
        """Try to complete one pending Live Photo; returns its resulting status.

        A GPhotosError propagates after the row has been rescheduled, so the
        sweep can stop on rate limiting / a halting error.
        """
        row = self._assets.live_photo(still_id)
        still = self._assets.get(still_id)
        if row is None or still is None:
            return livephoto.STATUS_FAILED
        if row["mode"] != self._mode or row["status"] != livephoto.STATUS_PENDING:
            return row["status"]
        if still.state is not AssetState.SYNCED:
            return livephoto.STATUS_PENDING
        if still.asset.is_trashed:
            return self._fail(row, "still_trashed")
        companion = self._assets.get(row["video_id"])
        if companion is None:
            return self._retry_later(row, "companion_missing")
        if companion.state in (AssetState.PENDING, AssetState.UPLOADING):
            return self._retry_later(row, "companion_not_settled")
        if companion.asset.is_trashed:
            return self._fail(row, "companion_trashed")
        try:
            if self._mode == "original_pair":
                return self._complete_original_pair(row, still, companion)
            return self._complete_derived(row, still)
        except GPhotosError as exc:
            self._retry_later(row, f"google_{exc.error_class.value}")
            raise
        except OSError as exc:
            logger.debug("Live Photo %s motion unreadable: %s", still_id, exc)
            return self._retry_later(row, "companion_unreadable")

    def _upload_or_find(self, resolved, filename: str) -> tuple[str, Outcome]:  # noqa: ANN001
        content_hash = resolved.sha1_b64
        assert content_hash is not None
        existing = self._gphotos.exists(content_hash)
        if existing:
            return existing, Outcome.ALREADY_PRESENT
        key = self._gphotos.upload(
            resolved.path,
            checksum=content_hash,
            filename=filename,
            source=self._resolver.upload_source(resolved),
        )
        return key, Outcome.UPLOADED

    def _complete_original_pair(self, row, still: StoredAsset, companion: StoredAsset) -> str:  # noqa: ANN001
        if companion.state is AssetState.SYNCED and companion.media_key:
            media_key, video_sha1 = companion.media_key, companion.upload_sha1
        elif (
            companion.state is AssetState.INELIGIBLE
            and companion.ineligible_reason in _PARKED_COMPANION_REASONS
        ):
            resolved = self._resolver.resolve(companion.asset)
            try:
                media_key, outcome = self._upload_or_find(resolved, companion.asset.filename)
            finally:
                self._resolver.release(resolved)
            video_sha1 = resolved.sha1_b64
            assert video_sha1 is not None
            if not self._assets.mark_companion_synced(
                companion.asset.immich_id, media_key, outcome, upload_sha1=video_sha1
            ):
                return self._retry_later(row, "companion_row_changed")
        else:
            return self._fail(row, f"companion_{companion.ineligible_reason or companion.state.value}")
        self._assets.record_live_photo(
            row["still_id"],
            row["video_id"],
            mode=row["mode"],
            status=livephoto.STATUS_COMPLETE,
            video_sha1=video_sha1,
            video_media_key=media_key,
        )
        self._assets.remove_sync_note(row["still_id"], livephoto.MOTION_NOT_BACKED_UP)
        return livephoto.STATUS_COMPLETE

    def _complete_derived(self, row, still: StoredAsset) -> str:  # noqa: ANN001
        resolved = self._resolver.resolve(still.asset, mux_live_photo=True)
        try:
            if not resolved.derived:
                reason = resolved.live_failure or "mux_failed"
                if resolved.live_failure_transient:
                    return self._retry_later(row, reason)
                return self._fail(row, reason)
            media_key, _ = self._upload_or_find(resolved, still.asset.filename)
        finally:
            self._resolver.release(resolved)
        self._assets.record_live_photo(
            row["still_id"],
            row["video_id"],
            mode=row["mode"],
            status=livephoto.STATUS_COMPLETE,
            still_sha1=resolved.raw_still_sha1,
            derived_sha1=resolved.sha1_b64,
            derived_media_key=media_key,
        )
        try:
            self._assets.mark_companion_paired(row["video_id"], livephoto.LIVE_PHOTO_PAIRED_REASON)
        except Exception as exc:  # noqa: BLE001 - bookkeeping only
            logger.debug("could not mark %s paired: %s", row["video_id"], exc)
        self._assets.remove_sync_note(row["still_id"], livephoto.MOTION_NOT_BACKED_UP)
        return livephoto.STATUS_COMPLETE
