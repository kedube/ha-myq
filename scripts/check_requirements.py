#!/usr/bin/env python3
"""Check dependency resolution under Home Assistant's package constraints.

Home Assistant installs an integration's requirements with uv, held to the versions in its
own `package_constraints.txt`, before it loads the config flow. A requirement that conflicts
with those pins cannot be installed, and adding the integration fails with "Config flow could
not be loaded: 500 Internal Server Error". Release 0.01 shipped aiortc 1.14.0, which needs
PyAV below 17, while Home Assistant 2026.7 and later pin PyAV 17 and then 19.

The test environment pins one Home Assistant version and ignores those constraints, so it
cannot see this. This script resolves the requirements the way Home Assistant does, against
each version's constraints and minimum Python, for x86-64 and ARM64 Alpine (musl)
Linux. The separate check_runtime.py container jobs verify installation and imports:

    check_requirements.py              the minimum in hacs.json and the latest release
    check_requirements.py 2026.10.4    the versions given

It needs network access (PyPI and GitHub) and `uv` on PATH. The Validate workflow runs it on
every push, nightly and before a release.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "custom_components" / "myq" / "manifest.json"
HACS = ROOT / "hacs.json"

CONSTRAINTS = (
    "https://raw.githubusercontent.com/home-assistant/core/{}/homeassistant/package_constraints.txt"
)
PLATFORMS = ("x86_64-unknown-linux-musl", "aarch64-unknown-linux-musl")
STABLE = re.compile(r"\d{4}\.\d{1,2}\.\d+")
# The full suite needs HA's own test fixtures and the runtime jobs its container image,
# both published separately from the PyPI release and sometimes later.
FIXTURES = "pytest-homeassistant"
IMAGE = "home-assistant/home-assistant"
IMAGE_MANIFESTS = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
RECENT_RELEASES = 5  # How far back to look for a release with fixtures and an image.


def _read(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch(url: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> bytes:
    """Request `url`, retrying twice on network and server errors."""
    request = urllib.request.Request(
        url,
        method=method,
        headers={"User-Agent": "ha-myq-requirements-check", **(headers or {})},
    )
    for delay in (2, 5):
        try:
            return _read(request)
        except urllib.error.URLError as err:
            # A missing page (a version that was never released) won't appear on a retry.
            if isinstance(err, urllib.error.HTTPError) and err.code < 500:
                raise
            time.sleep(delay)
    return _read(request)


def requirements() -> list[str]:
    return list(json.loads(MANIFEST.read_text())["requirements"])


def release_info(version: str | None = None) -> dict:
    """PyPI's metadata for a Home Assistant release, or for the latest one."""
    path = f"homeassistant/{version}/json" if version else "homeassistant/json"
    return json.loads(fetch(f"https://pypi.org/pypi/{path}"))["info"]


def default_versions() -> list[str]:
    """The oldest version hacs.json allows and the newest stable release, oldest first."""
    minimum = json.loads(HACS.read_text())["homeassistant"]
    latest = release_info()["version"]
    return [minimum] if latest == minimum else [minimum, latest]


def python_floor(requires_python: str) -> str:
    """The lowest Python a `requires_python` such as ">=3.14.2" admits."""
    if match := re.search(r">=\s*(\d+(?:\.\d+)*)", requires_python):
        return match.group(1)
    raise ValueError(f"No minimum Python in {requires_python!r}")


def python_series(requires_python: str) -> str:
    """A setup-python range for the minimum Python's series, such as ">=3.14.2 <3.15".

    GitHub's runner images carry builds of only some patch releases, which differ
    between Ubuntu versions, so an exact patch can be missing from a newer image.
    """
    floor = python_floor(requires_python)
    parts = floor.split(".")
    if len(parts) < 2:
        raise ValueError(f"No minor Python version in {requires_python!r}")
    return f">={floor} <{parts[0]}.{int(parts[1]) + 1}"


def release_inputs(version: str) -> tuple[str, str]:
    """Home Assistant `version`'s minimum Python and package constraints, for any platform."""
    python = python_floor(release_info(version)["requires_python"])
    return python, fetch(CONSTRAINTS.format(version)).decode()


