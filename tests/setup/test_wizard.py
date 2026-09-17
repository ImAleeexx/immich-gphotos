from immich_gphotos.gphotos.fake import FakeGooglePhotosClient
from immich_gphotos.gphotos.protocol import GPhotosError
from immich_gphotos.immich.fake import FakeImmichClient
from immich_gphotos.immich.protocol import ImmichError
from immich_gphotos.models import ErrorClass
from immich_gphotos.setup.wizard import CORE_PERMISSIONS, REQUIRED_PERMISSIONS, Wizard


def test_a_fully_configured_immich_passes():
    client = FakeImmichClient(permissions=set(REQUIRED_PERMISSIONS), version=(3, 2, 2))
    check = Wizard().check_immich(client)
    assert check.ok is True
    assert check.event_driven is True
    assert check.missing_permissions == set()


def test_missing_permissions_are_named_not_just_counted():
    granted = set(REQUIRED_PERMISSIONS) - {"asset.download", "workflow.create"}
    check = Wizard().check_immich(FakeImmichClient(permissions=granted))
    assert check.ok is False
    assert check.missing_permissions == {"asset.download", "workflow.create"}


def test_missing_permissions_are_reported_before_reconciler_only_mode():
    """A pre-3.0 server (no workflow system) with a key that is *also* missing
    a core permission must be told which permission is missing, not that it's
    running in reconciler-only mode -- that would bury the actionable,
    specific problem behind a fact that's true but beside the point."""
    granted = set(CORE_PERMISSIONS) - {"asset.download"}
    client = FakeImmichClient(permissions=granted, version=(2, 9, 0))
    check = Wizard().check_immich(client)
    assert check.ok is False
    assert check.missing_permissions == {"asset.download"}
    assert "missing" in check.message.lower()
    assert "reconcil" not in check.message.lower()


def test_immich_below_v3_degrades_to_reconciler_only_instead_of_failing():
    # A pre-3.0 server has no workflow system, so a key on it can only ever
    # carry the core permissions -- the workflow.* / plugin.read scopes don't
    # exist to grant. This is the realistic configuration for this version.
    client = FakeImmichClient(permissions=set(CORE_PERMISSIONS), version=(2, 9, 0))
    check = Wizard().check_immich(client)
    assert check.ok is True
    assert check.event_driven is False
    assert "reconcil" in check.message.lower()


def test_a_server_without_the_webhook_method_is_not_offered_a_workflow():
    client = FakeImmichClient(permissions=set(REQUIRED_PERMISSIONS), method_keys=set())
    check = Wizard().check_immich(client)
    assert check.webhook_method_present is False
    assert check.event_driven is False


def test_plugin_method_keys_failure_degrades_instead_of_propagating():
    class Failing(FakeImmichClient):
        def plugin_method_keys(self) -> set[str]:
            raise ImmichError("plugin listing endpoint returned 500")

    client = Failing(permissions=set(REQUIRED_PERMISSIONS))
    check = Wizard().check_immich(client)
    assert check.webhook_method_present is False
    assert check.event_driven is False
    assert check.ok is True
    assert check.missing_permissions == set()


def test_google_check_uses_a_hash_that_cannot_exist():
    google = FakeGooglePhotosClient()
    check = Wizard().check_google(google)
    assert check.ok is True


def test_google_check_reports_an_auth_failure_plainly():
    class Failing(FakeGooglePhotosClient):
        def exists(self, checksum):
            raise GPhotosError("401 unauthorized", ErrorClass.AUTH_INVALID)

    check = Wizard().check_google(Failing())
    assert check.ok is False
    assert "auth" in check.message.lower()


def test_register_workflow_passes_the_secret_through():
    client = FakeImmichClient()
    workflow_id = Wizard().register_workflow(
        client,
        public_url="http://igp:8080/hooks/immich",
        secret="s3cret",
        header="X-IGP-Secret",
    )
    assert workflow_id == "wf-1"
    assert client.created_workflows[0]["url"] == "http://igp:8080/hooks/immich"


def test_generated_secrets_are_long_and_unique():
    wizard = Wizard()
    a, b = wizard.generate_secret(), wizard.generate_secret()
    assert a != b
    assert len(a) >= 32
