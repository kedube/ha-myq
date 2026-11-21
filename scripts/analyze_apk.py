#!/usr/bin/env python3
"""Reproduce the APK provenance and static HTTP route inventory with JADX.

Only protocol facts and source references are emitted. Decompiled application
code stays in the supplied private work directory, outside the integration.
"""

import argparse
import hashlib
import json
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

PACKAGE = "com.chamberlain.android.liftmaster.myq"
METHODS = {"f": "GET", "o": "POST", "p": "PUT", "b": "DELETE", "n": "PATCH"}
ROUTE = re.compile(r'@lj2\.([fopbn])\("([^"\n]+)"\)')
HTTP_ROUTE = re.compile(r'@lj2\.h\([^\n]*method = "([A-Z]+)", path = "([^"\n]+)"')
SDK_ROUTE = re.compile(
    r'new com\.seedonk\.mobilesdk\.RestApi\.API\("([A-Z_]+)", \d+, '
    r'new com\.seedonk\.mobilesdk\.ApiDetails\((requestMethod\d*), "([^"\n]+)"'
)
SDK_METHOD = re.compile(r"RequestMethod (requestMethod\d*) = [\w.]+\.([A-Z]+);")
SOURCE = re.compile(r"compiled from: ([^*]+?) \*/")


def digest(path: Path) -> str:
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def inventory(source_root: Path) -> list[dict]:
    routes = []
    for file in sorted(source_root.rglob("*.java")):
        content = file.read_text(errors="replace")
        source = SOURCE.search(content)
        for match in ROUTE.finditer(content):
            routes.append(
                {
                    "method": METHODS[match[1]],
                    "path": match[2],
                    "source_file": file.relative_to(source_root).as_posix(),
                    "source_type": source[1] if source else "unknown",
                    "line": content[: match.start()].count("\n") + 1,
                    "declaration_kind": "retrofit_annotation",
                }
            )
        for match in HTTP_ROUTE.finditer(content):
            routes.append(
                {
                    "method": match[1],
                    "path": match[2],
                    "source_file": file.relative_to(source_root).as_posix(),
                    "source_type": source[1] if source else "unknown",
                    "line": content[: match.start()].count("\n") + 1,
                    "declaration_kind": "retrofit_http_annotation",
                }
            )
        if file.name == "RestApi.java":
            methods = dict(SDK_METHOD.findall(content))
            for match in SDK_ROUTE.finditer(content):
                routes.append(
                    {
                        "method": methods[match[2]],
                        "path": match[3],
                        "source_file": file.relative_to(source_root).as_posix(),
                        "source_type": "Tend/Seedonk RestApi",
                        "operation": match[1],
                        "line": content[: match.start()].count("\n") + 1,
                        "declaration_kind": "sdk_api_table",
                    }
                )
    return routes


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--jadx-dir", type=Path, help="Reuse an existing JADX output directory")
    parser.add_argument("--output", type=Path, default=Path("docs/api/api_inventory.json"))
    args = parser.parse_args()
    work = args.work_dir or Path(tempfile.mkdtemp(prefix="ha-myq-analysis-"))
    work.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.archive.expanduser()) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        if manifest["package_name"] != PACKAGE:
            raise SystemExit("This archive is not the expected MyQ Android package")
        # Fixed basename; never extract arbitrary archive paths or symlinks.
        apk = work / (PACKAGE + ".apk")
        apk.write_bytes(archive.read(PACKAGE + ".apk"))
    jadx_version = subprocess.check_output(["jadx", "--version"], text=True).strip()
    output = args.jadx_dir or work / "jadx"
    returncode = None
    if not args.jadx_dir:
        with (work / "jadx.log").open("w") as log:
            process = subprocess.run(
                [
                    "jadx",
                    "-j",
                    "4",
                    "--no-imports",
                    "--no-inline-methods",
                    "-d",
                    str(output),
                    str(apk),
                ],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=False,
            )
            returncode = process.returncode
        if not (output / "sources").is_dir():
            raise SystemExit("JADX did not produce sources; inspect the private log")
    routes = inventory(output / "sources")
    result = {
        "package": PACKAGE,
        "version": manifest["version_name"],
        "version_code": manifest["version_code"],
        "archive_sha256": digest(args.archive.expanduser()),
        "base_apk_sha256": digest(apk),
        "signature_status": "Not independently verified against the Play Store signing certificate",
        "jadx_version": jadx_version,
        "jadx_returncode": returncode,
        "coverage": "Static Retrofit literal routes and the Tend/Seedonk SDK HTTP API table. "
        "Runtime URLs, native signaling, unresolved constants and embedded library routes "
        "may be missing or not applicable to MyQ. Paths retain their declaration prefixes; "
        "base URLs must be resolved separately. Presence does not prove server support.",
        "routes": routes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Wrote {len(routes)} static route declarations to {args.output}")


if __name__ == "__main__":
    main()
