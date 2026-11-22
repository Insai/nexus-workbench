"""Command line: ``nexus-workbench`` (also installed as ``nexus``).

Exit codes: 0 ok, 1 failure, 2 usage. The version 1.1 commands are in
:mod:`nexus_workbench.commands`.

Error messages name a stable code in brackets, for example ``(not_signed_in)`` (contract
section 3.3). With ``--json`` an error is printed to stderr as
``{"error": {"code": ..., "message": ...}}``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import shlex
import shutil
import sys
import textwrap
import traceback
from collections.abc import Sequence
from typing import Any

from . import __version__, auth
from . import scopes as sc
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

#: Set to 1 to print the traceback of an unexpected error.
ENV_DEBUG = "NEXUS_DEBUG"
DEBUG_HINT = f"Rerun with {ENV_DEBUG}=1 to see the details."
#: Codes for errors that only the command line raises (contract section 3.3).
USAGE_CODE = "usage"
LOCAL_FILE_CODE = "local_file"
LOCAL_UNEXPECTED_CODE = "local_unexpected"
LOGIN_FAILED_CODE = "login_failed"
LOGIN_CANCELLED_CODE = "login_cancelled"


def _err(text: str) -> None:
    # stdout may be buffered (a pipe): flush it first so messages stay in order.
    sys.stdout.flush()
    print(text, file=sys.stderr)


def with_code(code: str, message: str, hint: str = "") -> str:
    """The first sentence of ``message`` with its code in brackets, then the rest and the hint.

    "Not signed in (not_signed_in). Run `nexus-workbench login`, then try again."
    """
    text = f"{message.strip()} {hint.strip()}".strip()
    head, sep, rest = text.partition(". ")
    if not sep:
        head, rest = text, ""
    head = head.rstrip()
    if head.endswith("."):
        head = head[:-1]
    return f"{head} ({code}). {rest}".strip()


def _fail(code: str, message: str, hint: str = "", *, as_json: bool = False) -> None:
    """Print one error on stderr: a line with the code, or a JSON error object."""
    if as_json:
        text = f"{message.strip()} {hint}".strip()
        _err(json.dumps({"error": {"code": code, "message": text}}, ensure_ascii=False))
    else:
        _err(with_code(code, message, hint))


def _fail_workbench(exc: WorkbenchError, *, as_json: bool = False) -> None:
    _fail(exc.code, exc.message, exc.hint, as_json=as_json)


def _debug() -> bool:
    return os.environ.get(ENV_DEBUG, "").strip() not in ("", "0")


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
    scopes: tuple[str, ...] = (sc.READ,),
) -> int:
    """Sign in. ``cancelled`` is returned when no token was given (setup skips the step)."""
    io = LoginIO()
    try:
        result = run_login(
            client, io, token_prompt=token_prompt, use_keyring=use_keyring, scopes=scopes
        )
    except LoginCancelled as exc:
        _fail(LOGIN_CANCELLED_CODE, str(exc))
        return cancelled
    except (LoginFailed, auth.CredentialsError) as exc:
        _fail(LOGIN_FAILED_CODE, str(exc))
        return FAILURE
    except WorkbenchError as exc:
        _fail_workbench(exc)
        return FAILURE
    describe_result(result, io)
    if os.environ.get(auth.ENV_TOKEN):
        _err(f"Note: {auth.ENV_TOKEN} is set, and it is used before the saved token.")
    return OK


def cmd_login(args: argparse.Namespace) -> int:
    use_keyring = bool(args.keyring) or auth.keyring_requested()
    scopes = sc.normalise(args.scope or ())
    with _client() as client:
        return do_login(client, token_prompt=args.token, use_keyring=use_keyring, scopes=scopes)


def _remember_scopes(api_base: str, data: dict[str, Any]) -> None:
    """Save the scopes ``whoami`` reported beside the saved token (contract 7.10)."""
    token = data.get("token") or {}
    scopes = data.get("scopes")
    if not isinstance(token, dict) or not isinstance(token.get("id"), str):
        return
    if not isinstance(scopes, list):
        return
    # The saved scopes only mirror what the server says, so a file that cannot be changed
    # is not an error here.
    with contextlib.suppress(auth.CredentialsError, OSError):
        auth.save_scopes(api_base, token["id"], [s for s in scopes if isinstance(s, str)])


def cmd_logout(args: argparse.Namespace) -> int:
    api_base = resolve_api_base()
    try:
        removed = auth.delete_token(api_base)
    except auth.CredentialsError as exc:
        _fail(LOCAL_FILE_CODE, str(exc))
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
            _fail_workbench(exc, as_json=args.json)
            return FAILURE
        _remember_scopes(client.api_base, data)
    if args.json:
        print(json.dumps(data, indent=2))
        return OK
    workspace = data.get("workspace") or {}
    token = data.get("token") or {}
    scopes = data.get("scopes")
    print(
        f"Workspace: {display_name(workspace.get('name'))} "
        f"({_safe(workspace.get('id')) or 'unknown'})"
    )
    print(f"Role:      {_safe(data.get('role')) or 'unknown'}")
    print(f"Member:    {_safe(data.get('memberId')) or 'unknown'}")
    print(
        "Scopes:    "
        + (
            sc.describe(s for s in scopes if isinstance(s, str))
            if isinstance(scopes, list)
            else "unknown"
        )
    )
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
        _fail(USAGE_CODE, f"--limit must be from 1 to 100, got {args.limit}.", as_json=args.json)
        return USAGE
    with _client() as client:
        try:
            data = client.call(
                "list_recordings", projectId=args.project, limit=args.limit, cursor=args.cursor
            )
        except WorkbenchError as exc:
            _fail_workbench(exc, as_json=args.json)
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
    from .local.sample import FILE_NAMES, approx_edf_bytes, make_sample_night  # noqa: PLC0415

    if not 0.5 <= args.hours <= 24:
        _fail(USAGE_CODE, f"--hours must be from 0.5 to 24, got {args.hours:g}.")
        return USAGE
    if args.seed < 0:
        _fail(USAGE_CODE, f"--seed must be zero or more, got {args.seed}.")
        return USAGE
    existing = [n for n in FILE_NAMES if os.path.lexists(os.path.join(args.out, n))]
    if existing and not args.force:
        more = f" and {len(existing) - 1} other sample file(s)" if len(existing) > 1 else ""
        _fail(
            LOCAL_FILE_CODE,
            f"{existing[0]}{more} already exist in that folder.",
            "Run again with --force to replace them, or pass another folder with --out.",
        )
        return FAILURE
    size = approx_edf_bytes(args.hours) / 1e6
    size_text = f"{size:.0f}" if size >= 10 else f"{size:.1f}"
    unit = "hour" if args.hours == 1 else "hours"
    print(f"Writing a sample night of {args.hours:g} {unit} (about {size_text} MB)…", flush=True)
    try:
        night = make_sample_night(args.out, hours=args.hours, seed=args.seed)
    except OSError as exc:
        _fail(
            LOCAL_FILE_CODE,
            f"Could not write the sample files: {exc.strerror or type(exc).__name__}.",
            "Check that the folder exists and you can write to it, or pass another with --out.",
        )
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
        message = exc.message.replace(api.SHORT_FILE_HINT, "qc needs a recording of 30 s or more.")
        _fail(LOCAL_FILE_CODE, message, as_json=args.json)
        return FAILURE
    except Exception as exc:  # noqa: BLE001  (one plain line; the traceback only on request)
        if _debug():
            traceback.print_exc()
        _fail(
            LOCAL_UNEXPECTED_CODE,
            f"Something went wrong while reading the file: {type(exc).__name__}.",
            f"Check that it is a 16-bit EDF or EDF+ file. {DEBUG_HINT}",
            as_json=args.json,
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
            if stored.scopes is not None:
                print(f"Scopes: {sc.describe(stored.scopes)} (saved with the token)")
            else:
                print("Scopes: not saved with the token; the MCP server lists the read tools only")
            token_ok = True
        else:  # signed out is fine: the local tools need no token
            print(
                "Token: none (local tools work without one). "
                "Run `nexus-workbench login` for the workspace tools."
            )
    except (auth.CredentialsError, auth.InvalidTokenError) as exc:
        print(f"Token: not usable. {exc}")
        ok = False

    if token_ok:
        try:
            with _client() as client:
                data = client.call("whoami")
                _remember_scopes(client.api_base, data)
            workspace = display_name((data.get("workspace") or {}).get("name"))
            expires = _safe((data.get("token") or {}).get("expiresAt")) or "unknown"
            server_scopes = data.get("scopes")
            scope_text = (
                sc.describe(x for x in server_scopes if isinstance(x, str))
                if isinstance(server_scopes, list)
                else "unknown"
            )
            print(
                f"Workbench: signed in to {workspace}, scopes {scope_text}, token expires {expires}"
            )
        except WorkbenchError as exc:
            print(f"Workbench: {with_code(exc.code, exc.message, exc.hint)}")
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
        description=textwrap.fill(
            "Connect Claude Code and Codex to Insai Workbench. Early access. "
            "For research use only. Not for clinical decision-making.",
            79,
        ),
        epilog=(
            "Exit codes: 0 ok, 1 failure, 2 usage. Errors name a code in brackets.\n"
            f"Agent access is in early access: {EARLY_ACCESS_URL}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"nexus-workbench {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    def add(name: str, summary: str, description: str, **kwargs: Any) -> argparse.ArgumentParser:
        return sub.add_parser(
            name,
            help=summary,
            description=textwrap.fill(description, 79),
            formatter_class=argparse.RawDescriptionHelpFormatter,
            **kwargs,
        )

    p = add(
        "setup",
        "add Workbench to Claude Code and Codex (start here)",
        "Install nexus-workbench as a uv tool, register the MCP server `nexus` with Claude "
        "Code and Codex, copy the skills, and sign in. It asks before it changes anything, and "
        "running it again changes nothing that is already in place.",
        epilog=(
            "examples:\n"
            "  uvx nexus-workbench setup             everything, for both agents\n"
            "  uvx nexus-workbench setup --no-login  no account: local tools only\n"
            "  nexus-workbench setup --dry-run       show what would change\n"
            "\n"
            "Then try the local tools:\n"
            "  nexus-workbench sample && nexus-workbench qc nexus-sample/sample_night.edf"
        ),
    )
    p.add_argument(
        "--agent",
        choices=["claude", "codex", "all"],
        default="all",
        help="which agent to set up (default all; an agent that is not installed is skipped)",
    )
    p.add_argument("--no-login", action="store_true", help="do not sign in")
    p.add_argument("--dry-run", action="store_true", help="print what would happen, change nothing")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask before making changes")
    p.set_defaults(func=cmd_setup)

    p = add(
        "login",
        "sign in to Workbench",
        "Sign in to Workbench. It opens Workbench in your browser: approve this computer there, "
        "then type the code it shows. With --token, paste a token made on the Connect an agent "
        "page instead. The token is saved only on this computer, with its scopes. --scope asks "
        "for more than reading; the approver may give fewer.",
        epilog=(
            "scopes (read is always included):\n"
            + "".join(f"  {name:<13} {sc.MEANING[name]}\n" for name in sc.ALL)
            + "\nexamples:\n"
            "  nexus-workbench login\n"
            "  nexus-workbench login --scope detail --scope marks:write"
        ),
    )
    p.add_argument(
        "--scope",
        action="append",
        choices=list(sc.ALL),
        metavar="SCOPE",
        help="ask for this scope too (repeat for more): " + ", ".join(sc.ALL[1:]),
    )
    p.add_argument(
        "--token",
        action="store_true",
        help="paste a token made in Workbench (hidden prompt, or read from stdin when piped)",
    )
    p.add_argument("--keyring", action="store_true", help="store the token in the system keyring")
    p.set_defaults(func=cmd_login)

    p = add(
        "logout",
        "remove the saved token from this computer",
        "Remove the saved token from this computer. The token keeps working until you revoke "
        "it in Workbench (account menu, then Connect an agent).",
    )
    p.set_defaults(func=cmd_logout)

    p = add(
        "whoami",
        "show the workspace this sign-in reads",
        "Show the workspace your token reads, your role and member id, the token's scopes, and "
        "the token's name and expiry. It saves the scopes beside a saved token.",
        epilog="example:\n  nexus-workbench whoami --json",
    )
    p.add_argument("--json", action="store_true", help="print the raw JSON")
    p.set_defaults(func=cmd_whoami)

    p = add(
        "recordings",
        "list recordings",
        "List one page of recordings in your workspace, newest first. Needs sign-in.",
    )
    p.add_argument("--project", help="only this project id")
    p.add_argument("--limit", type=int, default=50, help="rows, 1 to 100 (default 50)")
    p.add_argument("--cursor", help="cursor printed by the previous page")
    p.add_argument("--json", action="store_true", help="print the raw JSON")
    p.set_defaults(func=cmd_recordings)

    p = add(
        "sample",
        "write a synthetic sample night to try the local tools on",
        "Write a synthetic night (an EDF file, two hypnograms and two event files) to try the "
        "local tools on. Nothing in it comes from a recording. An 8 hour night is about 24 MB.",
    )
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

    p = add(
        "qc",
        "describe a local EDF file and check its signal quality",
        "Describe a local EDF or EDF+ file and check its signal quality in 30 s epochs. "
        "The file is read on this computer and nothing is sent anywhere. No account needed.",
        epilog="example:\n  nexus-workbench qc nexus-sample/sample_night.edf",
    )
    p.add_argument("path", help="EDF or EDF+ file on this computer")
    p.add_argument("--json", action="store_true", help="print the raw results as JSON")
    p.set_defaults(func=cmd_qc)

    p = add(
        "doctor",
        "check the install and the sign-in",
        "Check the install, the sign-in and the agent registrations. Being signed out is not "
        "a failure: the local tools work without a token. It exits 1 only when a check fails.",
    )
    p.set_defaults(func=cmd_doctor)

    p = add(
        "mcp",
        "run the MCP server on stdio (agents start this)",
        "Run the MCP server `nexus` on stdio. Claude Code or Codex starts this; you do not "
        "need to run it yourself.",
    )
    p.set_defaults(func=cmd_mcp)

    from .commands import add_parsers  # noqa: PLC0415 (it imports this module)

    add_parsers(add)
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
        _fail_workbench(exc, as_json=bool(getattr(args, "json", False)))
        return FAILURE


if __name__ == "__main__":
    sys.exit(main())
