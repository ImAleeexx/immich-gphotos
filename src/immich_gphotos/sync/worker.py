import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from immich_gphotos.clock import Clock
from immich_gphotos.config import Filters, RetryPolicy
from immich_gphotos.gphotos.protocol import GooglePhotosClient, GPhotosError
from immich_gphotos.models import AssetState, ErrorClass, Outcome, StoredAsset
from immich_gphotos.store.assets import AssetRepo
from immich_gphotos.sync.backoff import next_delay, should_quarantine
from immich_gphotos.sync.bytes import ByteResolver
from immich_gphotos.sync.eligibility import check_eligibility
from immich_gphotos.sync.throttle import TokenBucket

HALT_RETRY_DELAY = timedelta(minutes=10)

# How long a transfer-window-deferred asset waits before being reclaimed. Long
# enough that an idle background loop (which ticks every couple of seconds)
# does not re-run the remote hash check dozens of times a minute for the
# whole closed window; short enough that transfer resumes promptly once the
# window reopens or is widened.
WINDOW_RETRY_DELAY = timedelta(minutes=15)

# Bounds any single throttle wait `_throttle_upload` hands to `sleep`,
# regardless of the configured bandwidth cap. `time.sleep` is uninterruptible
# and runs on the single background-loop thread (see Runtime.tick), so an
# unbounded wait -- even from a validly low but nonzero
# bandwidth_bytes_per_second -- would stall the reconciler, backfill, album
# mirror and deletion sweep for as long as the cap and file size dictate. A
# wedged loop must not be reachable from any setting value, so this is
# enforced here rather than only at the API boundary. 30s is well under
# every other timer in this module (WINDOW_RETRY_DELAY, HALT_RETRY_DELAY)
# and IDLE_SLEEP_SECONDS, so a pathologically low cap slows one upload
# rather than stopping the world.
MAX_THROTTLE_WAIT_SECONDS = 30.0

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerResult:
    state: AssetState
    outcome: Outcome | None = None
    media_key: str | None = None
    reason: str | None = None
    error_class: ErrorClass | None = None
    halt: bool = False
    deferred: bool = False


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
        *,
        bandwidth: TokenBucket | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._assets = assets
        self._gphotos = gphotos
        self._resolver = resolver
        self._filters = filters
        self._retry = retry
        self._clock = clock
        # Shared across every worker thread that calls this same Worker
        # instance's process() -- one bucket, not one per thread, or the cap
        # would be wrong by a factor of the pool size. None means no cap and
        # must add no overhead (see _throttle_upload below).
        self._bandwidth = bandwidth
        # Injectable so tests can drive the wait with a FakeClock and assert
        # on it without a real sleep; production uses the real time.sleep.
        self._sleep = sleep

    def process(
        self,
        stored: StoredAsset,
        *,
        transfer_allowed: bool = True,
        album_allowlist_ids: frozenset[str] | None = None,
    ) -> WorkerResult:
        """Process one asset.

        `transfer_allowed` gates only the byte-moving step (resolving bytes
        off Immich and uploading them to Google). Eligibility, the local
        duplicate lookup and the remote hash check all run regardless, since
        the spec calls those "tiny" and wants them clearing the queue for
        free at any hour. An asset that clears eligibility and both dedup
        checks but still needs bytes moved, and is asked outside the
        schedule window, is handed back to the queue rather than uploaded --
        see the `deferred` branch below.

        `album_allowlist_ids` is the set of Immich asset ids the runtime
        resolved (once per tick, from `filters.album_allowlist`) as
        belonging to an allowed album. It is only consulted when
        `filters.album_allowlist` is actually set.
        """
        asset = stored.asset

        reason = check_eligibility(asset, self._filters, album_allowlist_ids)
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

            if not transfer_allowed:
                # Neither dedup check cleared it for free -- this asset
                # genuinely needs bytes moved. That's the one thing the
                # schedule window gates. Hand it back to PENDING without
                # counting an attempt (this is not the asset's fault, the
                # same reasoning `requeue` already exists for on the halt
                # path) and move on; the runtime keeps processing the rest
                # of the batch rather than stopping.
                self._assets.requeue(asset.immich_id, self._clock.now() + WINDOW_RETRY_DELAY)
                return WorkerResult(
                    AssetState.PENDING, reason="deferred: outside the transfer window", deferred=True
                )

            resolved = self._resolver.resolve(asset)
            try:
                self._throttle_upload(resolved.path)
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
            # last_error only keeps a 500-char truncated message with no traceback,
            # so the full exception must be captured here or it is lost forever.
            logger.exception("unexpected error processing asset %s", asset.immich_id)
            return self._handle_failure(stored, ErrorClass.UNKNOWN, str(exc))

    def _throttle_upload(self, path: Path) -> None:
        """Make the caller wait its share of the configured upload bandwidth cap.

        Only the upload leg is metered, not the download/resolve leg: the cap
        is documented (and read back from the API) as "cap on upload
        throughput" -- it protects the outbound link to Google, which is
        often the metered/constrained one (e.g. a residential or mobile
        upstream), not the local read from Immich's own volume or API, which
        is a different link entirely and, in the direct-read fast path, may
        not even cross the network. Metering it too would throttle local disk
        reads for no reason the setting claims to cover.

        No bucket configured (`self._bandwidth is None`, i.e. no cap) takes
        this branch and returns immediately -- no lock, no clock call, no
        stat() -- so an unconfigured cap adds no overhead.
        """
        if self._bandwidth is None:
            return
        try:
            size = path.stat().st_size
        except OSError:
            # Can't size the file (already gone, races with release, ...) --
            # never let metering itself fail the upload.
            return
        wait = self._bandwidth.take(size)
        if wait > 0:
            # Cap regardless of configuration -- see MAX_THROTTLE_WAIT_SECONDS.
            # The bucket's own accounting is unaffected: it measures elapsed
            # wall-clock time on the next `take()` call, not whether this
            # caller actually waited the full amount, so a capped sleep never
            # desyncs the token count -- it only means a pathologically low
            # cap is exceeded rather than the loop being wedged.
            self._sleep(min(wait, MAX_THROTTLE_WAIT_SECONDS))

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
