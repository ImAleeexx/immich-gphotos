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
    for field in ("updatedAfter", "page", "size", "withExif", "withDeleted", "visibility"):
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
