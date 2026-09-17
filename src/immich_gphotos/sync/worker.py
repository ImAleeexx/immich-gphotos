from dataclasses import dataclass
from datetime import timedelta

from immich_gphotos.clock import Clock
from immich_gphotos.config import Filters, RetryPolicy
from immich_gphotos.gphotos.protocol import GooglePhotosClient, GPhotosError
from immich_gphotos.models import AssetState, ErrorClass, Outcome, StoredAsset
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.sync.backoff import next_delay, should_quarantine
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.eligibility import check_eligibility

HALT_RETRY_DELAY = timedelta(minutes=10)


@dataclass(frozen=True)
class WorkerResult:
    state: AssetState
    outcome: Outcome | None = None
    media_key: str | None = None
    reason: str | None = None
    error_class: ErrorClass | None = None
    halt: bool = False


class Worker:
    """Process one asset. Every step is idempotent, so re-running is always safe."""

    def __init__(
        self,
        assets: AssetRepo,
        gphotos: GooglePhotosClient,
        resolver: ByteResolver,
        filters: Filters,
        retry: RetryPolicy,
        clock: Clock,
    ) -> None:
        self._assets = assets
        self._gphotos = gphotos
        self._resolver = resolver
        self._filters = filters
        self._retry = retry
        self._clock = clock

    def process(self, stored: StoredAsset) -> WorkerResult:
        asset = stored.asset

        reason = check_eligibility(asset, self._filters)
        if reason is not None:
            self._assets.mark_ineligible(asset.immich_id, reason)
            return WorkerResult(AssetState.INELIGIBLE, reason=reason)

        local_key = self._assets.media_key_for_checksum(asset.checksum)
        if local_key:
            self._assets.mark_synced(asset.immich_id, local_key, Outcome.ALREADY_PRESENT)
            return WorkerResult(AssetState.SYNCED, Outcome.ALREADY_PRESENT, local_key)

        try:
            remote_key = self._gphotos.exists(asset.checksum)
            if remote_key:
                self._assets.mark_synced(asset.immich_id, remote_key, Outcome.ALREADY_PRESENT)
                return WorkerResult(AssetState.SYNCED, Outcome.ALREADY_PRESENT, remote_key)

            resolved = self._resolver.resolve(asset)
            try:
                media_key = self._gphotos.upload(
                    resolved.path, checksum=asset.checksum, filename=asset.filename
                )
            finally:
                self._resolver.release(resolved)

            self._assets.mark_synced(asset.immich_id, media_key, Outcome.UPLOADED)
            return WorkerResult(AssetState.SYNCED, Outcome.UPLOADED, media_key)

        except GPhotosError as exc:
            return self._handle_failure(stored, exc.error_class, str(exc))
        except OSError as exc:
            return self._handle_failure(stored, ErrorClass.ASSET_UNAVAILABLE, str(exc))
        except Exception as exc:  # noqa: BLE001 - never let one asset kill the worker
            return self._handle_failure(stored, ErrorClass.UNKNOWN, str(exc))

    def _handle_failure(self, stored: StoredAsset, error_class: ErrorClass, message: str) -> WorkerResult:
        asset_id = stored.asset.immich_id

        if error_class.halts_transfer():
            self._assets.requeue(asset_id, self._clock.now() + HALT_RETRY_DELAY)
            return WorkerResult(AssetState.PENDING, error_class=error_class, halt=True)

        attempts = stored.attempts + 1
        if should_quarantine(attempts, self._retry):
            self._assets.mark_failed(asset_id, error_class, message)
            return WorkerResult(AssetState.FAILED, error_class=error_class, reason=message)

        delay = next_delay(attempts, self._retry)
        self._assets.mark_retry(asset_id, error_class, message, self._clock.now() + delay)
        return WorkerResult(AssetState.PENDING, error_class=error_class, reason=message)
