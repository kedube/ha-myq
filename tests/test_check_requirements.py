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
    ("requires_python", "series"),
    [(">=3.14.2", ">=3.14.2 <3.15"), (">=3.13", ">=3.13 <3.14"), (">=3.9.1,<4", ">=3.9.1 <3.10")],
)
def test_python_series_admits_any_patch_from_the_floor(requires_python, series):
    """Runner images carry only some patch builds, so CI asks for the series."""
    assert check_requirements.python_series(requires_python) == series


def test_python_series_needs_a_minor_version():
    with pytest.raises(ValueError):
        check_requirements.python_series(">=3")


@pytest.mark.parametrize(
    ("latest", "expected"),
    [("2099.1.0", [MINIMUM, "2099.1.0"]), (MINIMUM, [MINIMUM])],
)
def test_defaults_are_the_hacs_minimum_and_the_latest_release(monkeypatch, latest, expected):
    def release_info(version=None):
        return {"version": latest}

    monkeypatch.setattr(check_requirements, "release_info", release_info)
    assert check_requirements.default_versions() == expected


def test_release_inputs_read_the_minimum_python_and_constraints(monkeypatch):
    seen = {}

    def release_info(version=None):
        seen["release"] = version
        return {"requires_python": ">=3.14.2"}

    def fetch(url):
        seen["url"] = url
        return b"av==19.0.0\n"

    monkeypatch.setattr(check_requirements, "release_info", release_info)
    monkeypatch.setattr(check_requirements, "fetch", fetch)
    assert check_requirements.release_inputs("2026.11.2") == ("3.14.2", "av==19.0.0\n")
    assert seen["release"] == "2026.11.2"
    assert seen["url"].endswith("/core/2026.11.2/homeassistant/package_constraints.txt")


@pytest.mark.parametrize("returncode", [0, 1])
@pytest.mark.parametrize("platform", check_requirements.PLATFORMS)
def test_check_resolves_like_home_assistant(monkeypatch, returncode, platform):
    """uv gets the manifest's requirements, HA's constraints, its minimum Python and musl."""
    seen = {}

    def run(args, **kwargs):
        # The temporary files are gone once check() returns, so read them now.
        seen["args"] = args
        seen["requirements"] = Path(args[3]).read_text().split()
        seen["constraints"] = Path(args[args.index("--constraint") + 1]).read_text()
        return subprocess.CompletedProcess(args, returncode, "aiortc==1.16.0\n", "why not\n")

    monkeypatch.setattr(check_requirements.subprocess, "run", run)

    ok, output = check_requirements.check(
        check_requirements.requirements(), "3.14.2", "av==19.0.0\n", platform
    )

    assert ok is (returncode == 0)
    assert seen["args"][:3] == ["uv", "pip", "compile"]
    assert seen["requirements"] == MANIFEST_REQUIREMENTS
    assert seen["constraints"] == "av==19.0.0\n"
    args = seen["args"]
    assert args[args.index("--python-version") + 1] == "3.14.2"
    assert args[args.index("--python-platform") + 1] == platform
    assert "Python 3.14.2" in output and "why not" in output


def release_inputs(version):
    """Offline stand-in: each version's constraints name the version they came from."""
    if version == "2099.1.0":
        raise urllib.error.HTTPError("url", 404, "Not Found", None, None)  # type: ignore[arg-type]
    return "3.14.2", version


def test_main_fails_and_annotates_when_a_version_cannot_install(monkeypatch, capsys):
    def check(wanted, python, constraints, platform):
        if constraints == "2026.7.0":
            return False, "av==17.0.1 is pinned"
        return True, "aiortc==1.16.0"

    monkeypatch.setattr(check_requirements, "release_inputs", release_inputs)
    monkeypatch.setattr(check_requirements, "check", check)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")

    assert check_requirements.main(["2026.6.0", "2026.7.0", "2099.1.0"]) == 1
    out = capsys.readouterr().out
    assert "Home Assistant 2026.6.0 (" in out and " resolves" in out
    assert "CANNOT be resolved" in out
    assert "av==17.0.1 is pinned" in out
    assert "could not be checked" in out
    for platform in check_requirements.PLATFORMS:
        unresolved = f"::error title=Home Assistant 2026.7.0 ({platform})::Could not resolve "
        unreadable = f"::error title=Home Assistant 2099.1.0 ({platform})::Could not read "
        assert unresolved in out and unreadable in out
    # Only a real conflict is described as one; unreadable metadata is not.
    errors = [line for line in out.splitlines() if line.startswith("::error")]
    assert all(("500 error" in line) == ("2026.7.0" in line) for line in errors)
    assert "::error title=Home Assistant 2026.6.0" not in out


def test_each_version_is_read_once_for_every_platform(monkeypatch):
    reads, resolved = [], []

    def count(version):
        reads.append(version)
        return release_inputs(version)

    def check(wanted, python, constraints, platform):
        resolved.append((constraints, platform))
        return True, "ok"

    monkeypatch.setattr(check_requirements, "release_inputs", count)
    monkeypatch.setattr(check_requirements, "check", check)
    assert check_requirements.main(["2026.6.0", "2026.7.0"]) == 0
    assert reads == ["2026.6.0", "2026.7.0"]
    assert resolved == [
        (version, platform)
        for version in ("2026.6.0", "2026.7.0")
        for platform in check_requirements.PLATFORMS
    ]


