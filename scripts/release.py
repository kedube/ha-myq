#!/usr/bin/env python3
"""Version numbers and release bookkeeping, as the Release workflow uses them.

Versions are `MAJOR.NN`, starting at 0.01. A minor release adds one to the two-digit part
and carries into the major after 99 (0.01, 0.02, … 0.99, 1.00, 1.01); a major release goes
straight to the next `.00` (0.37 → 1.00, 1.04 → 2.00). Home Assistant and HACS compare the
two numbers, so 0.10 is newer than 0.09. Until the first release the files carry 0.00.

The versions released so far are the repository's git tags. `.github/workflows/release.yml`
runs these in order:

    release.py latest              the newest released version (prints nothing before the first)
    release.py next minor|major    the version the next release gets
    release.py stamp VERSION       write VERSION into manifest.json, pyproject.toml and
                                   api/const.py, and file the changelog's "Unreleased" section
                                   under it
    release.py notes VERSION       that version's section of the changelog, for the release notes
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "custom_components" / "myq"
MANIFEST = COMPONENT / "manifest.json"
CONST = COMPONENT / "api" / "const.py"
PYPROJECT = ROOT / "pyproject.toml"
CHANGELOG = ROOT / "CHANGELOG.md"

# The first release: 0.01, or 1.00 when the first release is a major one.
FIRST = {"minor": (0, 1), "major": (1, 0)}
BUMPS = ("minor", "major")
# Only tags this scheme made count as releases: `0.5`, `v0.05`, `00.05` and `0.1.10` are not.
VERSION = re.compile(r"(0|[1-9]\d*)\.(\d{2})")


def parse(text: str) -> tuple[int, int] | None:
    """`"0.07"` as `(0, 7)`; None for anything that is not a version of this scheme."""
    match = VERSION.fullmatch(text)
    return (int(match[1]), int(match[2])) if match else None


def show(version: tuple[int, int]) -> str:
    major, minor = version
    return f"{major}.{minor:02d}"


def latest(tags: Iterable[str]) -> str | None:
    """The highest released version among `tags`, comparing numbers, not text or tag dates."""
    versions = [version for version in map(parse, tags) if version and version != (0, 0)]
    return show(max(versions)) if versions else None


def following(current: str | None, bump: str) -> str:
    """The version released after `current` (None: nothing has been released yet)."""
    if bump not in BUMPS:
        raise ValueError(f"the kind of release must be one of {BUMPS}, not {bump!r}")
    if current is None:
        return show(FIRST[bump])
    version = parse(current)
    if version is None:
        raise ValueError(f"{current!r} is not a MAJOR.NN version")
    major, minor = version
    if bump == "major" or minor == 99:
        return show((major + 1, 0))
    return show((major, minor + 1))


def _set_once(text: str, pattern: str, version: str, where: str) -> str:
    result, count = re.subn(
        pattern, lambda found: f'{found[1]}"{version}"', text, flags=re.MULTILINE
    )
    if count != 1:
        raise ValueError(f"expected exactly one version in {where}, found {count}")
    return result


def stamp_manifest(text: str, version: str) -> str:
    """manifest.json with its version replaced and its layout left alone."""
    stamped = _set_once(text, r'^(\s*"version":\s*)"[^"]*"', version, "manifest.json")
    json.loads(stamped)
    return stamped


def stamp_pyproject(text: str, version: str) -> str:
    return _set_once(text, r'^(version\s*=\s*)"[^"]*"', version, "pyproject.toml")


def stamp_const(text: str, version: str) -> str:
    """api/const.py, whose VERSION also goes into the integration's User-Agent."""
    return _set_once(text, r'^(VERSION\s*=\s*)"[^"]*"', version, "api/const.py")


def release_changelog(text: str, version: str, date: str) -> str:
    """The changelog with "Unreleased" filed under `version`, and a new empty section above it."""
    if re.search(rf"^## \[{re.escape(version)}\]", text, flags=re.MULTILINE):
        raise ValueError(f"CHANGELOG.md already has a section for {version}")
    headings = f"## Unreleased\n\n## [{version}] - {date}"
    result, count = re.subn(r"^## Unreleased[ \t]*$", headings, text, flags=re.MULTILINE)
    if count != 1:
        raise ValueError(f'CHANGELOG.md needs exactly one "## Unreleased" heading, found {count}')
    return result


def notes(text: str, version: str) -> str:
    """The body of `version`'s section of the changelog ("" if it has none)."""
    section = re.search(
        rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return section[1].strip() if section else ""


def git_tags() -> list[str]:
    listed = subprocess.run(
        ["git", "tag", "--list"], cwd=ROOT, check=True, capture_output=True, text=True
    )
    return listed.stdout.split()


def stamp(version: str) -> None:
    if parse(version) in (None, (0, 0)):
        raise ValueError(f"{version!r} is not a releasable MAJOR.NN version")
    today = dt.datetime.now(dt.UTC).date().isoformat()
    # Everything is worked out before anything is written, so a refusal changes no file.
    files = {
        MANIFEST: stamp_manifest(MANIFEST.read_text(encoding="utf-8"), version),
        PYPROJECT: stamp_pyproject(PYPROJECT.read_text(encoding="utf-8"), version),
        CONST: stamp_const(CONST.read_text(encoding="utf-8"), version),
        CHANGELOG: release_changelog(CHANGELOG.read_text(encoding="utf-8"), version, today),
    }
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Version numbers and release bookkeeping.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("latest", help="print the newest released version")
    commands.add_parser("next", help="print the next version").add_argument("bump", choices=BUMPS)
    commands.add_parser("stamp", help="write a version into the files").add_argument("version")
    commands.add_parser("notes", help="print a version's changelog").add_argument("version")
    args = parser.parse_args(argv)
    try:
        if args.command == "latest":
            print(latest(git_tags()) or "")
        elif args.command == "next":
            print(following(latest(git_tags()), args.bump))
        elif args.command == "stamp":
            stamp(args.version)
        else:
            print(notes(CHANGELOG.read_text(encoding="utf-8"), args.version))
    except ValueError as err:
        print(f"release.py: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
