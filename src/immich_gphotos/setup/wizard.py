import secrets
from dataclasses import dataclass, field

from immich_gphotos.gphotos.protocol import GooglePhotosClient, GPhotosError
from immich_gphotos.immich.protocol import ImmichClient, ImmichError

REQUIRED_PERMISSIONS = frozenset(
    {
        "asset.read",
        "asset.download",
        "album.read",
        "album.create",
        "albumAsset.create",
        "workflow.create",
        "workflow.read",
        "workflow.update",
        "workflow.delete",
        "workflow.logs",
        "plugin.read",
    }
)

WEBHOOK_METHOD = "immich-plugin-core#webhook"

# A hash of the right shape that no real file will have.
PROBE_HASH = "AAAAAAAAAAAAAAAAAAAAAAAAAAA="


@dataclass(frozen=True)
class ImmichCheck:
    ok: bool
    version: tuple[int, int, int] | None = None
    event_driven: bool = False
    webhook_method_present: bool = False
    missing_permissions: set[str] = field(default_factory=set)
    message: str = ""


@dataclass(frozen=True)
class GoogleCheck:
    ok: bool
    message: str = ""


class Wizard:
    """Each check proves the thing works rather than merely storing what was typed."""

    def check_immich(self, client: ImmichClient) -> ImmichCheck:
        try:
            version = client.server_version()
            granted = client.key_permissions()
        except ImmichError as exc:
            return ImmichCheck(ok=False, message=str(exc))

        missing = set(REQUIRED_PERMISSIONS) - granted
        supports_workflows = version >= (3, 0, 0)

        webhook_present = False
        if supports_workflows:
            try:
                webhook_present = WEBHOOK_METHOD in client.plugin_method_keys()
            except ImmichError:
                webhook_present = False

        event_driven = supports_workflows and webhook_present
        if not supports_workflows:
            message = (
                f"Immich {version[0]}.{version[1]} has no workflow system; running in reconciler-only mode."
            )
        elif not webhook_present:
            message = (
                "This server does not expose immich-plugin-core#webhook; running in reconciler-only mode."
            )
        elif missing:
            message = "The API key is missing required permissions."
        else:
            message = "Connected."

        return ImmichCheck(
            ok=not missing,
            version=version,
            event_driven=event_driven,
            webhook_method_present=webhook_present,
            missing_permissions=missing,
            message=message,
        )

    def check_google(self, client: GooglePhotosClient) -> GoogleCheck:
        """Prove the credentials work with a lookup for a hash nothing can have."""
        try:
            client.exists(PROBE_HASH)
        except GPhotosError as exc:
            return GoogleCheck(ok=False, message=f"{exc.error_class.value}: {exc}")
        return GoogleCheck(ok=True, message="Authenticated.")

    def register_workflow(self, client: ImmichClient, public_url: str, secret: str, header: str) -> str:
        return client.create_workflow(
            name="Back up to Google Photos",
            url=public_url,
            header_name=header,
            header_value=secret,
        )

    @staticmethod
    def generate_secret() -> str:
        return secrets.token_urlsafe(32)
