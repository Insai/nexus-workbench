"""``nexus-workbench setup``: install, register the MCP server, install skills, sign in.

After ``uvx nexus-workbench setup`` both agents start the server from an
absolute path, so it keeps working in new terminals and when the agent starts
it without the user's shell PATH.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from . import __version__, auth
from .client import ENV_ALLOW_INSECURE_LOCAL, ENV_API_URL, WorkbenchClient, WorkbenchError

PACKAGE = "nexus-workbench"
SERVER_NAME = "nexus"
MARKER = ".nexus-workbench"
AGENTS = ("claude", "codex")
AGENT_LABELS = {"claude": "Claude Code", "codex": "Codex"}
EXE_SUFFIX = ".exe" if os.name == "nt" else ""
COMMAND_TIMEOUT = 300


class SetupError(Exception):
    """A setup step failed. The message says what to do next."""


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------


@dataclass
class Runner:
    """Runs external commands, or only prints them in a dry run."""

    dry_run: bool
    out: TextIO = field(default_factory=lambda: sys.stdout)

    def query(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        """A read-only command. Runs even in a dry run. Returns None if it cannot start."""
        try:
            return subprocess.run(  # noqa: S603 - argv is built here, never from a shell string
                argv, capture_output=True, text=True, timeout=COMMAND_TIMEOUT, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None

    def change(self, argv: list[str]) -> subprocess.CompletedProcess[str] | None:
        """A command that changes something. Printed, not run, in a dry run."""
        if self.dry_run:
            print(f"  would run: {shlex.join(argv)}", file=self.out)
            return None
        return self.query(argv)

    def say(self, text: str) -> None:
        print(text, file=self.out)


def _uv_executable() -> str | None:
    """uv sets ``UV`` to its own path for processes it starts; else look on PATH."""
    candidate = os.environ.get("UV")
    if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
        return candidate
    return shutil.which("uv")


def _failure_text(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return "it could not be started"
    text = (result.stderr or result.stdout or "").strip().splitlines()
    last = text[-1] if text else ""
    return f"exit code {result.returncode}" + (f": {last}" if last else "")


# ---------------------------------------------------------------------------
# Step 1: a persistent install
# ---------------------------------------------------------------------------


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def uv_cache_dir(runner: Runner, uv: str | None) -> Path | None:
    configured = os.environ.get("UV_CACHE_DIR")
    if configured:
        return Path(configured).expanduser()
    if not uv:
        return None
    result = runner.query([uv, "cache", "dir"])
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip().splitlines()[-1])


def is_ephemeral(prefix: str, cache_dir: Path | None) -> bool:
    """True when this copy runs from uv's cache (``uvx`` / ``uv tool run``), which uv may delete."""
    resolved = Path(prefix).resolve()
    if cache_dir is not None and _is_relative_to(resolved, cache_dir.expanduser().resolve()):
        return True
    # uvx environments live in <cache>/archive-v0/<hash>.
    return "archive-v0" in resolved.parts


def running_entry_point() -> tuple[str, list[str]]:
    """The absolute command that starts this copy's MCP server."""
    argv0 = sys.argv[0] if sys.argv else ""
    names = {f"{PACKAGE}{EXE_SUFFIX}", f"nexus{EXE_SUFFIX}"}
    if argv0 and Path(argv0).name in names:
        found = Path(argv0) if Path(argv0).exists() else Path(shutil.which(argv0) or argv0)
        if found.exists():
            sibling = found.parent / f"{PACKAGE}{EXE_SUFFIX}"
            path = sibling if sibling.exists() else found
            return os.path.abspath(path), ["mcp"]
    script = Path(sys.executable).parent / f"{PACKAGE}{EXE_SUFFIX}"
    if script.exists():
        return str(script.absolute()), ["mcp"]
    return os.path.abspath(sys.executable), ["-m", "nexus_workbench", "mcp"]


@dataclass
class ServerCommand:
    command: str
    args: list[str]
    note: str


