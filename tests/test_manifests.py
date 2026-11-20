from __future__ import annotations

import json
import re
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
