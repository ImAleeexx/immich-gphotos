from dataclasses import dataclass, field

from immich_gphotos.config import DeletionPolicy, Settings
from immich_gphotos.gphotos.protocol import GooglePhotosClient
from immich_gphotos.store.assets import AssetRepo


def deletion_allowed(count: int, synced_total: int, policy: DeletionPolicy) -> tuple[bool, str | None]:
    """Refuse implausibly large deletions.

    The scenario guarded against is an Immich database restored from an old
    backup, with this service faithfully removing years of photos from Google.
    Neither bound can be disabled.
    """
    if count == 0:
        return True, None
    if count > policy.max_absolute:
        return False, f"absolute limit exceeded: {count} > {policy.max_absolute}"
    if synced_total and (count / synced_total) > policy.max_fraction:
        return False, (f"fraction limit exceeded: {count}/{synced_total} > {policy.max_fraction:.0%}")
    return True, None


@dataclass(frozen=True)
class DeletionPlan:
    checksums: list[str] = field(default_factory=list)
    asset_ids: list[str] = field(default_factory=list)
    blocked: bool = False
    reason: str | None = None


class DeletionSweeper:
    def __init__(self, gphotos: GooglePhotosClient, assets: AssetRepo, settings: Settings) -> None:
        self._gphotos = gphotos
        self._assets = assets
        self._settings = settings

    def plan(self) -> DeletionPlan:
        if not self._settings.deletions_enabled:
            return DeletionPlan()
        candidates = self._assets.trashed_synced()
        if not candidates:
            return DeletionPlan()
        ok, reason = deletion_allowed(
            count=len(candidates),
            synced_total=self._assets.synced_count(),
            policy=self._settings.deletion_policy,
        )
        return DeletionPlan(
            checksums=[c.asset.checksum for c in candidates],
            asset_ids=[c.asset.immich_id for c in candidates],
            blocked=not ok,
            reason=reason,
        )

    def execute(self, plan: DeletionPlan) -> int:
        if plan.blocked or not plan.checksums:
            return 0
        self._gphotos.trash(plan.checksums)
        self._assets.mark_deleted(plan.asset_ids)
        return len(plan.checksums)
