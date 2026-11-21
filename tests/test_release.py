"""Release version numbers (0.01 … 0.99, 1.00, 1.01) and the files a release rewrites."""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import pytest
from awesomeversion import AwesomeVersion, AwesomeVersionStrategy

ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location("release", ROOT / "scripts" / "release.py")
assert _spec is not None and _spec.loader is not None
release = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release)

# The version formats Home Assistant's loader and hassfest accept from a custom integration.
ACCEPTED = [
    AwesomeVersionStrategy.CALVER,
    AwesomeVersionStrategy.SEMVER,
    AwesomeVersionStrategy.SIMPLEVER,
    AwesomeVersionStrategy.BUILDVER,
    AwesomeVersionStrategy.PEP440,
]

CHANGELOG = """\
# Changelog

## Unreleased

### Added

- A thing.

### Fixed

- Another thing.

## [0.04] - 2026-01-02

### Added

- An older thing.
"""


def test_the_first_release_is_0_01_or_1_00_for_a_major_one() -> None:
    assert release.following(None, "minor") == "0.01"
    assert release.following(None, "major") == "1.00"


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        ("0.01", "0.02"),
        ("0.09", "0.10"),
        ("0.98", "0.99"),
        ("0.99", "1.00"),
        ("1.00", "1.01"),
        ("1.99", "2.00"),
        ("9.99", "10.00"),
        ("12.34", "12.35"),
    ],
)
def test_a_minor_release_counts_up_and_carries_after_99(current: str, expected: str) -> None:
    assert release.following(current, "minor") == expected


@pytest.mark.parametrize(
    ("current", "expected"),
    [("0.01", "1.00"), ("0.37", "1.00"), ("0.99", "1.00"), ("1.00", "2.00"), ("9.05", "10.00")],
)
def test_a_major_release_goes_to_the_next_round_number(current: str, expected: str) -> None:
    assert release.following(current, "major") == expected


def test_an_unknown_kind_of_release_or_version_is_refused() -> None:
    with pytest.raises(ValueError, match="kind of release"):
        release.following("0.01", "patch")
    with pytest.raises(ValueError, match=r"MAJOR\.NN"):
        release.following("0.1.10", "minor")


def test_the_latest_release_is_the_highest_number() -> None:
    # Text order would pick 9.99, and tag order the last one listed.
    assert release.latest(["0.99", "10.00", "2.00", "9.99", "0.02"]) == "10.00"


def test_tags_from_outside_the_scheme_are_not_releases() -> None:
    assert release.latest(["v0.50", "0.1.10", "1.2.3", "0.5", "00.05", "0.00", "nightly"]) is None
    assert release.latest(["v9.00", "0.03", "2.0"]) == "0.03"
    assert release.latest([]) is None


def test_home_assistant_accepts_every_version_and_reads_each_as_newer() -> None:
    """Walk 0.01 → 3.00 one minor release at a time, the way installations will see them."""
    version = release.following(None, "minor")
    seen = [version]
    while version != "3.00":
        newer = release.following(version, "minor")
        assert AwesomeVersion(newer, ensure_strategy=ACCEPTED) > AwesomeVersion(version), newer
        version = newer
        seen.append(version)
    assert seen[:3] == ["0.01", "0.02", "0.03"]
    assert seen[8:10] == ["0.09", "0.10"]
    assert seen[98:101] == ["0.99", "1.00", "1.01"]
    # The 0.00 the files carry before the first release, and the old 0.1.10, are older.
    assert AwesomeVersion("0.01") > AwesomeVersion("0.00")
    assert AwesomeVersion("1.00") > AwesomeVersion("0.1.10")


def test_the_manifest_keeps_its_layout_when_stamped() -> None:
    before = release.MANIFEST.read_text(encoding="utf-8")
    after = release.stamp_manifest(before, "2.05")
    assert json.loads(after) == {**json.loads(before), "version": "2.05"}
    changed = [
        (old, new)
        for old, new in zip(before.splitlines(), after.splitlines(), strict=True)
        if old != new
    ]
    assert len(changed) <= 1
    assert all('"version"' in new for _old, new in changed)


def test_pyproject_is_stamped_in_the_project_table_only() -> None:
    before = release.PYPROJECT.read_text(encoding="utf-8")
    after = tomllib.loads(release.stamp_pyproject(before, "2.05"))
    expected = tomllib.loads(before)
    expected["project"]["version"] = "2.05"
    assert after == expected


def test_the_user_agent_version_is_stamped() -> None:
    after = release.stamp_const(release.CONST.read_text(encoding="utf-8"), "2.05")
    assert 'VERSION = "2.05"\n' in after
    assert 'USER_AGENT = f"ha-myq/{VERSION}"' in after


