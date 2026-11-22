#!/usr/bin/env python3
"""Build public release archives or a private installation with App Check registration."""

import argparse
import hashlib
import json
import os
import stat
import tempfile
import zipfile
from pathlib import Path

# Loaded only by the desktop scripts, which read api/ from the source tree.
DESKTOP_ONLY = frozenset({"api/browser.py", "api/har.py"})
SUFFIXES = frozenset({".py", ".json", ".yaml", ".js", ".png"})
HA_ZIP = "myq.zip"  # hacs.json "filename"
REGISTRATION_FILE = "myq_app_check.json"
PRIVATE_FILES = frozenset({REGISTRATION_FILE})


def _component_sources(root: Path) -> list[Path]:
    """Select distributable component files; credentials are never public assets."""
    return sorted(
        p
        for p in (root / "custom_components/myq").rglob("*")
        if p.suffix in SUFFIXES and p.is_file() and p.name not in PRIVATE_FILES
    )


def _write(destination: Path, files: dict[str, Path]) -> str:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, files[name].read_bytes())
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_name(destination.name + ".sha256").write_text(
        f"{checksum}  {destination.name}\n"
    )
    return checksum


def build(root: Path, out: Path) -> tuple[Path, Path]:
    component = root / "custom_components/myq"
    version = json.loads((component / "manifest.json").read_text())["version"]
    sources = _component_sources(root)
    out.mkdir(parents=True, exist_ok=True)

    # HACS extracts this archive's root directly into custom_components/myq.
    ha_zip = out / HA_ZIP
    ha_files = {
        name: p
        for p in sources
        if (name := p.relative_to(component).as_posix()) not in DESKTOP_ONLY
    }
    _write(ha_zip, ha_files)

    bundle = out / f"myq-{version}.zip"
    extras = [root / "README.md", root / "CHANGELOG.md", root / "LICENSE", root / "pyproject.toml"]
    extras += [root / "scripts/browser_handoff.py", root / "scripts/probe_account.py"]
    extras += sorted(
        p
        for p in (root / "docs").rglob("*")
        if p.suffix in {".md", ".json"} and p.name not in PRIVATE_FILES
    )
    _write(bundle, {p.relative_to(root).as_posix(): p for p in [*sources, *extras]})
    return ha_zip, bundle


def build_private(root: Path, registration: Path) -> Path:
    """Build one private ZIP to extract at the Home Assistant configuration root.

    The registration stays outside the component directory so replacing the
    integration during later updates preserves it. This ZIP must never enter
    dist/, whose archives the release workflow publishes automatically.
    """
    try:
        fd = os.open(registration, os.O_RDONLY | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError
            raw = handle.read(16385)
        if len(raw) > 16384:
            raise ValueError
        fields = json.loads(raw)
        required = {"project", "application", "api_key", "debug_token", "certificate"}
        if (
            not isinstance(fields, dict)
            or not required <= fields.keys()
            or fields.keys() - required - {"package"}
            or any(not isinstance(value, str) or not value.strip() for value in fields.values())
        ):
            raise ValueError
    except (OSError, ValueError, TypeError, RecursionError):
        raise ValueError(
            "Private App Check registration is missing, unreadable, or invalid"
        ) from None

    component = root / "custom_components/myq"
    files = {
        f"custom_components/myq/{name}": path
        for path in _component_sources(root)
        if (name := path.relative_to(component).as_posix()) not in DESKTOP_ONLY
    }
    out = root / "analysis/private/install"
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = out / "myq-install-private.zip"
    # NamedTemporaryFile creates mode 0600 before any credential bytes are written.
    with tempfile.NamedTemporaryFile(dir=out, suffix=".zip") as temporary:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted([*files, REGISTRATION_FILE]):
                info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                private = name == REGISTRATION_FILE
                info.external_attr = (0o100600 if private else 0o100644) << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(info, raw if private else files[name].read_bytes())
        temporary.flush()
        # The temporary file is still owned by its context manager, so hard-link
        # the completed archive to a separate name before atomically replacing.
        staging = Path(temporary.name).with_suffix(".ready")
        try:
            os.link(temporary.name, staging)
            os.replace(staging, destination)
        finally:
            staging.unlink(missing_ok=True)
    return destination


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-registration",
        type=Path,
        nargs="?",
        const=root / REGISTRATION_FILE,
        help="Build a private config-directory install ZIP with this registration file",
    )
    args = parser.parse_args()
    if args.private_registration is not None:
        try:
            path = build_private(root, args.private_registration)
        except ValueError as error:
            parser.exit(1, f"{error}\n")
        print(f"Built private install: {path}")
        print("Extract into the Home Assistant configuration directory. Keep this ZIP private.")
        return
    for path in build(root, root / "dist"):
        with zipfile.ZipFile(path) as archive:
            count = len(archive.namelist())
        checksum = path.with_name(path.name + ".sha256").read_text().split()[0]
        print(f"Built {path} ({count} files)")
        print(f"SHA256 {checksum}")


if __name__ == "__main__":
    main()