def ensure_persistent_install(runner: Runner, *, prefix: str | None = None) -> ServerCommand:
    uv = _uv_executable()
    cache = uv_cache_dir(runner, uv)
    if not is_ephemeral(prefix or sys.prefix, cache):
        command, args = running_entry_point()
        return ServerCommand(command, args, "already installed")

    if not uv:
        uvx = shutil.which("uvx")
        if uvx:
            return ServerCommand(
                os.path.abspath(uvx),
                [PACKAGE, "mcp"],
                "uv was not found, so the server will start through uvx each time",
            )
        raise SetupError(
            "This copy is temporary and uv was not found to install it. "
            f"Install uv, then run: uv tool install {PACKAGE}"
        )

    result = runner.change([uv, "tool", "install", "--upgrade", PACKAGE])
    if result is not None and result.returncode != 0:
        raise SetupError(
            f"Installing {PACKAGE} with uv failed ({_failure_text(result)}). "
            f"Run `uv tool install {PACKAGE}` yourself to see why, then run setup again."
        )
    bin_result = runner.query([uv, "tool", "dir", "--bin"])
    if bin_result is None or bin_result.returncode != 0 or not bin_result.stdout.strip():
        raise SetupError(
            "Could not find where uv puts installed commands. Run `uv tool dir --bin` to check."
        )
    exe = Path(bin_result.stdout.strip().splitlines()[-1]) / f"{PACKAGE}{EXE_SUFFIX}"
    if not runner.dry_run and not exe.exists():
        raise SetupError(
            f"uv reported success, but {exe} does not exist. "
            "Run `uv tool list` to check the install."
        )
    return ServerCommand(str(exe), ["mcp"], "installed with uv tool")


# ---------------------------------------------------------------------------
# Step 2: register the MCP server
# ---------------------------------------------------------------------------


def server_env() -> dict[str, str]:
    """Settings the server needs that the agent would not pass on. Never the token."""
    env: dict[str, str] = {}
    for key in (ENV_API_URL, ENV_ALLOW_INSECURE_LOCAL):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def claude_config_file() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home()) / ".claude.json"


def claude_skills_dir() -> Path:
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(base).expanduser() if base else Path.home() / ".claude") / "skills"


def codex_skills_dir() -> Path:
    base = os.environ.get("CODEX_HOME")
    return (Path(base).expanduser() if base else Path.home() / ".codex") / "skills"


def claude_registration() -> dict[str, Any] | None:
    """The user-scope ``nexus`` entry Claude Code holds, read from its config file."""
    path = claude_config_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def codex_registration(runner: Runner, codex: str) -> dict[str, Any] | None:
    result = runner.query([codex, "mcp", "get", SERVER_NAME, "--json"])
    if result is None or result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return None
    transport = data.get("transport") if isinstance(data, dict) else None
    if not isinstance(transport, dict):
        return None
    return {
        "type": transport.get("type"),
        "command": transport.get("command"),
        "args": transport.get("args") or [],
        "env": transport.get("env") or {},
    }


def matches(entry: dict[str, Any] | None, cmd: ServerCommand, env: dict[str, str]) -> bool:
    if not entry:
        return False
    return (
        entry.get("type", "stdio") == "stdio"
        and entry.get("command") == cmd.command
        and list(entry.get("args") or []) == cmd.args
        and dict(entry.get("env") or {}) == env
    )


def register_claude(runner: Runner, claude: str, cmd: ServerCommand) -> str:
    env = server_env()
    existing = claude_registration()
    if matches(existing, cmd, env):
        return "already registered"
    env_args = [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]
    add = [
        claude,
        "mcp",
        "add",
        "--scope",
        "user",
        SERVER_NAME,
        *env_args,
        "--",
        cmd.command,
        *cmd.args,
    ]
    remove = [claude, "mcp", "remove", "--scope", "user", SERVER_NAME]
    if existing is not None:
        result = runner.change(remove)
        if result is not None and result.returncode != 0:
            raise SetupError(
                f"Could not replace the old `{SERVER_NAME}` entry in Claude Code "
                f"({_failure_text(result)}). Run `{shlex.join(remove)}`, then run setup again."
            )
    result = runner.change(add)
    if result is not None and result.returncode != 0:
        if "already exists" in (result.stderr + result.stdout):
            runner.change(remove)
            result = runner.change(add)
        if result is not None and result.returncode != 0:
            raise SetupError(
                f"Claude Code did not accept the server ({_failure_text(result)}). "
                f"Run `{shlex.join(add)}` to see why."
            )
    return "updated" if existing is not None else "registered"