def test_a_file_without_exactly_one_version_is_refused() -> None:
    with pytest.raises(ValueError, match="exactly one version"):
        release.stamp_manifest('{\n  "domain": "x"\n}\n', "0.01")
    with pytest.raises(ValueError, match="exactly one version"):
        release.stamp_pyproject('version = "1"\nversion = "2"\n', "0.01")
    with pytest.raises(ValueError, match="exactly one version"):
        release.stamp_const("DOMAIN = 'x'\n", "0.01")


def test_the_unreleased_section_becomes_the_release() -> None:
    released = release.release_changelog(CHANGELOG, "0.05", "2026-11-13")
    assert released.startswith(
        "# Changelog\n\n## Unreleased\n\n## [0.05] - 2026-11-13\n\n### Added"
    )
    assert release.notes(released, "0.05") == (
        "### Added\n\n- A thing.\n\n### Fixed\n\n- Another thing."
    )
    # Earlier releases are untouched, and the new Unreleased section is empty.
    assert release.notes(released, "0.04") == "### Added\n\n- An older thing."
    assert released.count("## Unreleased") == 1
    assert released.endswith(CHANGELOG.split("## Unreleased")[1])


def test_notes_are_empty_for_a_version_the_changelog_does_not_have() -> None:
    assert release.notes(CHANGELOG, "0.05") == ""
    # 0.0 must not pick up 0.04's section.
    assert release.notes(CHANGELOG, "0.0") == ""


def test_the_changelog_is_refused_when_it_cannot_be_filed() -> None:
    with pytest.raises(ValueError, match=r"already has a section for 0\.04"):
        release.release_changelog(CHANGELOG, "0.04", "2026-11-13")
    with pytest.raises(ValueError, match="exactly one"):
        release.release_changelog("# Changelog\n\n## [0.04] - 2026-01-02\n", "0.05", "2026-11-13")


def test_this_repository_is_ready_for_the_release_workflow() -> None:
    """What the workflow relies on: one version, in the scheme, and an Unreleased section."""
    manifest = json.loads(release.MANIFEST.read_text(encoding="utf-8"))
    project = tomllib.loads(release.PYPROJECT.read_text(encoding="utf-8"))["project"]
    assert release.parse(manifest["version"]) is not None
    assert project["version"] == manifest["version"]
    release.stamp_const(release.CONST.read_text(encoding="utf-8"), "999.00")
    release.release_changelog(release.CHANGELOG.read_text(encoding="utf-8"), "999.00", "2026-01-01")


def test_the_commands_the_workflow_runs(tmp_path, monkeypatch, capsys) -> None:
    for name in ("MANIFEST", "PYPROJECT", "CONST"):
        copy = tmp_path / getattr(release, name).name
        copy.write_text(getattr(release, name).read_text(encoding="utf-8"), encoding="utf-8")
        monkeypatch.setattr(release, name, copy)
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    monkeypatch.setattr(release, "CHANGELOG", changelog)
    monkeypatch.setattr(release, "git_tags", lambda: ["0.03", "0.04", "0.1.10"])

    assert release.main(["latest"]) == 0
    assert capsys.readouterr().out == "0.04\n"
    assert release.main(["next", "minor"]) == 0
    assert capsys.readouterr().out == "0.05\n"
    assert release.main(["next", "major"]) == 0
    assert capsys.readouterr().out == "1.00\n"

    assert release.main(["stamp", "0.05"]) == 0
    assert json.loads(release.MANIFEST.read_text(encoding="utf-8"))["version"] == "0.05"
    assert tomllib.loads(release.PYPROJECT.read_text(encoding="utf-8"))["project"]["version"] == (
        "0.05"
    )
    assert 'VERSION = "0.05"' in release.CONST.read_text(encoding="utf-8")
    assert release.main(["notes", "0.05"]) == 0
    assert capsys.readouterr().out.startswith("### Added\n\n- A thing.")

    # A second attempt is refused, with a message and without rewriting anything.
    before = {path: path.read_text(encoding="utf-8") for path in tmp_path.iterdir()}
    assert release.main(["stamp", "0.05"]) == 1
    assert "already has a section for 0.05" in capsys.readouterr().err
    for refused in ("v0.06", "0.00"):
        assert release.main(["stamp", refused]) == 1
        assert "MAJOR.NN" in capsys.readouterr().err
    assert before == {path: path.read_text(encoding="utf-8") for path in tmp_path.iterdir()}


def test_nothing_is_printed_before_the_first_release(monkeypatch, capsys) -> None:
    monkeypatch.setattr(release, "git_tags", lambda: [])
    assert release.main(["latest"]) == 0
    assert capsys.readouterr().out.strip() == ""
    assert release.main(["next", "minor"]) == 0
    assert capsys.readouterr().out == "0.01\n"
