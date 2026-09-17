"""Guards on the promises the README and image make."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_states_the_risk_above_the_fold():
    head = ROOT.joinpath("README.md").read_text().split("\n")[:40]
    text = " ".join(head).lower()
    assert "reverse-engineered" in text
    assert "not affiliated" in text


def test_readme_documents_the_optional_read_only_library_mount():
    compose = ROOT.joinpath("docker-compose.yml").read_text()
    assert ":ro" in compose
    assert "IGP_DATA_DIR" in compose


def test_dockerfile_builds_for_both_architectures():
    dockerfile = ROOT.joinpath("docker/Dockerfile").read_text()
    assert "python:3.12" in dockerfile
    assert "immich_gphotos" in dockerfile


def test_license_is_agpl():
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in ROOT.joinpath("LICENSE").read_text()
