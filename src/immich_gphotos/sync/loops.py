import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from immich_gphotos.clock import Clock
from immich_gphotos.config import Settings
from immich_gphotos.store.events import EventRepo

IDLE_SLEEP_SECONDS = 2.0
PAUSE_RETRY_AFTER = timedelta(minutes=10)

# Recovers assets a crash left claimed mid-upload. Run on the reconcile cadence:
# build_services only does this once at boot, which leaves stranded assets
# stuck until someone notices and restarts the container.
STALE_UPLOAD_AGE = timedelta(hours=1)

logger = logging.getLogger(__name__)


class BackgroundLoops:
    """Everything that runs on a timer, expressed as one testable `iterate()`."""

    def __init__(
        self,
        runtime,
        reconciler,
        backfill,
        album_mirror,
        deletion_sweeper,  # noqa: ANN001
        settings: Settings,
        clock: Clock,
        events: EventRepo,
        assets=None,  # noqa: ANN001 - AssetRepo; only needed for the periodic stale-upload sweep
    ) -> None:
        self._runtime = runtime
        self._reconciler = reconciler
        self._backfill = backfill
        self._album_mirror = album_mirror
        self._deletion_sweeper = deletion_sweeper
        self._settings = settings
        self._clock = clock
        self._events = events
        self._assets = assets
        self._next_reconcile: datetime | None = None
        self._paused_at: datetime | None = None

    def iterate(self) -> dict[str, Any]:
        now = self._clock.now()
        ran: dict[str, Any] = {"reconciled": False, "resumed": False}

        # A halted runtime would otherwise stay halted forever. Retry periodically:
        # if the credentials are still bad the next asset pauses it again, which
        # costs one request per retry window.
        paused = getattr(self._runtime, "paused_reason", None)
        if paused:
            if self._paused_at is None:
                self._paused_at = now
            elif now - self._paused_at >= PAUSE_RETRY_AFTER:
                self._runtime.resume()
                self._paused_at = None
                ran["resumed"] = True
                self._events.add("info", "retrying transfer after pause")
        else:
            self._paused_at = None

        # Was called bare, unlike every other step below -- the one gap in
        # this method's own "one failing loop must not kill the rest"
        # guarantee. A raise here (a settings save racing an in-flight tick,
        # an Immich failure inside the album allowlist resolution, or a
        # sqlite error from mark_ineligible/media_key_for_checksum, both of
        # which sit outside Worker.process's own try block) used to abort
        # this whole iterate() call, skipping reconcile, stale-upload
        # recovery, album sync, deletions and backfill for that pass.
        self._safely("tick", self._runtime.tick)

        if self._next_reconcile is None or now >= self._next_reconcile:
            self._safely("reconcile", self._reconciler.run_once)
            if self._assets is not None:
                self._safely(
                    "stale upload requeue",
                    self._assets.requeue_stale_uploading,
                    STALE_UPLOAD_AGE,
                )
            self._next_reconcile = now + self._settings.reconcile_interval
            ran["reconciled"] = True

            if self._settings.albums_enabled:
                self._safely("album sync", self._album_mirror.sync_once)

            if self._settings.deletions_enabled:
                self._sweep_deletions()

        if self._backfill.is_running():
            self._safely("backfill", self._backfill.run_slice)

        return ran

    def _sweep_deletions(self) -> None:
        try:
            plan = self._deletion_sweeper.plan()
        except Exception as exc:  # noqa: BLE001
            self._events.add("error", f"deletion planning failed: {exc}")
            return
        if plan.blocked:
            self._events.add(
                "error",
                f"deletion refused by the safety limit: {plan.reason}. "
                "Review the trashed assets in Immich, then re-enable if intended.",
            )
            return
        if plan.checksums:
            self._safely("deletion", self._deletion_sweeper.execute, plan)

    def _safely(self, label: str, fn, *args) -> None:  # noqa: ANN001
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - one failing loop must not kill the rest
            self._events.add("error", f"{label} failed: {exc}")

    def run_forever(self, stop: threading.Event, sleep=time.sleep) -> None:  # noqa: ANN001
        while not stop.is_set():
            try:
                self.iterate()
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
                # This is the worst failure shape in the project: if this thread
                # dies, uvicorn keeps serving and the dashboard keeps rendering,
                # but no backups happen and nothing looks wrong. Log through the
                # redacting logger so it reaches the container's logs, and keep
                # looping.
                logger.exception("background loop iteration failed")
            sleep(IDLE_SLEEP_SECONDS)


class LoopsHandle:
    """A mutable pointer to the currently active `BackgroundLoops`.

    The background thread is started once, on this handle's `run_forever`,
    for the life of the process. It never holds a `BackgroundLoops` directly;
    it re-reads `self.current` on every pass. That is what lets a
    configuration change (the wizard completing, or a settings PUT) swap in
    a freshly built loop graph — new Immich/Google clients, new `Settings` —
    via `replace()`, without restarting this thread or the process.

    `BackgroundLoops.run_forever` already exists and is directly tested; this
    class is a thin indirection in front of it; it does not replace it.
    """

    def __init__(self, loops: BackgroundLoops) -> None:
        self.current = loops

    def replace(self, loops: BackgroundLoops) -> None:
        self.current = loops

    def run_forever(self, stop: threading.Event, sleep=time.sleep) -> None:  # noqa: ANN001
        while not stop.is_set():
            try:
                self.current.iterate()
            except Exception:  # noqa: BLE001 - one bad pass must not kill the loop
                logger.exception("background loop iteration failed")
            sleep(IDLE_SLEEP_SECONDS)
