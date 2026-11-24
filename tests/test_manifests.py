from __future__ import annotations

import importlib.util
import json
import re
import shutil
from pathlib import Path

import pytest

from nexus_workbench import __version__

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("manifest", [".claude-plugin/plugin.json", ".codex-plugin/plugin.json"])
def test_plugin_version_matches_package(manifest: str) -> None:
    data = json.loads((ROOT / manifest).read_text(encoding="utf-8"))
    assert data["version"] == __version__


def test_pyproject_version_matches_package() -> None:
    # tomllib is Python 3.11+, and the package supports 3.10.
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', text, flags=re.MULTILINE)
    assert match and match.group(1) == __version__


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bump_version = _load_script("bump_version")
changelog_notes = _load_script("changelog_notes")

VERSION_FILES = [
    "pyproject.toml",
    "src/nexus_workbench/__init__.py",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "server.json",
    "CHANGELOG.md",
]


def _copy_version_files(dest: Path) -> Path:
    for name in VERSION_FILES:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return dest


def test_all_versions_in_sync() -> None:
    found = bump_version.read_versions()
    assert len(found) == 5
    assert set(found.values()) == {__version__}


def test_bump_sets_every_file(tmp_path: Path) -> None:
    root = _copy_version_files(tmp_path)
    bump_version.write_version("9.8.7", root)
    assert set(bump_version.read_versions(root).values()) == {"9.8.7"}
    # The manifests stay valid JSON with their other fields untouched.
    for name in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        before = json.loads((ROOT / name).read_text(encoding="utf-8"))
        after = json.loads((root / name).read_text(encoding="utf-8"))
        before["version"] = "9.8.7"
        assert after == before
    before = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    after = json.loads((root / "server.json").read_text(encoding="utf-8"))
    before["version"] = before["packages"][0]["version"] = "9.8.7"
    assert after == before


def test_check_release_refuses_dev_version(tmp_path: Path) -> None:
    root = _copy_version_files(tmp_path)
    bump_version.write_version("0.1.0.dev0", root)
    problems = bump_version.check_release("v0.1.0", root)
    assert any("development version" in p for p in problems)
    assert bump_version.check_release("v0.1.0.dev0", root)


def test_check_release_refuses_mismatch_and_undated_changelog(tmp_path: Path) -> None:
    root = _copy_version_files(tmp_path)
    (root / "CHANGELOG.md").write_text("## [0.1.0] – unreleased\n\nText.\n", encoding="utf-8")
    bump_version.write_version("0.1.0", root)
    problems = bump_version.check_release("v0.1.0", root)
    assert problems == ["CHANGELOG.md still says unreleased for 0.1.0. Add the date."]
    problems = bump_version.check_release("v0.2.0", root)
    assert any("but the tag is v0.2.0" in p for p in problems)
    assert bump_version.check_release("0.1.0", root)


def test_check_release_passes_when_ready(tmp_path: Path) -> None:
    root = _copy_version_files(tmp_path)
    (root / "CHANGELOG.md").write_text("## [0.1.0] – 2030-01-01\n\nText.\n", encoding="utf-8")
    bump_version.write_version("0.1.0", root)
    assert bump_version.check_release("v0.1.0", root) == []


def test_changelog_notes_extracts_one_section() -> None:
    text = (
        "# Changelog\n\n## [0.2.0] – 2030-02-01\n\n### Added\n\n- Two.\n\n"
        "## [0.1.0] – 2030-01-01\n\n- One.\n\n[0.2.0]: https://example.org\n"
    )
    assert changelog_notes.section("0.2.0", text) == "### Added\n\n- Two.\n"
    assert changelog_notes.section("0.1.0", text) == "- One.\n"
    assert changelog_notes.section("0.3.0", text) is None


def test_changelog_has_section_for_current_version() -> None:
    release = __version__.split(".dev")[0]
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert changelog_notes.section(release, text)
