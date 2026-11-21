"""Every translation key the code uses exists, and every language matches English."""

import ast
import json
import re
import string
from pathlib import Path

import pytest
import yaml

from custom_components.myq import binary_sensor, camera_controls, exceptions, sensor

COMPONENT = Path(__file__).parents[1] / "custom_components" / "myq"
STRINGS = json.loads((COMPONENT / "strings.json").read_text())
LANGUAGES = sorted((COMPONENT / "translations").glob("*.json"))


def flatten(node, path=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from flatten(value, (*path, key))
    else:
        yield path, node


def placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def entity_keys() -> set[str]:
    return {key for platform in STRINGS["entity"].values() for key in platform}


def literal_calls(name: str) -> set[str]:
    """First string arguments of every call to `name` in the component."""
    found = set()
    for path in COMPONENT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == name
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                found.add(node.args[0].value)
    return found


def translated_errors() -> set[str]:
    """Literal translation keys of errors raised with translation_domain=… directly."""
    found = set()
    for path in COMPONENT.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                kwargs = {kw.arg: kw.value for kw in node.keywords}
                key = kwargs.get("translation_key")
                if "translation_domain" in kwargs and isinstance(key, ast.Constant):
                    found.add(key.value)
    return found


def test_english_copy_matches_strings():
    assert json.loads((COMPONENT / "translations" / "en.json").read_text()) == STRINGS


@pytest.mark.parametrize("path", LANGUAGES, ids=lambda path: path.stem)
def test_language_has_same_keys_and_placeholders(path):
    english = dict(flatten(STRINGS))
    translated = dict(flatten(json.loads(path.read_text())))
    assert translated.keys() == english.keys()
    for key, text in english.items():
        assert isinstance(translated[key], str) and translated[key].strip(), key
        assert placeholders(translated[key]) == placeholders(text), key
        # hassfest rejects URLs and HTML in translations.
        assert not re.search(r"https?://|<[a-z/]", translated[key], re.I), key


@pytest.mark.parametrize(
    "path", [COMPONENT / "strings.json", *LANGUAGES], ids=lambda path: path.stem
)
def test_placeholders_are_plain_names(path):
    """hassfest reads every {…} as a placeholder and requires [a-zA-Z_][a-zA-Z0-9_]*.

    So text cannot show a literal brace, such as a JSON example.
    """
    for key, text in flatten(json.loads(path.read_text())):
        for _, name, spec, conversion in string.Formatter().parse(text):
            if name is None:
                continue
            assert name.isascii() and name.isidentifier(), (key, name)
            assert not spec and not conversion, (key, name)


def test_entity_translation_keys_exist():
    used = set()
    for path in COMPONENT.glob("*.py"):
        used |= set(re.findall(r'_attr_translation_key = "([a-z0-9_]+)"$', path.read_text(), re.M))
    used |= set(sensor.FIELDS) | {"last_update"}
    used |= set(binary_sensor.FIELDS) | set(binary_sensor.DERIVED)
    used |= {action for action, _, _ in camera_controls._BUTTONS}
    used |= {f"video_{key}" for key in ("res", "fps", "quality")}
    used |= {f"camera_{key}" for key in ("battery", "power_source", "wifi_strength")}
    used |= {"microphone", "motion_detection", "status_led", "chime_enabled", "auto_tracking"}
    used |= {"wake_on_motion", "motion_sensitivity", "siren_volume", "rotation", "night_vision"}
    used |= {"power_saver", "chime_type"}
    assert used - entity_keys() == set()


def test_select_and_enum_options_are_translated():
    select = STRINGS["entity"]["select"]
    assert set(select["light_timer"]["state"]) == {"90", "150", "210", "270"}
    assert set(select["automatic_lock_delay"]["state"]) == {"off", "30", "60", "90", "120", "180"}
    sensors = STRINGS["entity"]["sensor"]
    assert set(sensors["battery_backup_state"]["state"]) == set(sensor.BACKUP_BATTERY)
    assert set(sensors["orientation"]["state"]) == set(sensor.ORIENTATIONS)


def test_exception_and_selector_keys_exist():
    used = literal_calls("translated") | {key for _, key in exceptions.KINDS} | {"myq_error"}
    used |= translated_errors()
    assert "legacy_entry" in used  # the scan finds direct raises
    assert used - set(STRINGS["exceptions"]) == set()
    services = yaml.safe_load((COMPONENT / "services.yaml").read_text())
    keys = {
        field["selector"]["select"]["translation_key"]
        for service in services.values()
        for field in (service.get("fields") or {}).values()
        if "translation_key" in (field.get("selector") or {}).get("select", {})
    }
    assert keys and keys - set(STRINGS["selector"]) == set()