def test_main_passes_when_every_version_installs(monkeypatch, capsys):
    monkeypatch.setattr(check_requirements, "release_inputs", release_inputs)
    monkeypatch.setattr(check_requirements, "check", lambda *args: (True, "ok"))
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert check_requirements.main(["2026.11.2"]) == 0
    assert "::error" not in capsys.readouterr().out


def test_arm_resolution_failure_fails_even_when_x86_passes(monkeypatch):
    seen = []

    def check(wanted, python, constraints, platform):
        seen.append(platform)
        return platform.startswith("x86_64"), "synthetic resolution"

    monkeypatch.setattr(check_requirements, "release_inputs", release_inputs)
    monkeypatch.setattr(check_requirements, "check", check)
    assert check_requirements.main([MINIMUM]) == 1
    assert seen == list(check_requirements.PLATFORMS)
    seen.clear()
    assert check_requirements.main([MINIMUM, "--platform", "x86_64-unknown-linux-musl"]) == 0
    assert seen == ["x86_64-unknown-linux-musl"]


def published(monkeypatch, *, latest, fixtures, images):
    """Offline PyPI and ghcr.io: HA's latest release, fixture releases and image tags."""
    pythons = {"2099.1.1": ">=3.14.1", "2099.1.2": ">=3.14.2"}

    def release_info(version=None):
        version = version or latest
        return {"version": version, "requires_python": pythons.get(version, ">=3.14.2")}

    monkeypatch.setattr(check_requirements, "release_info", release_info)
    monkeypatch.setattr(check_requirements, "fixture_releases", lambda: fixtures)
    monkeypatch.setattr(check_requirements, "image_token", lambda: "anonymous")
    monkeypatch.setattr(
        check_requirements, "image_published", lambda version, token: version in images
    )


def test_workflow_pins_one_current_release_and_python(monkeypatch, tmp_path, capsys):
    published(
        monkeypatch,
        latest="2099.1.2",
        fixtures=["2099.1.2", "2099.1.1"],
        images={"2099.1.2", "2099.1.1"},
    )
    output = tmp_path / "outputs"
    assert check_requirements.main(["--github-output", str(output)]) == 0
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert json.loads(values["versions"]) == [MINIMUM, "2099.1.2"]
    assert values["current"] == "2099.1.2"
    assert values["python"] == ">=3.14.2 <3.15"
    assert "::notice" not in capsys.readouterr().out


@pytest.mark.parametrize(
    ("fixtures", "images"),
    [
        (["2099.1.1", "2099.1.0"], {"2099.1.2", "2099.1.1"}),  # Fixtures not published yet.
        (["2099.1.2", "2099.1.1"], {"2099.1.1"}),  # Container image not published yet.
    ],
)
def test_workflow_tests_the_newest_release_with_fixtures_and_image(
    monkeypatch, capsys, fixtures, images
):
    published(monkeypatch, latest="2099.1.2", fixtures=fixtures, images=images)
    values = check_requirements.workflow_versions()
    assert values["current"] == "2099.1.1"
    assert json.loads(values["versions"]) == [MINIMUM, "2099.1.1"]
    assert values["python"] == ">=3.14.1 <3.15"
    out = capsys.readouterr().out
    assert "::notice title=Home Assistant 2099.1.2::" in out and "tests 2099.1.1" in out


def test_workflow_ignores_fixtures_newer_than_home_assistant(monkeypatch):
    published(
        monkeypatch, latest="2099.1.1", fixtures=["2099.1.2", "2099.1.1"], images={"2099.1.2"}
    )
    with pytest.raises(ValueError):
        check_requirements.workflow_versions()


@pytest.mark.parametrize("version", ["2099.1.0b1", "2099.1.2\ninjected=value"])
def test_workflow_rejects_nonstable_or_multiline_metadata(monkeypatch, version):
    published(monkeypatch, latest=version, fixtures=[version], images={version})
    with pytest.raises(ValueError):
        check_requirements.workflow_versions()


def test_fixture_releases_are_stable_unyanked_and_newest_first(monkeypatch):
    releases = {
        "2026.11.2": [{"yanked": False}],
        "2026.11.10": [{"yanked": False}],
        "2026.11.3": [{"yanked": True}],
        "2026.12.0b1": [{"yanked": False}],
        "2026.10.0": [],
    }
    monkeypatch.setattr(
        check_requirements, "fetch", lambda url: json.dumps({"releases": releases}).encode()
    )
    assert check_requirements.fixture_releases() == ["2026.11.10", "2026.11.2"]


@pytest.mark.parametrize(("status", "expected"), [(None, True), (404, False)])
def test_image_published_reads_the_tag_manifest(monkeypatch, status, expected):
    seen = {}

    def fetch(url, *, method="GET", headers=None):
        seen.update(url=url, method=method, headers=headers)
        if status:
            raise urllib.error.HTTPError(url, status, "Not Found", None, None)  # type: ignore[arg-type]
        return b""

    monkeypatch.setattr(check_requirements, "fetch", fetch)
    assert check_requirements.image_published("2026.11.3", "anonymous") is expected
    assert seen["url"].endswith("/home-assistant/home-assistant/manifests/2026.11.3")
    assert seen["method"] == "HEAD"
    assert seen["headers"]["Authorization"] == "Bearer anonymous"


def test_image_lookup_errors_other_than_missing_are_raised(monkeypatch):
    def fetch(url, *, method="GET", headers=None):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)  # type: ignore[arg-type]

    monkeypatch.setattr(check_requirements, "fetch", fetch)
    with pytest.raises(urllib.error.HTTPError):
        check_requirements.image_published("2026.11.3", "anonymous")
