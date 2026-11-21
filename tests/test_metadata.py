"""Release metadata, action descriptions and translations stay in agreement."""

import json
import tomllib
from pathlib import Path

import yaml

from custom_components.myq.api.const import USER_AGENT, VERSION
from custom_components.myq.services import SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/myq"


def test_version_matches_manifest_and_project():
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert manifest["version"] == project["version"] == VERSION
    assert USER_AGENT == f"ha-myq/{VERSION}"


def test_strings_and_english_translation_are_identical():
    assert json.loads((COMPONENT / "strings.json").read_text()) == json.loads(
        (COMPONENT / "translations/en.json").read_text()
    )


def test_every_action_and_field_is_described_and_translated():
    described = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    translated = json.loads((COMPONENT / "strings.json").read_text())["services"]
    assert set(described) == set(SCHEMAS) == set(translated)
    for name, schema in SCHEMAS.items():
        fields = {str(key) for key in schema}
        assert set(described[name].get("fields", {})) == fields, name
        assert translated[name]["name"] and translated[name]["description"], name
        for field in fields:
            text = translated[name]["fields"][field]
            assert text["name"] and text["description"], (name, field)
        # Text lives in translations only; services.yaml keeps selectors and flags.
        assert "name" not in described[name] and "description" not in described[name]


def test_home_assistant_code_never_imports_desktop_only_modules():
    import ast

    from scripts.package import DESKTOP_ONLY

    desktop = {Path(name).stem for name in DESKTOP_ONLY}
    for path in COMPONENT.rglob("*.py"):
        if path.relative_to(COMPONENT).as_posix() in DESKTOP_ONLY:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                imported = {(node.module or "").rsplit(".", 1)[-1]}
                imported |= {alias.name for alias in node.names}
                assert not imported & desktop, path


def test_every_home_assistant_component_used_is_a_dependency():
    """hassfest: an imported component must be in dependencies or after_dependencies.

    Entity platforms are exempt. hassfest also allows a few other built-ins; this is stricter.
    """
    import ast

    from homeassistant.const import Platform

    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    declared = {*manifest["dependencies"], *manifest["after_dependencies"]}
    used = set()
    for path in COMPONENT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module == "homeassistant.components":
                modules = [f"{node.module}.{alias.name}" for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                modules = [node.module or ""]
            else:
                continue
            for parts in (module.split(".") for module in modules):
                if parts[:2] == ["homeassistant", "components"] and len(parts) > 2:
                    used.add(parts[2])
    assert {"camera", "web_rtc", "frontend"} <= used  # the scan finds the imports
    assert used - {platform.value for platform in Platform} - declared == set()


def test_ha_install_zip_excludes_desktop_modules(tmp_path):
    import zipfile

    from scripts.package import DESKTOP_ONLY, HA_ZIP, build

    ha_zip, bundle = build(ROOT, tmp_path)
    assert ha_zip.name == HA_ZIP == json.loads((ROOT / "hacs.json").read_text())["filename"]
    names = set(zipfile.ZipFile(ha_zip).namelist())
    assert {"manifest.json", "__init__.py", "www/myq-live-card.js", "translations/en.json"} <= names
    assert not names & DESKTOP_ONLY
    full = set(zipfile.ZipFile(bundle).namelist())
    assert {f"custom_components/myq/{name}" for name in DESKTOP_ONLY} <= full
    assert "scripts/probe_account.py" in full


def test_dependency_pins_agree():
    """The checks install the test group, not the project, so its pins must match."""
    manifest = json.loads((COMPONENT / "manifest.json").read_text())
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    runtime = set(manifest["requirements"])
    assert runtime <= set(pyproject["project"]["dependencies"])
    assert runtime <= set(pyproject["dependency-groups"]["test"])
    assert "test" not in pyproject["project"]["optional-dependencies"]
