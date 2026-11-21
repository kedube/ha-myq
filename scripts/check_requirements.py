#!/usr/bin/env python3
"""Check that Home Assistant can install the manifest's requirements.

Home Assistant installs an integration's requirements with uv, held to the versions in its
own `package_constraints.txt`, before it loads the config flow. A requirement that conflicts
with those pins cannot be installed, and adding the integration fails with "Config flow could
not be loaded: 500 Internal Server Error". Release 0.01 shipped aiortc 1.14.0, which needs
PyAV below 17, while Home Assistant 2026.7 and later pin PyAV 17 and then 19.

The test environment pins one Home Assistant version and ignores those constraints, so it
cannot see this. This script resolves the requirements the way Home Assistant does, against
each version's constraints and minimum Python, for the Alpine (musl) Linux that Home
Assistant OS and the container image run:

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
PLATFORM = "x86_64-unknown-linux-musl"


def _read(request: urllib.request.Request) -> bytes:
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch(url: str) -> bytes:
    """GET `url`, retrying twice on network and server errors."""
    request = urllib.request.Request(url, headers={"User-Agent": "ha-myq-requirements-check"})
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


def check(version: str, wanted: list[str]) -> tuple[bool, str]:
    """Resolve `wanted` as Home Assistant `version` would install it; (ok, uv's output)."""
    python = python_floor(release_info(version)["requires_python"])
    constraints = fetch(CONSTRAINTS.format(version)).decode()
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
                PLATFORM,
                "--no-header",
                "--no-annotate",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    return result.returncode == 0, f"Python {python}\n{result.stdout}{result.stderr}".strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("versions", nargs="*", help="Home Assistant versions (default: see top)")
    versions = parser.parse_args(argv).versions or default_versions()
    wanted = requirements()
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    failed = []
    for version in versions:
        try:
            ok, output = check(version, wanted)
            verdict = "installs" if ok else "CANNOT be installed"
        except urllib.error.URLError as err:
            ok, output = False, f"Could not read this version's metadata: {err}"
            verdict = "could not be checked"
        print(f"Home Assistant {version}: {', '.join(wanted)} {verdict}")
        print("    " + output.replace("\n", "\n    "))
        if not ok:
            failed.append(version)
            if annotate:
                print(
                    f"::error title=Home Assistant {version}::Could not show that it can install"
                    f" {', '.join(wanted)}. When it can't, adding the integration fails with a"
                    " 500 error. This job's log has the reason."
                )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
