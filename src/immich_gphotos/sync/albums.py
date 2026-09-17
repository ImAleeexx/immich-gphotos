from dataclasses import dataclass

from immich_gphotos.gphotos.protocol import GooglePhotosClient
from immich_gphotos.immich.protocol import ImmichAlbum, ImmichClient
from immich_gphotos.store.albums import AlbumRepo
from immich_gphotos.store.assets import AssetRepo

GOOGLE_ALBUM_LIMIT = 20_000
ALBUM_BATCH = 500


@dataclass(frozen=True)
class AlbumSyncResult:
    albums: int = 0
    added: int = 0


class AlbumMirror:
    """Reproduces Immich album membership in Google Photos.

    Driven by the reconciler rather than the webhook: AssetCreate fires before an
    asset is placed in an album, and Immich has no "added to album" trigger.
    """

    def __init__(
        self,
        immich: ImmichClient,
        gphotos: GooglePhotosClient,
        albums: AlbumRepo,
        assets: AssetRepo,
    ) -> None:
        self._immich = immich
        self._gphotos = gphotos
        self._albums = albums
        self._assets = assets

    def sync_once(self) -> AlbumSyncResult:
        touched = added = 0
        for album in self._immich.list_albums():
            keys = self._pending_media_keys(album)
            if not keys:
                continue
            touched += 1
            for start in range(0, len(keys), ALBUM_BATCH):
                batch = keys[start : start + ALBUM_BATCH]
                self._push(album, batch)
                added += len(batch)
        return AlbumSyncResult(albums=touched, added=added)

    def _pending_media_keys(self, album: ImmichAlbum) -> list[tuple[str, str]]:
        pending: list[tuple[str, str]] = []
        for asset_id in self._immich.album_asset_ids(album.id):
            if self._albums.is_member_added(album.id, asset_id):
                continue
            stored = self._assets.get(asset_id)
            if stored and stored.media_key:
                pending.append((asset_id, stored.media_key))
        return pending

    def _push(self, album: ImmichAlbum, batch: list[tuple[str, str]]) -> None:
        media_keys = [key for _, key in batch]
        target_key, mapping = self._target(album, len(batch))

        if mapping is None:
            gp_album_id = self._gphotos.create_album(self._album_name(album, target_key), media_keys)
            self._albums.put(
                target_key,
                gp_album_id,
                self._album_name(album, target_key),
                item_count=len(media_keys),
                overflow_of=None if target_key == album.id else album.id,
            )
        else:
            self._gphotos.add_to_album(mapping.gp_album_id, media_keys)
            self._albums.bump(target_key, len(media_keys))

        for asset_id, _ in batch:
            self._albums.mark_added(album.id, asset_id)

    def _target(self, album: ImmichAlbum, adding: int):  # noqa: ANN202
        """Pick the album in the chain with room, or the key for a new overflow."""
        chain = self._albums.chain(album.id)
        if not chain:
            return album.id, None
        last = chain[-1]
        if last.item_count + adding <= GOOGLE_ALBUM_LIMIT:
            return last.immich_album_id, last
        return f"{album.id}#{len(chain) + 1}", None

    @staticmethod
    def _album_name(album: ImmichAlbum, key: str) -> str:
        if "#" not in key:
            return album.name
        return f"{album.name} ({key.rsplit('#', 1)[1]})"