def register_codex(runner: Runner, codex: str, cmd: ServerCommand) -> str:
    env = server_env()
    existing = codex_registration(runner, codex)
    if matches(existing, cmd, env):
        return "already registered"
    env_args = [arg for key, value in env.items() for arg in ("--env", f"{key}={value}")]
    # `codex mcp add` replaces an entry with the same name.
    add = [codex, "mcp", "add", SERVER_NAME, *env_args, "--", cmd.command, *cmd.args]
    result = runner.change(add)
    if result is not None and result.returncode != 0:
        raise SetupError(
            f"Codex did not accept the server ({_failure_text(result)}). "
            f"Run `{shlex.join(add)}` to see why."
        )
    return "updated" if existing is not None else "registered"


# ---------------------------------------------------------------------------
# Step 3: skills
# ---------------------------------------------------------------------------


def packaged_skills_dir() -> Path | None:
    """Skills in the wheel (``nexus_workbench/_skills``), else a source checkout's ``skills/``."""
    here = Path(__file__).resolve().parent
    packaged = here / "_skills"
    if packaged.is_dir():
        return packaged
    repo = here.parent.parent
    if (repo / "pyproject.toml").is_file() and (repo / "skills").is_dir():
        return repo / "skills"
    return None


def list_skills(source: Path | None) -> list[Path]:
    if source is None or not source.is_dir():
        return []
    return sorted(
        p
        for p in source.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / "SKILL.md").is_file()
    )


