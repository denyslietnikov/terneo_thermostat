"""Keep development dependencies, HACS and integration metadata aligned."""

import importlib.metadata
import json
import tomllib
from pathlib import Path

import yaml
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def test_supported_home_assistant_version():
    hacs = json.loads((ROOT / "hacs.json").read_text())
    requirements = (ROOT / "requirements-dev.txt").read_text().splitlines()
    assert hacs["homeassistant"] == "2026.9.4"
    assert "homeassistant==2026.9.4" in requirements
    assert importlib.metadata.version("homeassistant") == "2026.9.4"
    assert "iot_class" not in hacs


def test_manifest_matches_maintained_repository_and_dependency_pins():
    manifest = json.loads((ROOT / "custom_components/terneo/manifest.json").read_text())
    requirements = (ROOT / "requirements-dev.txt").read_text().splitlines()
    assert manifest["codeowners"] == ["@denyslietnikov"]
    assert manifest["name"] == "Terneo Thermostat"
    assert (
        manifest["documentation"]
        == "https://github.com/denyslietnikov/terneo_thermostat"
    )
    assert manifest["issue_tracker"] == manifest["documentation"] + "/issues"
    assert all(requirement in requirements for requirement in manifest["requirements"])
    core_requirements = {
        canonicalize_name(Requirement(requirement).name)
        for requirement in importlib.metadata.requires("homeassistant")
    }
    for requirement in manifest["requirements"]:
        package, version = requirement.split("==")
        assert canonicalize_name(package) not in core_requirements
        assert importlib.metadata.version(package) == version
    assert "requests" in core_requirements
    assert "requests==2.34.2" in requirements
    assert importlib.metadata.version("requests") == "2.34.2"


def test_license_and_local_brand_assets_are_present():
    license_text = (ROOT / "LICENSE").read_text()
    assert license_text.startswith("MIT License\n")
    assert "Permission is hereby granted, free of charge" in license_text
    assert 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text
    for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
        with Image.open(ROOT / "custom_components/terneo/brand" / name) as asset:
            assert asset.format == "PNG"
            assert all(dimension > 0 for dimension in asset.size)
            if name.startswith("icon"):
                assert asset.width == asset.height
            asset.verify()


def test_branch_coverage_and_project_local_lint_configuration():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert config["tool"]["coverage"]["run"]["branch"] is True
    assert config["tool"]["coverage"]["report"]["fail_under"] >= 92
    assert config["tool"]["ruff"]["target-version"] == "py314"
    assert config["tool"]["pytest"]["ini_options"]["testpaths"] == ["tests"]


def test_ci_checks_only_supported_release_with_read_only_permissions():
    workflow = yaml.load(
        (ROOT / ".github/workflows/ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert set(workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"lint", "tests", "hassfest", "hacs"}
    requirements = (ROOT / "requirements-dev.txt").read_text().splitlines()
    for job in workflow["jobs"].values():
        assert job["runs-on"] == "ubuntu-24.04"
        for step in job["steps"]:
            if "uses" in step:
                action, sha = step["uses"].rsplit("@", 1)
                assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
                if action == "actions/checkout":
                    assert step["with"]["persist-credentials"] == "false"
                if action == "actions/setup-python":
                    assert step["with"]["python-version"] == "3.14"
                if action == "actions/upload-artifact":
                    assert sha == "b7c566a772e6b6bfb58ed0dc250532a479d7789f"
            if step.get("run", "").startswith("python -m pip install ruff=="):
                assert step["run"].split()[-1] in requirements
    assert workflow["jobs"]["tests"]["name"] == "Home Assistant 2026.9.4"
    assert "matrix" not in workflow["jobs"]["tests"].get("strategy", {})
    assert workflow["jobs"]["hacs"]["steps"][-1]["with"] == {
        "category": "integration",
        "comment": "false",
    }
