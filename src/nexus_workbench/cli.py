"""Command line: ``nexus-workbench`` (also installed as ``nexus``).

Exit codes: 0 ok, 1 failure, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import shutil
import sys
from collections.abc import Sequence
from typing import Any

from . import __version__, auth
from .client import (
    EARLY_ACCESS_URL,
    WorkbenchClient,
    WorkbenchError,
    display_name,
    resolve_api_base,
)
from .client import safe_text as _safe
from .login import LoginCancelled, LoginFailed, LoginIO, describe_result, run_login

OK, FAILURE, USAGE = 0, 1, 2


def _err(text: str) -> None:
    # stdout may be buffered (a pipe): flush it first so messages stay in order.
    sys.stdout.flush()
    print(text, file=sys.stderr)


def _client() -> WorkbenchClient:
    return WorkbenchClient()


# ---------------------------------------------------------------------------
# login / logout / whoami
# ---------------------------------------------------------------------------


def do_login(
    client: WorkbenchClient,
    *,
    token_prompt: bool,
    use_keyring: bool,
    cancelled: int = FAILURE,
) -> int:
    """Sign in. ``cancelled`` is returned when no token was given (setup skips the step)."""
    io = LoginIO()
    try:
        result = run_login(client, io, token_prompt=token_prompt, use_keyring=use_keyring)
    except LoginCancelled as exc:
        _err(str(exc))
        return cancelled
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
    print(
        f"Workspace: {display_name(workspace.get('name'))} "
        f"({_safe(workspace.get('id')) or 'unknown'})"
    )
    print(f"Role:      {_safe(data.get('role')) or 'unknown'}")
    print(f"Token:     {display_name(token.get('name'))} ({_safe(token.get('id')) or 'unknown'})")
    print(f"Expires:   {_safe(token.get('expiresAt')) or 'unknown'}")
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
        print(f"More recordings: run again with --cursor {_safe(data['nextCursor'], limit=None)}")
    return OK


# ---------------------------------------------------------------------------
# local files: sample / qc
# ---------------------------------------------------------------------------


def cmd_sample(args: argparse.Namespace) -> int:
    from .local.sample import FILE_NAMES, make_sample_night  # noqa: PLC0415

    if not 0.5 <= args.hours <= 24:
        _err("--hours must be between 0.5 and 24.")
        return USAGE
    if args.seed < 0:
        _err("--seed must be zero or more.")
        return USAGE
    existing = [n for n in FILE_NAMES if os.path.lexists(os.path.join(args.out, n))]
    if existing and not args.force:
        more = f" and {len(existing) - 1} other sample file(s)" if len(existing) > 1 else ""
        _err(
            f"{existing[0]}{more} already exist in that folder. Run again with --force to "
            "replace them, or pass another folder with --out."
        )
        return FAILURE
    try:
        night = make_sample_night(args.out, hours=args.hours, seed=args.seed)
    except OSError as exc:
        _err(f"Could not write the sample files: {exc.strerror or type(exc).__name__}.")
        return FAILURE
    print(f"Wrote a synthetic {args.hours:g} h night (seed {args.seed}) to {args.out}:")
    for key in ("edf", "hypnogram", "hypnogramScorer2", "eventsReference", "eventsCandidate"):
        print(f"  {night[key]}")
    print(f"Try: nexus-workbench qc {shlex.quote(night['edf'])}")
    return OK


def _pct(value: Any) -> str:
    return "-" if value is None else f"{value:g}%"


def format_qc(info: dict[str, Any], quality: dict[str, Any]) -> str:
    lines = [
        f"File:      {info['format']}, {_fmt_duration(info['durationSec'])}, "
        f"{info['nChannels']} channels, id {info['recordingId']}",
        f"Epochs:    {quality['nEpochs']} of {quality['epochSec']:g} s",
        f"Usable:    {_pct(quality['usablePercent'])} of channel epochs are ok; "
        f"{_pct(quality['allChannelsOkPercent'])} of epochs are ok on every channel",
        "",
    ]
    headers = ["#", "LABEL", "TYPE", "HZ", "OVERALL", "OK", "SUSPECT", "FLAT", "RAILED"]
    rates = {ch["index"]: ch["sampleRateHz"] for ch in info["channels"]}
    table = [headers]
    for ch in quality["channels"]:
        pct = ch["percent"]
        rate = rates.get(ch["index"])
        table.append(
            [
                str(ch["index"]),
                ch["label"],
                ch["type"],
                "" if rate is None else f"{rate:g}",
                ch["overall"],
                *(_pct(pct.get(k)) for k in ("ok", "suspect", "flat", "railed")),
            ]
        )
    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    lines += ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in table]
    for skipped in quality["skipped"]:
        lines.append(f"Skipped {skipped['label']}: {skipped['reason']}")
    spans = quality["badSpans"]
    if spans:
        lines += ["", f"Longest bad stretches ({len(spans)} of {quality['badSpansTotal']}):"]
        lines += [
            f"  {s['channel']}: {s['class']} from {s['startSec']:g} s to {s['endSec']:g} s"
            for s in spans[:5]
        ]
    return "\n".join(lines)


def cmd_qc(args: argparse.Namespace) -> int:
    from .local import api  # noqa: PLC0415
    from .local.errors import LocalToolError  # noqa: PLC0415

    try:
        info = api.inspect_edf(args.path)
        quality = api.check_signal_quality(args.path)
    except LocalToolError as exc:
        # qc always uses 30 s epochs, so the tools' epoch_sec advice does not apply here.
        _err(exc.message.replace(api.SHORT_FILE_HINT, "qc needs a recording of 30 s or more."))
        return FAILURE
    except Exception as exc:  # noqa: BLE001  (one plain line, never a traceback)
        _err(
            "Something went wrong while reading the file "
            f"({type(exc).__name__}). Check that it is a 16-bit EDF or EDF+ file."
        )
        return FAILURE
    if args.json:
        print(json.dumps({"inspect_edf": info, "check_signal_quality": quality}, indent=2))
        return OK
    print(format_qc(info, quality))
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
            workspace = display_name((data.get("workspace") or {}).get("name"))
            expires = _safe((data.get("token") or {}).get("expiresAt")) or "unknown"
            print(f"Workbench: signed in to {workspace}, token expires {expires}")
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
        _err("Some checks failed. See above.")  # after the report, even when piped
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
        return do_login(
            client,
            token_prompt=False,
            use_keyring=auth.keyring_requested(),
            cancelled=setup_cmd.LOGIN_SKIPPED,
        )

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

    p = sub.add_parser("sample", help="write a synthetic sample night to try the local tools on")
    p.add_argument(
        "--out", default="nexus-sample", help="folder to write to (default ./nexus-sample)"
    )
    p.add_argument("--hours", type=float, default=8.0, help="length, 0.5 to 24 (default 8)")
    p.add_argument("--seed", type=int, default=0, help="random seed (default 0)")
    p.add_argument(
        "--force",
        action="store_true",
        help="replace sample files already in the folder (without it, existing files are "
        "left alone and the command stops)",
    )
    p.set_defaults(func=cmd_sample)

    p = sub.add_parser("qc", help="describe a local EDF file and check its signal quality")
    p.add_argument("path", help="EDF or EDF+ file on this computer")
    p.add_argument("--json", action="store_true", help="print the raw results as JSON")
    p.set_defaults(func=cmd_qc)

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
