import secrets
from dataclasses import dataclass, field

from immich_gphotos.gphotos.protocol import GooglePhotosClient, GPhotosError
from immich_gphotos.immich.protocol import ImmichClient, ImmichError

# Required on every Immich server, regardless of version: the workflow system
# doesn't exist below 3.0, so its permission scopes can't be granted there either.
CORE_PERMISSIONS = frozenset(
    {
        "asset.read",
        "asset.download",
        "album.read",
        "album.create",
        "albumAsset.create",
    }
)

# Only required when the server supports workflows (version >= 3.0).
WORKFLOW_PERMISSIONS = frozenset(
    {
        "workflow.create",
        "workflow.read",
        "workflow.update",
        "workflow.delete",
        "workflow.logs",
        "plugin.read",
    }
)

# Exported as the union for contract tests that check these names against
# Immich's published permission enum. check_immich only requires the
# workflow subset when the server actually supports workflows.
REQUIRED_PERMISSIONS = CORE_PERMISSIONS | WORKFLOW_PERMISSIONS

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

        supports_workflows = version >= (3, 0, 0)
        required = CORE_PERMISSIONS | WORKFLOW_PERMISSIONS if supports_workflows else CORE_PERMISSIONS
        missing = required - granted

        webhook_present = False
        if supports_workflows:
            try:
                webhook_present = WEBHOOK_METHOD in client.plugin_method_keys()
            except ImmichError:
                webhook_present = False

        workflow_permissions_present = not (WORKFLOW_PERMISSIONS - granted)
        event_driven = supports_workflows and webhook_present and workflow_permissions_present
        # missing is checked first: a broken API key is the more actionable,
        # more specific problem, and naming it must never be preempted by the
        # (also true, but secondary) fact that this server predates workflows.
        if missing:
            message = "The API key is missing required permissions."
        elif not supports_workflows:
            message = (
                f"Immich {version[0]}.{version[1]} has no workflow system; running in reconciler-only mode."
            )
        elif not webhook_present:
            message = (
                "This server does not expose immich-plugin-core#webhook; running in reconciler-only mode."
            )
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
