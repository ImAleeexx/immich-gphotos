import uuid
from dataclasses import dataclass
from pathlib import Path

from immich_gphotos.immich.protocol import ImmichClient
from immich_gphotos.models import Asset


@dataclass(frozen=True)
class ResolvedBytes:
    path: Path
    temporary: bool


class ByteResolver:
    """Get at an asset's bytes by the cheapest route available.

    When the container can see Immich's library volume, `originalPath` is handed
    straight to gpmc: no copy, no scratch space, no traffic through the API.
    Otherwise the original is downloaded to scratch and deleted afterwards.
    """

    def __init__(self, immich: ImmichClient, scratch: Path, allow_direct: bool = True) -> None:
        self._immich = immich
        self._scratch = Path(scratch)
        self._allow_direct = allow_direct

    def resolve(self, asset: Asset) -> ResolvedBytes:
        if self._allow_direct and asset.original_path:
            candidate = Path(asset.original_path)
            if candidate.is_file():
                return ResolvedBytes(path=candidate, temporary=False)
        self._scratch.mkdir(parents=True, exist_ok=True)
        # The nonce guarantees a fresh path per call: two concurrent resolutions of
        # the *same* asset (e.g. a slow-but-healthy upload that crosses
        # requeue_stale_uploading's liveness-free age threshold and gets handed to a
        # second worker) must never share a scratch file, or one caller's release()
        # could delete bytes the other is still writing or uploading. Only the
        # basename of the filename is used: `originalFileName` comes from Immich
        # unsanitized, and a separator in it must not turn into a subdirectory.
        nonce = uuid.uuid4().hex
        dest = self._scratch / f"{asset.immich_id}-{nonce}-{Path(asset.filename).name}"
        self._immich.download_original(asset.immich_id, dest)
        return ResolvedBytes(path=dest, temporary=True)

    def release(self, resolved: ResolvedBytes) -> None:
        if resolved.temporary:
            resolved.path.unlink(missing_ok=True)