def _tree(root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != MARKER and "__pycache__" not in path.parts:
            files[path.relative_to(root).as_posix()] = path.read_bytes()
    return files


def _marker_version(directory: Path) -> str | None:
    marker = directory / MARKER
    if not marker.is_file():
        return None
    try:
        return marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def install_skill(runner: Runner, source: Path, target_root: Path) -> str:
    """Copy one skill directory. Returns "installed", "updated", "unchanged" or "collision"."""
    target = target_root / source.name
    existing_version: str | None = None
    if target.exists():
        existing_version = _marker_version(target)
        if existing_version is None:
            return "collision"
        if existing_version == __version__ and _tree(target) == _tree(source):
            return "unchanged"
    if runner.dry_run:
        runner.say(f"  would write: {target}/ (from {source.name}, with {MARKER})")
        return "updated" if existing_version is not None else "installed"
    target_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{source.name}-", dir=target_root))
    try:
        shutil.copytree(
            source, staging, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__")
        )
        (staging / MARKER).write_text(__version__ + "\n", encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        os.replace(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return "updated" if existing_version is not None else "installed"


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


@dataclass
class SetupOptions:
    agent: str = "all"
    no_login: bool = False
    dry_run: bool = False
    yes: bool = False


LoginCallback = Callable[[WorkbenchClient], int]


def _signed_in(api_base: str) -> tuple[bool, str | None]:
    try:
        stored = auth.load_token(api_base)
    except (auth.CredentialsError, auth.InvalidTokenError) as exc:
        return False, str(exc)
    return stored.token is not None, None


def _confirm(out: TextIO, stdin: TextIO) -> bool:
    try:
        interactive = stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if not interactive:
        return True
    print(
        "This adds the Workbench MCP server and skills to your coding agents"
        " and may install nexus-workbench with uv.",
        file=out,
    )
    try:
        answer = input("Continue? [Y/n] ").strip().lower()
    except EOFError:
        return False
    return answer in ("", "y", "yes")


def run_setup(
    options: SetupOptions,
    *,
    login: LoginCallback,
    out: TextIO | None = None,
    err: TextIO | None = None,
    stdin: TextIO | None = None,
    prefix: str | None = None,
) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    stdin = stdin or sys.stdin
    runner = Runner(dry_run=options.dry_run, out=out)
    selected = list(AGENTS) if options.agent == "all" else [options.agent]
    summary: list[str] = []
    failed = False

    if options.dry_run:
        runner.say("Dry run: nothing will be changed.")
    elif not options.yes and not _confirm(out, stdin):
        print("Setup cancelled. Nothing was changed.", file=err)
        return 1

    # Step 1
    runner.say("1. Install")
    try:
        cmd = ensure_persistent_install(runner, prefix=prefix)
    except SetupError as exc:
        print(str(exc), file=err)
        return 1
    runner.say(f"  server command: {shlex.join([cmd.command, *cmd.args])} ({cmd.note})")

    # Step 2
    runner.say("2. Register the MCP server")
    available: list[str] = []
    for agent in selected:
        label = AGENT_LABELS[agent]
        exe = shutil.which(agent)
        if not exe:
            runner.say(f"  {label}: not installed, skipped")
            summary.append(f"{label}: not found, skipped")
            continue
        available.append(agent)
        try:
            if agent == "claude":
                state = register_claude(runner, exe, cmd)
            else:
                state = register_codex(runner, exe, cmd)
        except SetupError as exc:
            print(f"  {label}: {exc}", file=err)
            summary.append(f"{label}: MCP server NOT registered")
            failed = True
            continue
        if options.dry_run and state != "already registered":
            state = f"would be {state}"
        runner.say(f"  {label}: {state}")
        summary.append(f"{label}: MCP server `{SERVER_NAME}` {state} (user scope)")

    if not available:
        names = " or ".join(AGENT_LABELS[a] for a in selected)
        print(
            f"{names} was not found on this computer. Install it, then run setup again.", file=err
        )
        return 1

    # Step 3
    runner.say("3. Install skills")
    skills = list_skills(packaged_skills_dir())
    if not skills:
        runner.say("  no skills packaged")
    for agent in available:
        if not skills:
            break
        label = AGENT_LABELS[agent]
        root = claude_skills_dir() if agent == "claude" else codex_skills_dir()
        counts: dict[str, list[str]] = {}
        for skill in skills:
            try:
                state = install_skill(runner, skill, root)
            except OSError as exc:
                print(
                    f"  {label}: could not install skill {skill.name}: {exc.strerror or exc}",
                    file=err,
                )
                failed = True
                continue
            counts.setdefault(state, []).append(skill.name)
            if state == "collision":
                print(
                    f"  {label}: skipped skill `{skill.name}`: {root / skill.name} already exists "
                    "and was not installed by nexus-workbench. Rename or remove it to get ours.",
                    file=err,
                )
        done = [f"{len(v)} {k}" for k, v in sorted(counts.items())]
        runner.say(f"  {label}: {', '.join(done) or 'nothing to do'} in {root}")
        summary.append(f"{label}: skills in {root} ({', '.join(done)})")

    # Step 4
    runner.say("4. Sign in")
    signed_in = False
    try:
        client = WorkbenchClient()
    except WorkbenchError as exc:
        print(str(exc), file=err)
        return 1
    try:
        signed_in, problem = _signed_in(client.api_base)
        if problem:
            print(f"  {problem}", file=err)
            failed = True
        elif options.no_login:
            runner.say("  skipped (--no-login)")
        elif signed_in:
            runner.say("  already signed in")
        elif options.dry_run:
            runner.say("  would run: nexus-workbench login")
        else:
            if login(client) != 0:
                failed = True
            else:
                signed_in = True
                summary.append("Signed in to Workbench")
    finally:
        client.close()

    runner.say("")
    runner.say("Summary")
    for line in summary:
        runner.say(f"  {line}")
    if options.dry_run:
        runner.say("Dry run finished. Run without --dry-run to do this.")
    elif failed:
        runner.say("Some steps did not finish. Fix the problems above, then run setup again.")
    else:
        first = "Claude Code" if "claude" in available else "Codex"
        if not signed_in:
            runner.say("Next: run `nexus-workbench login` to sign in to Workbench.")
        runner.say(f"Next: open {first} and ask: 'list my Workbench recordings'")
    return 1 if failed else 0
