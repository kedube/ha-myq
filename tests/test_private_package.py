"""A private config-directory install includes registration without leaking it publicly."""

import json
import os
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from scripts.package import build, build_private

REGISTRATION_NAME = "myq_app_check.json"
REGISTRATION = {
    "project": "test-project",
    "application": "1:123456:android:abcdef",
    "api_key": "synthetic-private-key",
    "debug_token": "synthetic-private-debug",
    "certificate": "ab" * 20,
    "package": "com.chamberlain.android.liftmaster.myq",
}
ERROR = "Private App Check registration is missing, unreadable, or invalid"


@pytest.fixture
def package_root(tmp_path):
    """Minimal source tree: no real checkout files or registration credentials."""
    files = {
        "custom_components/myq/manifest.json": '{"domain":"myq","version":"0.06"}',
        "custom_components/myq/__init__.py": '"""Synthetic integration."""\n',
        "custom_components/myq/api/http.py": '"""Synthetic runtime module."""\n',
        "custom_components/myq/api/browser.py": '"""Desktop helper."""\n',
        "custom_components/myq/api/har.py": '"""Desktop capture reader."""\n',
        "custom_components/myq/frontend/myq-panel.js": "export const panel = true;\n",
        "custom_components/myq/frontend/i18n/en.json": '{"title":"Garage"}',
        "README.md": "Synthetic documentation\n",
        "CHANGELOG.md": "Synthetic changelog\n",
        "LICENSE": "Synthetic license\n",
        "pyproject.toml": '[project]\nname="synthetic"\n',
        "scripts/browser_handoff.py": '"""Synthetic helper."""\n',
        "scripts/probe_account.py": '"""Synthetic probe."""\n',
        "docs/sign-in.md": "Synthetic sign-in guide\n",
    }
    for relative, text in files.items():
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text)
    return tmp_path


@pytest.fixture
def private_registration(package_root):
    path = package_root / REGISTRATION_NAME
    path.write_text(json.dumps(REGISTRATION))
    return path


def test_private_install_has_public_component_and_root_registration(
    package_root, private_registration
):
    public, _ = build(package_root, package_root / "dist")
    private = build_private(package_root, private_registration)

    assert private == package_root / "analysis/private/install/myq-install-private.zip"
    with ZipFile(public) as public_zip, ZipFile(private) as private_zip:
        expected = {f"custom_components/myq/{name}" for name in public_zip.namelist()}
        assert set(private_zip.namelist()) == expected | {REGISTRATION_NAME}
        for name in public_zip.namelist():
            assert private_zip.read(f"custom_components/myq/{name}") == public_zip.read(name)
        assert private_zip.read(REGISTRATION_NAME) == private_registration.read_bytes()
        assert (private_zip.getinfo(REGISTRATION_NAME).external_attr >> 16) & 0o777 == 0o600
    if os.name == "posix":
        assert private.stat().st_mode & 0o777 == 0o600


def test_public_archives_exclude_registration_from_every_scanned_location(package_root):
    for folder in ("", "custom_components/myq", "custom_components/myq/nested", "docs/nested"):
        path = package_root / folder / REGISTRATION_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(REGISTRATION))

    for archive in build(package_root, package_root / "dist"):
        with ZipFile(archive) as package:
            assert all(Path(name).name != REGISTRATION_NAME for name in package.namelist())
            assert all(
                REGISTRATION["debug_token"].encode() not in package.read(name)
                for name in package.namelist()
            )


@pytest.mark.parametrize(
    "content",
    [
        "synthetic-private-debug",
        json.dumps([REGISTRATION]),
        json.dumps({"debug_token": "synthetic-private-debug"}),
        json.dumps({**REGISTRATION, "api_key": ""}),
        json.dumps({**REGISTRATION, "certificate": 123}),
        json.dumps({**REGISTRATION, "debug_token": "x" * 16384}),
    ],
)
def test_invalid_registration_produces_no_private_install(package_root, content):
    path = package_root / REGISTRATION_NAME
    path.write_text(content)
    with pytest.raises(ValueError) as caught:
        build_private(package_root, path)
    assert str(caught.value) == ERROR
    assert not (package_root / "analysis/private/install/myq-install-private.zip").exists()


def test_missing_registration_produces_no_private_install(package_root):
    with pytest.raises(ValueError) as caught:
        build_private(package_root, package_root / REGISTRATION_NAME)
    assert str(caught.value) == ERROR
    assert not (package_root / "analysis/private/install/myq-install-private.zip").exists()


def test_unreadable_registration_error_redacts_filesystem_details(
    package_root, private_registration
):
    with (
        patch("scripts.package.os.open", side_effect=PermissionError("synthetic-private-debug")),
        pytest.raises(ValueError) as caught,
    ):
        build_private(package_root, private_registration)
    assert str(caught.value) == ERROR
    assert caught.value.__suppress_context__
    assert not (package_root / "analysis/private/install/myq-install-private.zip").exists()


def test_private_install_does_not_print_registration(package_root, private_registration, capsys):
    build_private(package_root, private_registration)
    output = capsys.readouterr()
    assert REGISTRATION["debug_token"] not in output.out + output.err
    assert REGISTRATION["api_key"] not in output.out + output.err
