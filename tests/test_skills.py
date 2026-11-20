"""The packaged skills agree with the server: folder names, front matter and tool names."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nexus_workbench import mcp_server

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SKILL_DIRS = sorted(p for p in SKILLS.iterdir() if p.is_dir() and not p.name.startswith("."))

#: A backticked word that looks like a tool name.
TOOL_LIKE = re.compile(r"`((?:whoami|list|get|inspect|check|compute|compare)(?:_[a-z]+)*)`")


def tool_names() -> set[str]:
    specs = mcp_server.workspace_tools(mcp_server.default_client_factory)
    return {s.name for s in specs + mcp_server.local_tools()}


def front_matter(text: str) -> dict[str, str]:
    match = re.match(r"---\n(.*?)\n---\n", text, flags=re.DOTALL)
    assert match, "SKILL.md must start with front matter"
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def test_ten_skills_and_nine_tools() -> None:
    assert len(SKILL_DIRS) == 10
    assert len(tool_names()) == 9


@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_skill_front_matter_names_its_folder(skill: Path) -> None:
    fields = front_matter((skill / "SKILL.md").read_text(encoding="utf-8"))
    assert fields.get("name") == skill.name
    assert "Use when" in fields.get("description", "") or "Use before" in fields.get(
        "description", ""
    )


@pytest.mark.parametrize("skill", SKILL_DIRS, ids=lambda p: p.name)
def test_skill_names_only_real_tools(skill: Path) -> None:
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    unknown = set(TOOL_LIKE.findall(text)) - tool_names()
    assert not unknown, f"{skill.name} names tools the server does not have: {sorted(unknown)}"


def test_plugin_manifests_name_the_package_and_find_the_skills() -> None:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^name = "([^"]+)"$', text, flags=re.MULTILINE)
    assert match
    for manifest in (".claude-plugin/plugin.json", ".codex-plugin/plugin.json"):
        data = json.loads((ROOT / manifest).read_text(encoding="utf-8"))
        assert data["name"] == match.group(1)
    codex = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert (ROOT / codex["skills"]).resolve() == SKILLS
    mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp_server.SERVER_NAME in mcp["mcpServers"]
