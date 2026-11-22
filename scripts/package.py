#!/usr/bin/env python3
"""Build release archives: an HA install zip and a full source/documentation bundle."""

import hashlib
import json
import zipfile
from pathlib import Path

# Loaded only by the desktop scripts, which read api/ from the source tree.
DESKTOP_ONLY = frozenset({"api/browser.py", "api/har.py"})
SUFFIXES = frozenset({".py", ".json", ".yaml", ".js", ".png"})
HA_ZIP = "myq.zip"  # hacs.json "filename"


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
    sources = sorted(p for p in component.rglob("*") if p.suffix in SUFFIXES and p.is_file())
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
    extras += sorted(p for p in (root / "docs").rglob("*") if p.suffix in {".md", ".json"})
    _write(bundle, {p.relative_to(root).as_posix(): p for p in [*sources, *extras]})
    return ha_zip, bundle


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    for path in build(root, root / "dist"):
        with zipfile.ZipFile(path) as archive:
            count = len(archive.namelist())
        checksum = path.with_name(path.name + ".sha256").read_text().split()[0]
        print(f"Built {path} ({count} files)")
        print(f"SHA256 {checksum}")


if __name__ == "__main__":
    main()
