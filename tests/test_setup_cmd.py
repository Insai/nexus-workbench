from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from nexus_workbench import __version__, auth, setup_cmd
from nexus_workbench.client import PRODUCTION_API_BASE
from tests.fake_api import make_token

FAKE_CLAUDE = r"""
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["claude", *sys.argv[1:]]) + "\n")
cfg = os.path.join(os.environ["CLAUDE_CONFIG_DIR"], ".claude.json")
data = json.load(open(cfg)) if os.path.exists(cfg) else {}
servers = data.setdefault("mcpServers", {})
args = sys.argv[1:]
if args[:2] == ["mcp", "add"]:
    rest = args[2:]
    sep = rest.index("--")
    opts, command = rest[:sep], rest[sep + 1:]
    scope = opts[opts.index("--scope") + 1]
    assert scope == "user", scope
    env = {}
    names = []
    i = 0
    while i < len(opts):
        if opts[i] == "--scope":
            i += 2
            continue
        if opts[i] == "-e":
            k, v = opts[i + 1].split("=", 1)
            env[k] = v
            i += 2
            continue
        names.append(opts[i])
        i += 1
    name = names[0]
    if name in servers:
        print(f"MCP server {name} already exists in user config", file=sys.stderr)
        sys.exit(1)
    servers[name] = {"type": "stdio", "command": command[0], "args": command[1:], "env": env}
elif args[:2] == ["mcp", "remove"]:
    servers.pop(args[-1], None)
else:
    sys.exit(2)
os.makedirs(os.path.dirname(cfg), exist_ok=True)
json.dump(data, open(cfg, "w"))
"""

FAKE_CODEX = r"""
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["codex", *sys.argv[1:]]) + "\n")
state = os.path.join(os.environ["CODEX_HOME"], "fake-mcp.json")
data = json.load(open(state)) if os.path.exists(state) else {}
args = sys.argv[1:]
if args[:2] == ["mcp", "get"]:
    entry = data.get(args[2])
    if entry is None:
        print(f"Error: No MCP server named '{args[2]}' found.", file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"name": args[2], "transport": entry}))
elif args[:2] == ["mcp", "add"]:
    rest = args[2:]
    sep = rest.index("--")
    opts, command = rest[:sep], rest[sep + 1:]
    env = {}
    for i, opt in enumerate(opts):
        if opt == "--env":
            k, v = opts[i + 1].split("=", 1)
            env[k] = v
    data[opts[0]] = {"type": "stdio", "command": command[0], "args": command[1:],
                     "env": env or None}
    os.makedirs(os.environ["CODEX_HOME"], exist_ok=True)
    json.dump(data, open(state, "w"))
    print(f"Added global MCP server '{opts[0]}'.")
else:
    sys.exit(2)
"""

FAKE_UV = r"""
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(["uv", *sys.argv[1:]]) + "\n")
args = sys.argv[1:]
if args == ["cache", "dir"]:
    print(os.environ["FAKE_UV_CACHE"])
elif args == ["tool", "dir", "--bin"]:
    print(os.environ["FAKE_UV_BIN"])
elif args[:2] == ["tool", "install"]:
    os.makedirs(os.environ["FAKE_UV_BIN"], exist_ok=True)
    exe = os.path.join(os.environ["FAKE_UV_BIN"], "nexus-workbench")
    open(exe, "w").write("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    print("Installed 2 executables: nexus, nexus-workbench")
else:
    sys.exit(2)
"""


