"""Static checks of the project scaffold: packaging, pins, Makefile, CI and releases."""

import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

import dyness_b3_bms_mqtt
from tests.support.makefile import recipe

ROOT = Path(__file__).resolve().parents[2]


def _pyproject() -> dict[str, Any]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def _requirement_name(spec: str) -> str:
    match = re.match(r"[A-Za-z0-9._-]+", spec)
    assert match is not None, spec
    return match.group(0).lower().replace("_", "-")


@pytest.mark.req("INST-11")
def test_python_is_pinned_to_314() -> None:
    assert (ROOT / ".python-version").read_text().strip() == "3.14"
    assert _pyproject()["project"]["requires-python"] == ">=3.14"
    lock = ROOT / "uv.lock"
    assert lock.is_file()
    assert 'requires-python = ">=3.14"' in lock.read_text()
    assert sys.version_info[:2] == (3, 14)


@pytest.mark.req("INST-13")
def test_runtime_dependencies_are_exact() -> None:
    project = _pyproject()["project"]
    assert project["dependencies"] == ["python-can>=4.5,<5", "paho-mqtt>=2,<3"]
    assert project["optional-dependencies"] == {"gs-usb": ["gs-usb", "pyusb"]}
    dev = _pyproject()["dependency-groups"]["dev"]
    assert {"pytest", "pytest-timeout", "hypothesis", "ruff", "mypy"} <= {
        _requirement_name(spec) for spec in dev
    }
    every = [*project["dependencies"], *project["optional-dependencies"]["gs-usb"], *dev]
    assert not [spec for spec in every if "notify" in spec.lower() or "systemd" in spec.lower()]


@pytest.mark.req("INST-14")
def test_console_script_and_build_backend() -> None:
    data = _pyproject()
    assert data["project"]["scripts"] == {
        "dyness-b3-bms-mqtt": "dyness_b3_bms_mqtt.app:main",
    }
    assert data["build-system"]["build-backend"] == "uv_build"
    assert [_requirement_name(spec) for spec in data["build-system"]["requires"]] == ["uv-build"]


@pytest.mark.req("INST-17")
def test_version_is_single_sourced() -> None:
    source = (ROOT / "src" / "dyness_b3_bms_mqtt" / "__init__.py").read_text()
    assert not re.search(r"\d+\.\d+\.\d+", source)
    assert 'version("dyness-b3-bms-mqtt")' in source
    assert dyness_b3_bms_mqtt.__version__ == _pyproject()["project"]["version"]


@pytest.mark.req("INST-17")
def test_release_please_config() -> None:
    """Pin the release-please config, manifest and workflow.

    The manifest records the last released version. Until the first release it is
    ``0.0.0`` while ``pyproject.toml`` says ``0.1.0``: release-please treats a manifest
    version as already released, so ``0.1.0`` there would make the first ``feat`` release
    ``0.2.0``. From ``0.0.0`` the first release PR proposes ``0.1.0``, and from then on
    each release PR moves the manifest and ``pyproject.toml`` together.
    """
    config = json.loads((ROOT / "release-please-config.json").read_text())
    assert config == {
        "packages": {
            ".": {
                "release-type": "python",
                "package-name": "dyness-b3-bms-mqtt",
                "changelog-path": "CHANGELOG.md",
                "include-v-in-tag": True,
                "bump-minor-pre-major": True,
                "prerelease": True,
                "pull-request-title-pattern": "build${scope}: release${component} ${version}",
                "extra-files": [
                    {
                        "type": "toml",
                        "path": "uv.lock",
                        "jsonpath": "$.package[?(@.name=='dyness-b3-bms-mqtt')].version",
                    },
                    {"type": "generic", "path": "README.md"},
                    {"type": "generic", "path": "docs/specs/10-installation.md"},
                ],
            }
        }
    }
    manifest = json.loads((ROOT / ".release-please-manifest.json").read_text())
    version = _pyproject()["project"]["version"]
    assert manifest in ({".": "0.0.0"}, {".": version})
    if manifest == {".": "0.0.0"}:
        assert version == "0.1.0"

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "release-please.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers == {"push": {"branches": ["main"]}}
    assert workflow["permissions"] == {"contents": "write", "pull-requests": "write"}
    steps = [step for job in workflow["jobs"].values() for step in job["steps"]]
    action = next(step for step in steps if "release-please-action" in step.get("uses", ""))
    assert action["uses"] == "googleapis/release-please-action@v4"
    assert action["with"] == {
        "config-file": "release-please-config.json",
        "manifest-file": ".release-please-manifest.json",
    }


