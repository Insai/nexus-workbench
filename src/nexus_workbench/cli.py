"""Command line: ``nexus-workbench`` (also installed as ``nexus``).

Exit codes: 0 ok, 1 failure, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__, auth
from .client import EARLY_ACCESS_URL, WorkbenchClient, WorkbenchError, resolve_api_base
from .login import LoginFailed, LoginIO, describe_result, run_login

OK, FAILURE, USAGE = 0, 1, 2


def _err(text: str) -> None:
    print(text, file=sys.stderr)


def _client() -> WorkbenchClient:
    return WorkbenchClient()


# ---------------------------------------------------------------------------
# login / logout / whoami
# ---------------------------------------------------------------------------


def do_login(client: WorkbenchClient, *, token_prompt: bool, use_keyring: bool) -> int:
    io = LoginIO()
    try:
        result = run_login(client, io, token_prompt=token_prompt, use_keyring=use_keyring)
    except LoginFailed as exc:
        _err(str(exc))
        return FAILURE
    except auth.CredentialsError as exc:
        _err(str(exc))
        return FAILURE
    except WorkbenchError as exc:
        _err(str(exc))
        return FAILURE
    describe_result(result, io)
    if os.environ.get(auth.ENV_TOKEN):
        _err(f"Note: {auth.ENV_TOKEN} is set, and it is used before the saved token.")
    return OK


def cmd_login(args: argparse.Namespace) -> int:
    use_keyring = bool(args.keyring) or auth.keyring_requested()
    with _client() as client:
        return do_login(client, token_prompt=args.token, use_keyring=use_keyring)


def cmd_logout(args: argparse.Namespace) -> int:
    api_base = resolve_api_base()
    try:
        removed = auth.delete_token(api_base)
    except auth.CredentialsError as exc:
        _err(str(exc))
        return FAILURE
    if removed:
        print("Signed out on this computer.")
        print(
            "The token still works until you revoke it in Workbench: account menu, "
            'then "Connect an agent".'
        )
    else:
        print("No saved sign-in for this Workbench server.")
    if os.environ.get(auth.ENV_TOKEN):
        _err(f"{auth.ENV_TOKEN} is still set in this shell. Unset it to stop using that token.")
    return OK


def cmd_whoami(args: argparse.Namespace) -> int:
    with _client() as client:
        try:
            data = client.call("whoami")
        except WorkbenchError as exc:
            _err(str(exc))
            return FAILURE
    if args.json:
        print(json.dumps(data, indent=2))
        return OK
    workspace = data.get("workspace") or {}
    token = data.get("token") or {}
    print(f"Workspace: {workspace.get('name', '(unnamed)')} ({workspace.get('id', '?')})")
    print(f"Role:      {data.get('role', '?')}")
    print(f"Token:     {token.get('name', '(unnamed)')} ({token.get('id', '?')})")
    print(f"Expires:   {token.get('expiresAt') or 'unknown'}")
    return OK


# ---------------------------------------------------------------------------
# recordings
# ---------------------------------------------------------------------------


def _fmt_duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
        return ""
    minutes = int(round(seconds / 60))
    return f"{minutes // 60}h{minutes % 60:02d}"


def _flags(row: dict[str, Any]) -> str:
    flags = []
    if row.get("held"):
        flags.append("held")
    if row.get("analysing"):
        flags.append("analysing")
    if row.get("machineAvailable"):
        flags.append("staged")
    if row.get("hasEvents"):
        flags.append("events")
    if row.get("pairId"):
        flags.append("paired")
    return ",".join(flags)


def _safe(value: Any) -> str:
    """Show server text as plain text on one line."""
    if value is None:
        return ""
    text = str(value)
    return "".join(ch if ch.isprintable() else " " for ch in text)


def format_recordings_table(rows: list[dict[str, Any]]) -> str:
    headers = ["ID", "PROJECT", "ADDED", "LENGTH", "QUALITY", "DEVICE", "FLAGS"]
    table = [headers]
    for row in rows:
        quality = row.get("qualityGrade") or ""
        table.append(
            [
                _safe(row.get("id")),
                _safe(row.get("projectId") or "-"),
                _safe(row.get("addedAt"))[:16].replace("T", " "),
                _fmt_duration(row.get("durationSec")),
                _safe(quality),
                _safe(row.get("device")),
                _flags(row),
            ]
        )
    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    return "\n".join(
        "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in table
    )


def cmd_recordings(args: argparse.Namespace) -> int:
    if not 1 <= args.limit <= 100:
        _err("--limit must be between 1 and 100.")
        return USAGE
    with _client() as client:
        try:
            data = client.call(
                "list_recordings", projectId=args.project, limit=args.limit, cursor=args.cursor
            )
        except WorkbenchError as exc:
            _err(str(exc))
            return FAILURE
    if args.json:
        print(json.dumps(data, indent=2))
        return OK
    rows = data.get("recordings") or []
    if not rows:
        print("No recordings.")
    else:
        print(format_recordings_table(rows))
    if data.get("truncated"):
        _err("The list is incomplete: the workspace holds more recordings than one pass reads.")
    if data.get("nextCursor"):
        print(f"More recordings: run again with --cursor {data['nextCursor']}")
    return OK


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import setup_cmd  # noqa: PLC0415

    ok = True
    print(f"nexus-workbench {__version__}")
    print(f"Python {platform.python_version()} ({sys.executable})")
    try:
        api_base = resolve_api_base()
    except WorkbenchError as exc:
        print(f"API base: not usable. {exc}")
        return FAILURE
    print(f"API base: {api_base}")

    token_ok = False
    try:
        stored = auth.load_token(api_base)
        if stored.token:
            print(f"Token: from {stored.source} ({stored.masked})")
            token_ok = True
        else:
            print("Token: none. Run `nexus-workbench login`.")
            ok = False
    except (auth.CredentialsError, auth.InvalidTokenError) as exc:
        print(f"Token: not usable. {exc}")
        ok = False

    if token_ok:
        try:
            with _client() as client:
                data = client.call("whoami")
            workspace = (data.get("workspace") or {}).get("name", "(unnamed)")
            expires = (data.get("token") or {}).get("expiresAt") or "unknown"
            print(f"Workbench: signed in to {_safe(workspace)}, token expires {expires}")
        except WorkbenchError as exc:
            print(f"Workbench: {exc}")
            ok = False

    runner = setup_cmd.Runner(dry_run=True)
    for agent in setup_cmd.AGENTS:
        label = setup_cmd.AGENT_LABELS[agent]
        exe = shutil.which(agent)
        if not exe:
            print(f"{label}: not found on PATH")
            continue
        if agent == "claude":
            entry = setup_cmd.claude_registration()
        else:
            entry = setup_cmd.codex_registration(runner, exe)
        if entry:
            command = " ".join(
                [str(entry.get("command"))] + [str(a) for a in entry.get("args") or []]
            )
            print(f"{label}: {exe}; MCP server `nexus` registered ({command})")
        else:
            print(
                f"{label}: {exe}; MCP server `nexus` not registered. Run `nexus-workbench setup`."
            )
    if not ok:
        _err("Some checks failed. See above.")
    return OK if ok else FAILURE


# ---------------------------------------------------------------------------
# setup / mcp
# ---------------------------------------------------------------------------


def cmd_setup(args: argparse.Namespace) -> int:
    from . import setup_cmd  # noqa: PLC0415

    options = setup_cmd.SetupOptions(
        agent=args.agent, no_login=args.no_login, dry_run=args.dry_run, yes=args.yes
    )

    def login(client: WorkbenchClient) -> int:
        return do_login(client, token_prompt=False, use_keyring=auth.keyring_requested())

    return setup_cmd.run_setup(options, login=login)


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import run_stdio  # noqa: PLC0415

    run_stdio()
    return OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nexus-workbench",
        description=(
            "Connect Claude Code and Codex to Insai Workbench. Early access. "
            "For research use only. Not for clinical decision-making."
        ),
        epilog=f"Agent access is in early access: {EARLY_ACCESS_URL}",
    )
    parser.add_argument("--version", action="version", version=f"nexus-workbench {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("setup", help="add Workbench to Claude Code and Codex (start here)")
    p.add_argument("--agent", choices=["claude", "codex", "all"], default="all")
    p.add_argument("--no-login", action="store_true", help="do not sign in")
    p.add_argument("--dry-run", action="store_true", help="print what would happen, change nothing")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask before making changes")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("login", help="sign in to Workbench")
    p.add_argument(
        "--token",
        action="store_true",
        help="paste a token made in Workbench (hidden prompt, or read from stdin when piped)",
    )
    p.add_argument("--keyring", action="store_true", help="store the token in the system keyring")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser("logout", help="remove the saved token from this computer")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("whoami", help="show the workspace this sign-in reads")
    p.add_argument("--json", action="store_true", help="print the raw JSON")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("recordings", help="list recordings")
    p.add_argument("--project", help="only this project id")
    p.add_argument("--limit", type=int, default=50, help="rows, 1 to 100 (default 50)")
    p.add_argument("--cursor", help="cursor printed by the previous page")
    p.add_argument("--json", action="store_true", help="print the raw JSON")
    p.set_defaults(func=cmd_recordings)

    p = sub.add_parser("doctor", help="check the install and the sign-in")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("mcp", help="run the MCP server on stdio (agents start this)")
    p.set_defaults(func=cmd_mcp)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return USAGE
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        _err("Stopped.")
        return FAILURE
    except WorkbenchError as exc:
        _err(str(exc))
        return FAILURE


if __name__ == "__main__":
    sys.exit(main())