class Env:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.bin = root / "fakebin"
        self.bin.mkdir()
        self.log = root / "calls.jsonl"
        self.log.touch()
        self.uv_cache = root / "uv-cache"
        self.uv_bin = root / "uv-bin"
        self.skills = root / "pkg-skills"
        self.skills.mkdir()
        self.monkeypatch = monkeypatch
        monkeypatch.setenv("FAKE_LOG", str(self.log))
        monkeypatch.setenv("FAKE_UV_CACHE", str(self.uv_cache))
        monkeypatch.setenv("FAKE_UV_BIN", str(self.uv_bin))
        monkeypatch.setenv("PATH", str(self.bin))
        monkeypatch.setattr(setup_cmd, "packaged_skills_dir", lambda: self.skills)

    def add(self, name: str, script: str) -> None:
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n{script}")
        path.chmod(0o755)

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def changing_calls(self) -> list[list[str]]:
        return [
            c
            for c in self.calls()
            if c[1:3] in (["mcp", "add"], ["mcp", "remove"], ["tool", "install"])
        ]

    def skill(self, name: str, body: str = "Do the thing.") -> Path:
        directory = self.skills / name
        directory.mkdir()
        (directory / "SKILL.md").write_text(f"---\nname: {name}\ndescription: test\n---\n{body}\n")
        (directory / "notes.txt").write_text("extra")
        return directory

    def claude_config(self) -> dict[str, Any]:
        cfg = Path(setup_cmd.claude_config_file())
        return json.loads(cfg.read_text()) if cfg.exists() else {}

    def codex_state(self) -> dict[str, Any]:
        state = self.root / "codex-home" / "fake-mcp.json"
        return json.loads(state.read_text()) if state.exists() else {}