@pytest.mark.req("INST-18")
def test_quick_start_version_is_bumped_by_release_please() -> None:
    """The quick start names the current release, and release-please keeps it that way.

    Each file carries exactly two annotated lines, the download and the ``cd`` into the
    unpacked archive, and every version on them is the one in ``pyproject.toml``.
    """
    package = json.loads((ROOT / "release-please-config.json").read_text())["packages"]["."]
    assert package["prerelease"] is True
    generic = {entry["path"] for entry in package["extra-files"] if entry["type"] == "generic"}
    assert generic == {"README.md", "docs/specs/10-installation.md"}

    version = _pyproject()["project"]["version"]
    for path in sorted(generic):
        lines = [
            line
            for line in (ROOT / path).read_text().splitlines()
            if "# x-release-please-version" in line
        ]
        assert len(lines) == 2, path
        for line in lines:
            assert re.findall(r"\d+\.\d+\.\d+", line) == [version], line


@pytest.mark.req("TEST-02", "TEST-11")
def test_markers_registered_and_strict(pytestconfig: pytest.Config) -> None:
    ini = _pyproject()["tool"]["pytest"]["ini_options"]
    names = {line.split(":")[0].split("(")[0].strip() for line in ini["markers"]}
    assert names == {"req", "integration", "mosquitto", "hardware"}
    assert ini["addopts"] == ["--strict-markers", "--strict-config", "-ra", "--req-strict"]
    assert ini["timeout"] == 120
    assert float(pytestconfig.getini("timeout")) == 120
    assert ini["testpaths"] == ["tests"]
    assert ini["pythonpath"] == ["."]
    registered = {line.split(":")[0].split("(")[0] for line in pytestconfig.getini("markers")}
    assert names <= registered
    assert pytestconfig.option.strict_markers
    assert pytestconfig.option.strict_config


@pytest.mark.req("TEST-33", "INST-04")
def test_make_check_runs_the_four_gates_in_order() -> None:
    text = (ROOT / "Makefile").read_text()
    lines = text.splitlines()
    assert "SHELL := /bin/bash" in lines
    assert ".SHELLFLAGS := -eu -o pipefail -c" in lines
    assert "CHECKOUT := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))" in lines

    gates = ["ruff check", "ruff format --check", "mypy --strict src tests", "pytest"]
    check = recipe(text, "check")
    assert len(check) == len(gates)
    for line, gate in zip(check, gates, strict=True):
        assert line.endswith(f" run --frozen {gate}"), line
        assert "$(CHECKOUT)" in line
        assert not line.startswith(("-", "@-")), line
        assert "||" not in line

    assert [line for line in recipe(text, "dev") if "sync" in line] == [
        "$(UV) --directory $(CHECKOUT) sync --all-extras"
    ]


@pytest.mark.req("TEST-34", "TEST-35")
def test_ci_workflow() -> None:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for forbidden in ("vcan", "modprobe", "sudo"):
        assert forbidden not in text
    workflow = yaml.safe_load(text)
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["pull_request"] == {"branches": ["main"]}
    jobs = workflow["jobs"]
    assert set(jobs) == {"check", "mqtt"}

    def runs(job: dict[str, Any]) -> list[str]:
        return [step["run"] for step in job["steps"] if "run" in step]

    def setup_uv(job: dict[str, Any]) -> dict[str, Any]:
        found = [s for s in job["steps"] if s.get("uses", "").startswith("astral-sh/setup-uv@")]
        assert len(found) == 1
        step: dict[str, Any] = found[0]
        return step

    check = jobs["check"]
    assert check["runs-on"] == "ubuntu-latest"
    assert check["steps"][0]["uses"].startswith("actions/checkout@")
    assert setup_uv(check)["with"]["python-version"] == "3.14"
    assert runs(check) == ["uv sync --frozen", "make check"]

    mqtt = jobs["mqtt"]
    assert mqtt["runs-on"] == "ubuntu-latest"
    services = list(mqtt["services"].values())
    assert [service["image"] for service in services] == [
        "public.ecr.aws/docker/library/eclipse-mosquitto:1.6"
    ]
    assert services[0]["ports"] == ["1883:1883"]
    assert mqtt["env"] == {"MQTT_HOST": "localhost", "MQTT_PORT": "1883"}
    assert setup_uv(mqtt)["with"]["python-version"] == "3.14"
    assert runs(mqtt) == ["uv sync --frozen", "uv run --frozen pytest --mosquitto -m mosquitto"]

    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    assert compose["services"] == {
        "mosquitto": {"image": services[0]["image"], "ports": ["1883:1883"]}
    }