def check(wanted: list[str], python: str, constraints: str, platform: str) -> tuple[bool, str]:
    """Resolve `wanted` as Home Assistant would install it on `platform`; (ok, uv's output)."""
    with tempfile.TemporaryDirectory() as tmp:
        requirements_in = Path(tmp) / "requirements.in"
        requirements_in.write_text("\n".join(wanted) + "\n")
        constraints_txt = Path(tmp) / "package_constraints.txt"
        constraints_txt.write_text(constraints)
        result = subprocess.run(
            [
                "uv",
                "pip",
                "compile",
                str(requirements_in),
                "--constraint",
                str(constraints_txt),
                "--python-version",
                python,
                "--python-platform",
                platform,
                "--no-header",
                "--no-annotate",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    return result.returncode == 0, f"Python {python}\n{result.stdout}{result.stderr}".strip()


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _stable(version: str) -> str:
    if not STABLE.fullmatch(version):
        raise ValueError(f"Expected a stable Home Assistant release, got {version!r}")
    return version


def fixture_releases() -> list[str]:
    """Stable pytest-homeassistant releases with files that are not yanked, newest first."""
    releases = json.loads(fetch(f"https://pypi.org/pypi/{FIXTURES}/json"))["releases"]
    published = [
        version
        for version, files in releases.items()
        if STABLE.fullmatch(version) and any(not file.get("yanked") for file in files)
    ]
    return sorted(published, key=_version_key, reverse=True)


def image_token() -> str:
    """An anonymous pull token for HA's public container image on ghcr.io."""
    url = f"https://ghcr.io/token?scope=repository:{IMAGE}:pull&service=ghcr.io"
    return json.loads(fetch(url))["token"]


def image_published(version: str, token: str) -> bool:
    """Whether ghcr.io has HA's container image for `version` (all architectures)."""
    try:
        fetch(
            f"https://ghcr.io/v2/{IMAGE}/manifests/{version}",
            method="HEAD",
            headers={"Authorization": f"Bearer {token}", "Accept": IMAGE_MANIFESTS},
        )
    except urllib.error.HTTPError as err:
        if err.code == 404:
            return False
        raise
    return True


def current_release() -> dict:
    """PyPI's metadata for the newest stable HA release with its fixtures and image.

    Normally the latest release. While its fixtures or image are still being
    published, an earlier release is tested instead, with a notice saying so.
    """
    latest = release_info()
    newest = _stable(latest["version"])
    token = image_token()
    candidates = [v for v in fixture_releases() if _version_key(v) <= _version_key(newest)]
    for version in candidates[:RECENT_RELEASES]:
        if image_published(version, token):
            if version == newest:
                return latest
            print(
                f"::notice title=Home Assistant {newest}::Its {FIXTURES} release or"
                f" container image is not published yet, so this run tests {version}."
            )
            return release_info(version)
    raise ValueError(f"No recent Home Assistant release has {FIXTURES} and an image")


def workflow_versions() -> dict[str, str]:
    """Select one stable release for every compatibility job in this workflow run."""
    latest = current_release()
    current = _stable(latest["version"])
    minimum = _stable(json.loads(HACS.read_text())["homeassistant"])
    return {
        "versions": json.dumps(list(dict.fromkeys((minimum, current)))),
        "current": current,
        "python": python_series(latest["requires_python"]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("versions", nargs="*", help="Home Assistant versions (default: see top)")
    parser.add_argument("--platform", action="append", choices=PLATFORMS)
    parser.add_argument(
        "--github-output", type=Path, help="Write workflow versions instead of resolving"
    )
    args = parser.parse_args(argv)
    if args.github_output is not None:
        with args.github_output.open("a") as output:
            for name, value in workflow_versions().items():
                output.write(f"{name}={value}\n")
        return 0
    versions = args.versions or default_versions()
    wanted = requirements()
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    failed = []
    for version in versions:
        # The metadata and constraints are the same for every platform: read them once.
        try:
            inputs: tuple[str, str] | urllib.error.URLError = release_inputs(version)
        except urllib.error.URLError as err:
            inputs = err
        for platform in args.platform or PLATFORMS:
            if not report(version, wanted, platform, inputs, annotate=annotate):
                failed.append((version, platform))
    return 1 if failed else 0


def report(
    version: str,
    wanted: list[str],
    platform: str,
    inputs: tuple[str, str] | urllib.error.URLError,
    *,
    annotate: bool,
) -> bool:
    """Report one resolution without claiming wheels were installed or imported."""
    label = f"Home Assistant {version} ({platform})"
    packages = ", ".join(wanted)
    if isinstance(inputs, urllib.error.URLError):
        ok, output = False, f"Could not read this version's metadata: {inputs}"
        verdict = "could not be checked"
        annotation = (
            "Could not read this release's metadata or package constraints, so its"
            f" requirements were not checked: {packages}. This job's log has the reason."
        )
    else:
        ok, output = check(wanted, *inputs, platform)
        verdict = "resolves" if ok else "CANNOT be resolved"
        annotation = (
            f"Could not resolve {packages} under this release's package constraints."
            " Home Assistant could not install them, and adding the integration would"
            " fail with a 500 error. This job's log has the reason."
        )
    print(f"{label}: {packages} {verdict}")
    print("    " + output.replace("\n", "\n    "))
    if not ok and annotate:
        print(f"::error title={label}::{annotation}")
    return ok


if __name__ == "__main__":
    sys.exit(main())