@pytest.fixture
def env(isolated: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(isolated, monkeypatch)


def run(
    *,
    agent: str = "all",
    no_login: bool = True,
    dry_run: bool = False,
    prefix: str | None = None,
    login: Any = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = setup_cmd.run_setup(
        setup_cmd.SetupOptions(agent=agent, no_login=no_login, dry_run=dry_run, yes=True),
        login=login or (lambda client: pytest.fail("login should not run")),
        out=out,
        err=err,
        prefix=prefix or sys.prefix,
    )
    return code, out.getvalue(), err.getvalue()


def persistent_command() -> str:
    return setup_cmd.running_entry_point()[0]


# -- detection -------------------------------------------------------------------------


def test_is_ephemeral(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    assert setup_cmd.is_ephemeral(str(cache / "archive-v0" / "abc"), cache)
    assert setup_cmd.is_ephemeral(str(tmp_path / "x" / "archive-v0" / "abc"), None)
    assert not setup_cmd.is_ephemeral(str(tmp_path / "tools" / "nexus-workbench"), cache)
    assert not setup_cmd.is_ephemeral(sys.prefix, cache)


def test_running_entry_point_is_absolute() -> None:
    command, args = setup_cmd.running_entry_point()
    assert Path(command).is_absolute()
    assert args[-1] == "mcp"


def test_running_entry_point_prefers_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "bin" / "nexus"
    script.parent.mkdir()
    script.write_text("")
    (tmp_path / "bin" / "nexus-workbench").write_text("")
    monkeypatch.setattr(sys, "argv", [str(script), "setup"])
    assert setup_cmd.running_entry_point() == (str(tmp_path / "bin" / "nexus-workbench"), ["mcp"])


# -- runs ---------------------------------------------------------------------------------


def test_dry_run_changes_nothing(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    env.add("codex", FAKE_CODEX)
    env.skill("workbench-review")
    code, out, _ = run(dry_run=True, no_login=False)
    assert code == 0
    assert "Dry run" in out
    assert "would run: " in out and "claude mcp add --scope user nexus --" in out
    assert "codex mcp add nexus --" in out
    assert "would write: " in out and "workbench-review" in out
    assert "would run: nexus-workbench login" in out
    assert "Claude Code: would be registered" in out
    assert env.changing_calls() == []
    assert env.claude_config() == {}
    assert env.codex_state() == {}
    assert not setup_cmd.claude_skills_dir().exists()
    assert not setup_cmd.codex_skills_dir().exists()


def test_persistent_install_registers_absolute_path(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    env.add("codex", FAKE_CODEX)
    code, out, err = run()
    assert code == 0, err
    command = persistent_command()
    assert Path(command).is_absolute()
    entry = env.claude_config()["mcpServers"]["nexus"]
    assert entry == {"type": "stdio", "command": command, "args": ["mcp"], "env": {}}
    codex_entry = env.codex_state()["nexus"]
    assert codex_entry["command"] == command and codex_entry["args"] == ["mcp"]
    assert not any(c[0] == "uv" and c[1:3] == ["tool", "install"] for c in env.calls())
    assert "no skills packaged" in out
    assert "Next: run `nexus-workbench login`" in out
    assert "list my Workbench recordings" in out


def test_idempotent(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    env.add("codex", FAKE_CODEX)
    env.skill("workbench-review")
    assert run()[0] == 0
    first = len(env.changing_calls())
    claude_cfg = env.claude_config()
    skill_dir = setup_cmd.claude_skills_dir() / "workbench-review"
    mtime = (skill_dir / "SKILL.md").stat().st_mtime_ns
    code, out, _ = run()
    assert code == 0
    assert len(env.changing_calls()) == first  # nothing new
    assert env.claude_config() == claude_cfg
    assert (skill_dir / "SKILL.md").stat().st_mtime_ns == mtime
    assert "Claude Code: already registered" in out
    assert "Codex: already registered" in out
    assert "1 unchanged" in out


def test_replaces_a_different_claude_entry(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    cfg = setup_cmd.claude_config_file()
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(
        json.dumps({"mcpServers": {"nexus": {"type": "stdio", "command": "uvx", "args": []}}})
    )
    code, out, _ = run(agent="claude")
    assert code == 0
    changing = env.changing_calls()
    assert changing[0][:3] == ["claude", "mcp", "remove"]
    assert changing[1][:3] == ["claude", "mcp", "add"]
    assert env.claude_config()["mcpServers"]["nexus"]["command"] == persistent_command()
    assert "updated" in out


def test_api_url_is_passed_to_the_server(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    env.add("claude", FAKE_CLAUDE)
    env.add("codex", FAKE_CODEX)
    monkeypatch.setenv("NEXUS_API_URL", "https://staging.test.invalid")
    monkeypatch.setenv("NEXUS_TOKEN", make_token())
    assert run()[0] == 0
    entry = env.claude_config()["mcpServers"]["nexus"]
    assert entry["env"] == {"NEXUS_API_URL": "https://staging.test.invalid"}
    assert env.codex_state()["nexus"]["env"] == {"NEXUS_API_URL": "https://staging.test.invalid"}
    # Second run is still a no-op with env in the comparison.
    before = len(env.changing_calls())
    assert run()[0] == 0
    assert len(env.changing_calls()) == before


def test_ephemeral_run_installs_with_uv(env: Env) -> None:
    env.add("uv", FAKE_UV)
    env.add("claude", FAKE_CLAUDE)
    prefix = env.uv_cache / "archive-v0" / "abc123"
    code, out, err = run(agent="claude", prefix=str(prefix))
    assert code == 0, err
    assert ["uv", "tool", "install", "--upgrade", "nexus-workbench"] in env.calls()
    assert ["uv", "tool", "dir", "--bin"] in env.calls()
    exe = str(env.uv_bin / "nexus-workbench")
    assert env.claude_config()["mcpServers"]["nexus"]["command"] == exe
    assert "installed with uv tool" in out


def test_ephemeral_dry_run_does_not_install(env: Env) -> None:
    env.add("uv", FAKE_UV)
    env.add("claude", FAKE_CLAUDE)
    prefix = env.uv_cache / "archive-v0" / "abc123"
    code, out, _ = run(agent="claude", prefix=str(prefix), dry_run=True)
    assert code == 0
    assert "would run: " in out and "tool install --upgrade nexus-workbench" in out
    assert env.changing_calls() == []
    assert not env.uv_bin.exists()


def test_ephemeral_install_failure_is_explained(env: Env) -> None:
    env.add("uv", "import sys\nprint('boom', file=sys.stderr)\nsys.exit(1)\n")
    env.add("claude", FAKE_CLAUDE)
    code, _, err = run(prefix=str(env.root / "c" / "archive-v0" / "x"))
    assert code == 1
    assert "uv tool install nexus-workbench" in err


def test_uses_uv_from_env_var(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    hidden = env.root / "hidden"
    hidden.mkdir()
    (hidden / "uv").write_text(f"#!{sys.executable}\n{FAKE_UV}")
    (hidden / "uv").chmod(0o755)
    monkeypatch.setenv("UV", str(hidden / "uv"))
    env.add("claude", FAKE_CLAUDE)
    code, _, err = run(agent="claude", prefix=str(env.uv_cache / "archive-v0" / "z"))
    assert code == 0, err
    assert ["uv", "tool", "install", "--upgrade", "nexus-workbench"] in env.calls()


def test_missing_agent_is_skipped(env: Env) -> None:
    env.add("codex", FAKE_CODEX)
    code, out, _ = run()
    assert code == 0
    assert "Claude Code: not installed, skipped" in out
    assert "nexus" in env.codex_state()


def test_no_agents_fails(env: Env) -> None:
    code, _, err = run()
    assert code == 1
    assert "was not found" in err


def test_skills_installed_with_marker(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    env.add("codex", FAKE_CODEX)
    env.skill("workbench-review")
    code, out, _ = run()
    assert code == 0
    for root in (setup_cmd.claude_skills_dir(), setup_cmd.codex_skills_dir()):
        target = root / "workbench-review"
        assert (target / "SKILL.md").is_file()
        assert (target / "notes.txt").read_text() == "extra"
        assert (target / ".nexus-workbench").read_text().strip() == __version__
    assert setup_cmd.codex_skills_dir() == env.root / "codex-home" / "skills"
    assert setup_cmd.claude_skills_dir() == env.root / "claude-config" / "skills"


def test_skill_update_replaces_our_own(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    source = env.skill("workbench-review", "v1")
    assert run(agent="claude")[0] == 0
    (source / "SKILL.md").write_text("---\nname: workbench-review\ndescription: t\n---\nv2\n")
    (source / "notes.txt").unlink()
    code, out, _ = run(agent="claude")
    assert code == 0
    target = setup_cmd.claude_skills_dir() / "workbench-review"
    assert "v2" in (target / "SKILL.md").read_text()
    assert not (target / "notes.txt").exists()
    assert "1 updated" in out


def test_skill_collision_is_skipped(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    env.skill("workbench-review")
    env.skill("workbench-other")
    theirs = setup_cmd.claude_skills_dir() / "workbench-review"
    theirs.mkdir(parents=True)
    (theirs / "SKILL.md").write_text("somebody else's skill")
    code, out, err = run(agent="claude")
    assert code == 0
    assert (theirs / "SKILL.md").read_text() == "somebody else's skill"
    assert not (theirs / ".nexus-workbench").exists()
    assert "skipped skill `workbench-review`" in err
    assert "not installed by nexus-workbench" in err
    assert (setup_cmd.claude_skills_dir() / "workbench-other" / "SKILL.md").exists()
    assert "1 collision" in out and "1 installed" in out


def test_empty_packaged_skills_dir(tmp_path: Path) -> None:
    empty = tmp_path / "_skills"
    empty.mkdir()
    (empty / ".gitkeep").write_text("")
    assert setup_cmd.list_skills(empty) == []
    assert setup_cmd.list_skills(None) == []
    assert setup_cmd.list_skills(tmp_path / "missing") == []


def test_source_checkout_skills_dir_exists() -> None:
    found = setup_cmd.packaged_skills_dir()
    assert found is not None and found.is_dir()


# -- login step ------------------------------------------------------------------------------


def test_login_runs_when_signed_out(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    calls: list[Any] = []
    code, out, _ = run(
        agent="claude", no_login=False, login=lambda client: calls.append(client) or 0
    )
    assert code == 0
    assert len(calls) == 1
    assert calls[0].api_base == PRODUCTION_API_BASE
    assert "Signed in to Workbench" in out
    assert "Next: run `nexus-workbench login`" not in out


def test_login_failure_makes_setup_fail(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    code, out, _ = run(agent="claude", no_login=False, login=lambda client: 1)
    assert code == 1
    assert "did not finish" in out


def test_login_skipped_when_signed_in(env: Env) -> None:
    env.add("claude", FAKE_CLAUDE)
    auth.save_token(PRODUCTION_API_BASE, make_token())
    code, out, _ = run(agent="claude", no_login=False)
    assert code == 0
    assert "already signed in" in out


def test_cli_setup_entry(env: Env, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    from nexus_workbench import cli

    env.add("codex", FAKE_CODEX)
    assert cli.main(["setup", "--agent", "codex", "--no-login", "--dry-run"]) == 0
    assert "codex mcp add nexus" in capsys.readouterr().out
    assert env.changing_calls() == []
