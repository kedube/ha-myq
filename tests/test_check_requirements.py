"""scripts/check_requirements.py resolves the manifest's requirements as Home Assistant would.

These tests stay offline: PyPI, GitHub and uv are replaced. The Validate workflow runs the
script for real.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "check_requirements", ROOT / "scripts" / "check_requirements.py"
)
assert _spec is not None and _spec.loader is not None
check_requirements = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_requirements)

MINIMUM = json.loads((ROOT / "hacs.json").read_text())["homeassistant"]
MANIFEST_REQUIREMENTS = json.loads((ROOT / "custom_components/myq/manifest.json").read_text())[
    "requirements"
]


@pytest.mark.parametrize(
    ("requires_python", "floor"),
    [(">=3.14.2", "3.14.2"), (">=3.13", "3.13"), (">=3.13.2,<3.15", "3.13.2")],
)
def test_python_floor(requires_python, floor):
    assert check_requirements.python_floor(requires_python) == floor


def test_python_floor_needs_a_minimum():
    with pytest.raises(ValueError):
        check_requirements.python_floor("<4")


@pytest.mark.parametrize(
    ("latest", "expected"),
    [("2099.1.0", [MINIMUM, "2099.1.0"]), (MINIMUM, [MINIMUM])],
)
def test_defaults_are_the_hacs_minimum_and_the_latest_release(monkeypatch, latest, expected):
    def release_info(version=None):
        return {"version": latest}

    monkeypatch.setattr(check_requirements, "release_info", release_info)
    assert check_requirements.default_versions() == expected


@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize("platform", check_requirements.PLATFORMS)
def test_check_resolves_like_home_assistant(monkeypatch, returncode, platform):
    """uv gets the manifest's requirements, HA's constraints, its minimum Python and musl."""
    seen = {}

    def release_info(version=None):
        seen["release"] = version
        return {"requires_python": ">=3.14.2"}

    def fetch(url):
        seen["url"] = url
        return b"av==19.0.0\n"

    def run(args, **kwargs):
        # The temporary files are gone once check() returns, so read them now.
        seen["args"] = args
        seen["requirements"] = Path(args[3]).read_text().split()
        seen["constraints"] = Path(args[args.index("--constraint") + 1]).read_text()
        return subprocess.CompletedProcess(args, returncode, "aiortc==1.16.0\n", "why not\n")

    monkeypatch.setattr(check_requirements, "release_info", release_info)
    monkeypatch.setattr(check_requirements, "fetch", fetch)
    monkeypatch.setattr(check_requirements.subprocess, "run", run)

    ok, output = check_requirements.check("2026.11.2", check_requirements.requirements(), platform)

    assert ok is (returncode == 0)
    assert seen["release"] == "2026.11.2"
    assert seen["url"].endswith("/core/2026.11.2/homeassistant/package_constraints.txt")
    assert seen["args"][:3] == ["uv", "pip", "compile"]
    assert seen["requirements"] == MANIFEST_REQUIREMENTS
    assert seen["constraints"] == "av==19.0.0\n"
    args = seen["args"]
    assert args[args.index("--python-version") + 1] == "3.14.2"
    assert args[args.index("--python-platform") + 1] == platform
    assert "Python 3.14.2" in output and "why not" in output


def test_main_fails_and_annotates_when_a_version_cannot_install(monkeypatch, capsys):
    def check(version, wanted, platform):
        if version == "2026.7.0":
            return False, "av==17.0.1 is pinned"
        if version == "2099.1.0":
            raise urllib.error.HTTPError("url", 404, "Not Found", None, None)  # type: ignore[arg-type]
        return True, "aiortc==1.16.0"

    monkeypatch.setattr(check_requirements, "check", check)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert check_requirements.main(["2026.6.0", "2026.7.0", "2099.1.0"]) == 1
    out = capsys.readouterr().out
    assert "Home Assistant 2026.6.0 (" in out and " resolves" in out
    assert "CANNOT be resolved" in out
    assert "av==17.0.1 is pinned" in out
    assert "could not be checked" in out
    for platform in check_requirements.PLATFORMS:
        assert f"::error title=Home Assistant 2026.7.0 ({platform})::" in out
        assert f"::error title=Home Assistant 2099.1.0 ({platform})::" in out
    assert "::error title=Home Assistant 2026.6.0" not in out


def test_main_passes_when_every_version_installs(monkeypatch, capsys):
    monkeypatch.setattr(check_requirements, "check", lambda version, wanted, platform: (True, "ok"))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert check_requirements.main(["2026.11.2"]) == 0
    assert "::error" not in capsys.readouterr().out


def test_arm_resolution_failure_fails_even_when_x86_passes(monkeypatch):
    seen = []

    def check(version, wanted, platform):
        seen.append(platform)
        return platform.startswith("x86_64"), "synthetic resolution"

    monkeypatch.setattr(check_requirements, "check", check)
    assert check_requirements.main([MINIMUM]) == 1
    assert seen == list(check_requirements.PLATFORMS)
    seen.clear()
    assert check_requirements.main([MINIMUM, "--platform", "x86_64-unknown-linux-musl"]) == 0
    assert seen == ["x86_64-unknown-linux-musl"]


def test_workflow_pins_one_current_release_and_python(monkeypatch, tmp_path):
    monkeypatch.setattr(
        check_requirements,
        "release_info",
        lambda: {"version": "2099.1.2", "requires_python": ">=3.14.2"},
    )
    output = tmp_path / "outputs"
    assert check_requirements.main(["--github-output", str(output)]) == 0
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(values["versions"]) == [MINIMUM, "2099.1.2"]
    assert values["current"] == "2099.1.2"
    assert values["python"] == "3.14.2"


@pytest.mark.parametrize("version", ["2099.1.0b1", "2099.1.2\ninjected=value"])
def test_workflow_rejects_nonstable_or_multiline_metadata(monkeypatch, version):
    monkeypatch.setattr(
        check_requirements,
        "release_info",
        lambda: {"version": version, "requires_python": ">=3.14.2"},
    )
    with pytest.raises(ValueError):
        check_requirements.workflow_versions()
