"""Contract test: the Immich API surface this project depends on.

Skipped unless IGP_CONTRACT_TESTS=1, so the ordinary suite stays offline.
"""

import os

import httpx
import pytest

SPEC_URL = "https://raw.githubusercontent.com/immich-app/immich/main/open-api/immich-openapi-specs.json"

pytestmark = pytest.mark.skipif(os.environ.get("IGP_CONTRACT_TESTS") != "1", reason="network contract test")


@pytest.fixture(scope="module")
def spec() -> dict:
    return httpx.get(SPEC_URL, timeout=60, follow_redirects=True).json()


@pytest.mark.contract
@pytest.mark.parametrize(
    ("path", "method"),
    [
        ("/server/version", "get"),
        ("/api-keys/me", "get"),
        ("/plugins/methods", "get"),
        ("/search/metadata", "post"),
        ("/assets/{id}/original", "get"),
        ("/albums", "get"),
        ("/albums/{id}", "get"),
        ("/workflows", "post"),
        ("/workflows/{id}/logs", "get"),
    ],
)
def test_endpoints_we_depend_on_still_exist(spec, path, method):
    assert method in spec["paths"][path]


@pytest.mark.contract
def test_metadata_search_fields(spec):
    props = spec["components"]["schemas"]["MetadataSearchDto"]["properties"]
    # "visibility" is deliberately not asserted here: the client never sends
    # it, and upstream marked it deprecated as of Immich v3.2.0, so pinning
    # it would fail this suite the moment Immich drops a field we never
    # depended on in the first place.
    for field in ("updatedAfter", "page", "size", "withExif", "withDeleted"):
        assert field in props


@pytest.mark.contract
def test_asset_response_fields_we_read(spec):
    props = spec["components"]["schemas"]["AssetResponseDto"]["properties"]
    for field in (
        "id",
        "checksum",
        "originalFileName",
        "originalPath",
        "type",
        "updatedAt",
        "visibility",
        "isOffline",
        "isTrashed",
        "tags",
        "exifInfo",
    ):
        assert field in props


@pytest.mark.contract
def test_visibility_and_type_values_we_compare_against_are_still_valid(spec):
    """`test_asset_response_fields_we_read` only proves `visibility` and
    `type` are still present on the DTO -- it says nothing about the actual
    strings those fields carry. `sync.eligibility.SYNCABLE_VISIBILITY` and
    `config.Filters.allowed_types` are string comparisons against Immich's
    enums, and they gate every asset: if Immich ever re-cases or renames a
    value we compare against, every asset would silently become
    `ineligible` -- nothing syncs, no error is raised -- while the two field-
    presence tests above stayed green. Asserting our constants are a subset
    of the live enums is what actually catches that."""
    from immich_gphotos.config import Filters
    from immich_gphotos.sync.eligibility import SYNCABLE_VISIBILITY

    visibility_values = set(spec["components"]["schemas"]["AssetVisibility"]["enum"])
    assert visibility_values >= SYNCABLE_VISIBILITY

    type_values = set(spec["components"]["schemas"]["AssetTypeEnum"]["enum"])
    assert type_values >= Filters().allowed_types


@pytest.mark.contract
def test_search_response_paging_fields(spec):
    props = spec["components"]["schemas"]["SearchAssetResponseDto"]["properties"]
    assert "items" in props
    assert "nextPage" in props


@pytest.mark.contract
def test_workflow_create_shape(spec):
    create = spec["components"]["schemas"]["WorkflowCreateDto"]["properties"]
    assert {"trigger", "steps", "name", "enabled"} <= set(create)
    step = spec["components"]["schemas"]["WorkflowStepDto"]["properties"]
    assert {"method", "config"} <= set(step)


@pytest.mark.contract
def test_api_key_permissions_we_require_still_exist(spec):
    from immich_gphotos.setup.wizard import REQUIRED_PERMISSIONS

    available = set(spec["components"]["schemas"]["Permission"]["enum"])
    assert available >= REQUIRED_PERMISSIONS
