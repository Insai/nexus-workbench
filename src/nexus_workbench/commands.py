"""Version 1.1 commands (contract section 7) and ``preprocess``.

Each mirrors one MCP tool. Every one takes ``--json``; errors carry their code as in
:mod:`nexus_workbench.cli`. Exit codes: 0 ok, 1 failure, 2 usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from collections.abc import Callable
from typing import Any

from . import bridge, cli
from . import marks as mk
from .client import WorkbenchClient, WorkbenchError
from .client import safe_text as _safe
from .tables import render

STAGES = ("W", "N1", "N2", "N3", "R", "?")
STAGE_NAMES = {"?": "unscored"}
QUALITY_LETTERS = (("g", "good"), ("m", "marginal"), ("b", "bad"), ("?", "not graded"))
REFUSAL_WHY = {
    "not_found": "not in this workspace, or you cannot see it",
    "open_task": "the member already has an open task on it",
    "no_source": "its source is not known, so it cannot be given blind",
    "staged": "the member already staged it with the machine visible",
    "machine_seen": "member saw machine output before; --confirm-machine-seen",
}
WITHHELD = (
    "Withheld: you are blinded on this recording, so the machine output is not shown. "
    "That is by design."
)


# ---------------------------------------------------------------------------- plumbing


def _out(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _workspace(args: argparse.Namespace, call: Callable[[WorkbenchClient], Any]) -> Any:
    """Run ``call`` with a client. On a Workbench error print it and return None."""
    with cli._client() as client:
        try:
            return call(client)
        except WorkbenchError as exc:
            cli._fail_workbench(exc, as_json=args.json)
            return None


def _usage(args: argparse.Namespace, message: str) -> int:
    cli._fail(cli.USAGE_CODE, message, as_json=getattr(args, "json", False))
    return cli.USAGE


def _limit(args: argparse.Namespace, most: int) -> bool:
    if args.limit is not None and not 1 <= args.limit <= most:
        _usage(args, f"--limit must be from 1 to {most}, got {args.limit}.")
        return False
    return True


def _more(data: dict[str, Any], what: str) -> None:
    cursor = data.get("nextCursor")
    if cursor:
        print(f"More {what}: run again with --cursor {_safe(cursor, limit=None)}")


def _secs(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    return f"{value:.1f}" if value != int(value) else f"{int(value)}"


# ---------------------------------------------------------------------------- reads


def cmd_members(args: argparse.Namespace) -> int:
    data = _workspace(args, lambda c: c.list_members())
    if data is None:
        return cli.FAILURE
    if args.json:
        _out(data)
        return cli.OK
    rows = data.get("members") or []
    if not rows:
        print("No members.")
        return cli.OK
    print(
        render(
            ["MEMBER", "ROLE", ""],
            [[m.get("id"), m.get("role"), "you" if m.get("you") else ""] for m in rows],
        )
    )
    return cli.OK


def cmd_tasks(args: argparse.Namespace) -> int:
    data = _workspace(args, lambda c: c.list_tasks(mine=not args.all))
    if data is None:
        return cli.FAILURE
    if args.json:
        _out(data)
        return cli.OK
    rows = data.get("tasks") or []
    if not rows:
        print(
            "No tasks."
            if args.all
            else "No tasks given to you. --all also shows the ones you gave."
        )
        return cli.OK
    print(
        render(
            ["TASK", "RECORDING", "MODE", "STATE", "GIVEN", "DONE", "TO", "BY"],
            [
                [
                    t.get("id"),
                    t.get("recordingId"),
                    t.get("mode"),
                    t.get("state"),
                    _safe(t.get("givenAt"))[:16].replace("T", " "),
                    _safe(t.get("doneAt"))[:16].replace("T", " "),
                    t.get("assignee"),
                    t.get("givenBy"),
                ]
                for t in rows
            ],
        )
    )
    return cli.OK


def cmd_nights(args: argparse.Namespace) -> int:
    data = _workspace(args, lambda c: c.list_nights(project_id=args.project))
    if data is None:
        return cli.FAILURE
    if args.json:
        _out(data)
        return cli.OK
    rows = data.get("nights") or []
    if not rows:
        print("No nights. A night is two or more recordings recorded together.")
    else:
        print(
            render(
                ["NIGHT", "RECORDINGS", "PROJECT", "PERSON", "IDS"],
                [
                    [
                        n.get("id"),
                        len(n.get("recordingIds") or []),
                        n.get("projectId"),
                        n.get("personKey"),
                        ", ".join(_safe(r) for r in n.get("recordingIds") or []),
                    ]
                    for n in rows
                ],
                right=[1],
            )
        )
    if data.get("truncated"):
        cli._err("The list is incomplete: the workspace holds more recordings than one pass reads.")
    return cli.OK


def cmd_layers(args: argparse.Namespace) -> int:
    data = _workspace(args, lambda c: c.list_layers(project_id=args.project))
    if data is None:
        return cli.FAILURE
    if args.json:
        _out(data)
        return cli.OK
    rows = []
    for layer in data.get("layers") or []:
        recs = layer.get("recordings") or []

        def count(state: str, recs: list[Any] = recs) -> int:
            return sum(1 for r in recs if isinstance(r, dict) and r.get("state") == state)

        hidden = sum(1 for r in recs if isinstance(r, dict) and r.get("hidden"))
        total_rows = sum(r.get("rows") or 0 for r in recs if isinstance(r, dict))
        rows.append(
            [
                layer.get("source"),
                layer.get("origin"),
                layer.get("status"),
                count("have"),
                count("notRun"),
                count("missing"),
                hidden,
                total_rows,
            ]
        )
    if not rows:
        print("No layers.")
    else:
        print(
            render(
                ["LAYER", "ORIGIN", "STATUS", "HAVE", "NOT RUN", "MISSING", "HIDDEN", "ROWS"],
                rows,
                right=[3, 4, 5, 6, 7],
            )
        )
    if data.get("truncated"):
        cli._err("The list is incomplete: the workspace holds more recordings than one pass reads.")
    return cli.OK


def _save_text(args: argparse.Namespace, path: str, text: str, what: str) -> int:
    if os.path.lexists(path):
        cli._fail(
            cli.LOCAL_FILE_CODE,
            f"{os.path.basename(path)} already exists.",
            "Pass another file to --save; nothing is replaced.",
            as_json=args.json,
        )
        return cli.FAILURE
    try:
        with open(path, "x", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        cli._fail(
            cli.LOCAL_FILE_CODE,
            f"Could not write {os.path.basename(path)}: {exc.strerror or type(exc).__name__}.",
            as_json=args.json,
        )
        return cli.FAILURE
    print(f"Saved {what} to {path}")
    return cli.OK


def cmd_events(args: argparse.Namespace) -> int:
    if not _limit(args, 1000):
        return cli.USAGE
    if args.save:
        return _save_events(args)
    data = _workspace(
        args,
        lambda c: c.get_events(args.id, kind=args.kind, cursor=args.cursor, limit=args.limit),
    )
    if data is None:
        return cli.FAILURE
    if args.json:
        _out(data)
        return cli.OK
    if data.get("hidden"):
        print(WITHHELD)
        return cli.OK
    if not data.get("available"):
        print("This recording has no machine events.")
        return cli.OK
    rows = data.get("events") or []
    if not rows and not args.cursor and not data.get("nextCursor"):
        # The kinds can come from counts that are all zero: the analysis found none.
        what = f"{_safe(args.kind)} events" if args.kind else "events"
        print(f"No {what} on this recording.")
        return cli.OK
    kinds = ", ".join(_safe(k) for k in data.get("kinds") or []) or "none"
    print(f"Kinds: {kinds}")
    if rows:
        print(
            render(
                ["ONSET S", "DURATION S", "KIND", "CHANNEL", "CONFIDENCE"],
                [
                    [
                        f"{e.get('onsetSec', 0):.3f}",
                        f"{e.get('durationSec', 0):.3f}",
                        e.get("kind"),
                        e.get("channel"),
                        e.get("confidence"),
                    ]
                    for e in rows
                ],
                right=[0, 1, 3, 4],
            )
        )
    else:
        print("No events on this page.")
    _more(data, "events")
    return cli.OK


def _save_events(args: argparse.Namespace) -> int:
    lower = args.save.lower()
    if not lower.endswith((".csv", ".json")):
        return _usage(args, "--save must name a .csv or .json file.")
    try:
        rows = _workspace(args, lambda c: bridge.fetch_events(c, args.id, kind=args.kind))
    except bridge.BridgeError as exc:
        print(f"Nothing was saved. {exc}")
        return cli.OK
    if rows is None:
        return cli.FAILURE
    records = bridge.events_records(rows)
    text = (
        bridge.events_json(records) + "\n"
        if lower.endswith(".json")
        else bridge.events_csv(records)
    )
    return _save_text(args, args.save, text, f"{len(records)} machine events")


def _stage_summary(codes: list[str], epoch: float) -> list[list[Any]]:
    n = len(codes) or 1
    rows = []
    for stage in STAGES:
        k = codes.count(stage)
        rows.append(
            [STAGE_NAMES.get(stage, stage), k, f"{k * epoch / 60:.1f}", f"{100 * k / n:.1f}%"]
        )
    return rows


def cmd_stages(args: argparse.Namespace) -> int:
    data = _workspace(args, lambda c: c.get_stages(args.id, source=args.source))
    if data is None:
        return cli.FAILURE
    if args.save:
        try:
            text, _ = bridge.stages_text(data)
        except bridge.BridgeError as exc:
            print(f"Nothing was saved. {exc}")
            return cli.OK
        return _save_text(args, args.save, text, f"{args.source} stages")
    if args.json:
        _out(data)
        return cli.OK
    if data.get("hidden"):
        print(WITHHELD)
        return cli.OK
    if not data.get("available"):
        print(
            "You have not scored this recording."
            if args.source == "mine"
            else "This recording has no machine staging."
        )
        return cli.OK
    try:
        codes = bridge.split_stages(data.get("stages") or "")
    except bridge.BridgeError as exc:
        cli._fail("unexpected_response", str(exc))
        return cli.FAILURE
    epoch = float(data.get("epochSec") or 30)
    what = f"machine staging, model {_safe(data.get('model')) or 'unknown'}"
    if args.source == "mine":
        what = f"your scoring, {_safe(data.get('status')) or 'status unknown'}"
    print(f"Stages: {what}; {len(codes)} epochs of {epoch:g} s")
    print(
        render(
            ["STAGE", "EPOCHS", "MINUTES", "SHARE"], _stage_summary(codes, epoch), right=[1, 2, 3]
        )
    )
    if args.source == "mine":
        print(
            f"Flagged questionable: {len(data.get('questionable') or [])} epochs; "
            f"no signal: {len(data.get('noSignal') or [])} epochs"
        )
    return cli.OK


def cmd_quality(args: argparse.Namespace) -> int:
    data = _workspace(args, lambda c: c.get_quality(args.id))
    if data is None:
        return cli.FAILURE
    if args.json:
        _out(data)
        return cli.OK
    if not data.get("available"):
        print("This recording was never graded.")
        return cli.OK
    per = data.get("perEpoch") if isinstance(data.get("perEpoch"), str) else ""
    counts = ", ".join(f"{name} {per.count(letter)}" for letter, name in QUALITY_LETTERS)
    score = data.get("score")
    print(
        f"Quality: {_safe(data.get('grade')) or 'unknown'}"
        + (f" (score {score:g})" if isinstance(score, (int, float)) else "")
        + f"; {len(per)} epochs of {float(data.get('epochSec') or 30):g} s: {counts}"
    )
    rows = []
    for ch in data.get("channels") or []:
        shares = ch.get("shares") or {}

        def pct(key: str, shares: dict[str, Any] = shares) -> str:
            v = shares.get(key)
            return "-" if not isinstance(v, (int, float)) else f"{100 * v:.0f}%"

        rows.append(
            [
                ch.get("index"),
                ch.get("grade"),
                ch.get("score"),
                pct("ok"),
                pct("suspect"),
                pct("flat"),
                pct("railed"),
                ch.get("excluded"),
            ]
        )
    if rows:
        print(
            render(
                ["CH", "GRADE", "SCORE", "OK", "SUSPECT", "FLAT", "RAILED", "EXCLUDED"],
                rows,
                right=[0, 2, 3, 4, 5, 6],
            )
        )
    return cli.OK


def cmd_marks(args: argparse.Namespace) -> int:
    if not _limit(args, 500):
        return cli.USAGE
    data = _workspace(
        args,
        lambda c: c.list_marks(args.id, origin=args.origin, cursor=args.cursor, limit=args.limit),
    )
    if data is None:
        return cli.FAILURE
    if args.json:
        _out(data)
        return cli.OK
    rows = data.get("marks") or []
    if rows:
        print(
            render(
                [
                    "MARK",
                    "ORIGIN",
                    "KIND",
                    "T0 S",
                    "T1 S",
                    "CHANNELS",
                    "LABEL",
                    "AGREE",
                    "DISAGREE",
                ],
                [
                    [
                        m.get("id"),
                        m.get("origin"),
                        m.get("kind"),
                        _secs(m.get("t0")),
                        _secs(m.get("t1")),
                        ",".join(str(c) for c in m.get("channels") or []) or "all",
                        m.get("label"),
                        (m.get("verdicts") or {}).get("agree"),
                        (m.get("verdicts") or {}).get("disagree"),
                    ]
                    for m in rows
                ],
                right=[3, 4, 7, 8],
            )
        )
    else:
        print("No marks.")
    if data.get("hidden"):
        print("Machine and agent marks are withheld: you are blinded on this recording.")
    _more(data, "marks")
    return cli.OK


# ---------------------------------------------------------------------------- writes


def cmd_propose(args: argparse.Namespace) -> int:
    if args.op_id is not None and not mk.OP_ID.match(args.op_id):
        return _usage(args, "--op-id must be 1 to 64 characters from 0-9, A-Z, a-z, _ and -.")
    try:
        marks = mk.read_marks_file(args.file)
    except mk.MarksError as exc:
        cli._fail(cli.LOCAL_FILE_CODE, str(exc), "Nothing was sent.", as_json=args.json)
        return cli.FAILURE
    calls = mk.batches(marks)
    if args.dry_run:
        if args.json:
            _out({"valid": len(marks), "calls": len(calls), "sent": False})
        else:
            noun = "call" if len(calls) == 1 else "calls"
            print(
                f"{len(marks)} marks are valid. They would go in {len(calls)} {noun}. "
                "Nothing was sent."
            )
        return cli.OK
    results: list[dict[str, Any]] = []
    sent = 0
    with cli._client() as client:
        for k, batch in enumerate(calls, start=1):
            op = f"{args.op_id}-{k}" if args.op_id else mk.op_id_for(args.id, batch)
            try:
                result = client.propose_marks(args.id, batch, op_id=op[:64])
            except WorkbenchError as exc:
                if sent:
                    cli._err(
                        f"{sent} of {len(marks)} marks were sent before this error. Running the "
                        "same command again within 24 hours sends only the rest."
                    )
                cli._fail_workbench(exc, as_json=args.json)
                return cli.FAILURE
            results.append(result)
            sent += len(batch)
    accepted = sum(int(r.get("accepted") or 0) for r in results)
    last = results[-1] if results else {}
    summary = {
        "source": last.get("source"),
        "accepted": accepted,
        "total": last.get("total"),
        "ids": [i for r in results for i in r.get("ids") or []],
    }
    if args.json:
        _out(summary)
        return cli.OK
    print(
        f"Proposed {accepted} marks on {_safe(args.id)} as layer {_safe(summary['source'])}; "
        f"{summary['total']} live marks in the layer."
    )
    print("They are proposals: members review each one on the desk. Nothing else changed.")
    return cli.OK


def cmd_withdraw(args: argparse.Namespace) -> int:
    if not args.mark and not args.yes:
        if sys.stdin.isatty():
            answer = input(f"Withdraw every mark this token proposed on {_safe(args.id)}? [y/N] ")
            if answer.strip().lower() not in ("y", "yes"):
                print("Nothing was withdrawn.")
                return cli.OK
        else:
            return _usage(args, "Name marks with --mark, or pass --yes to withdraw them all.")
    data = _workspace(args, lambda c: c.withdraw_marks(args.id, mark_ids=args.mark or None))
    if data is None:
        return cli.FAILURE
    if args.json:
        _out(data)
        return cli.OK
    print(
        f"Withdrew {data.get('withdrawn')} marks; {data.get('total')} live marks left in the layer."
    )
    return cli.OK


def _split_ids(values: list[str]) -> list[str]:
    return [p for v in values for p in re.split(r"[,\s]+", v) if p]


def cmd_task_give(args: argparse.Namespace) -> int:
    ids = _split_ids(args.ids)
    if not 1 <= len(ids) <= 100:
        return _usage(args, f"Give 1 to 100 recording ids, got {len(ids)}.")
    if len(set(ids)) != len(ids):
        return _usage(args, "A recording id is listed twice.")
    if not re.match(r"^m_[0-9a-f]{16}$", args.member):
        return _usage(args, "--member must be a member id like m_0123456789abcdef (see `members`).")
    data = _workspace(
        args,
        lambda c: c.create_review_task(
            ids,
            member_id=args.member,
            mode=args.mode,
            confirm_machine_seen=args.confirm_machine_seen,
        ),
    )
    if data is None:
        return cli.FAILURE
    given = data.get("given") or []
    refused = data.get("refused") or []
    if args.json:
        _out(data)
    else:
        if given:
            print(
                f"Gave {_safe(args.member)} {len(given)} {args.mode} "
                f"task{'s' if len(given) != 1 else ''} (batch {_safe(data.get('batchId'))}): "
                + ", ".join(_safe(g) for g in given)
            )
        else:
            print("No task was given.")
        mailed = data.get("mailed")
        if given and mailed is True:
            print("Workbench emailed the member about the new tasks.")
        elif given and mailed is False:
            print(
                "Workbench sent no email this time (at most one a minute and five an hour per "
                "member, and none for tasks you give yourself). The tasks are on the member's "
                "Tasks page either way."
            )
        if refused:
            print(
                render(
                    ["REFUSED", "CODE", "WHY"],
                    [
                        [r.get("id"), r.get("code"), REFUSAL_WHY.get(str(r.get("code")), "")]
                        for r in refused
                    ],
                )
            )
    return cli.OK if given or not refused else cli.FAILURE


def cmd_analyse(args: argparse.Namespace) -> int:
    data = _workspace(args, lambda c: c.request_analysis(args.id))
    if data is None:
        return cli.FAILURE
    if args.json:
        _out(data)
        return cli.OK
    if data.get("status") == "already":
        print(f"An analysis of {_safe(args.id)} is already pending. Nothing new was started.")
    else:
        print(
            f"Analysis started for {_safe(args.id)}. `recordings` shows it as analysing, then "
            "staged when it is done."
        )
    return cli.OK


# ---------------------------------------------------------------------------- preprocess


def cmd_preprocess(args: argparse.Namespace) -> int:
    from .local import api  # noqa: PLC0415
    from .local.errors import LocalToolError  # noqa: PLC0415

    channels = _split_ids(args.channels) if args.channels else None
    parsed: list[str | int] | None = None
    if channels is not None:
        parsed = [int(c) if c.isdigit() else c for c in channels]
    try:
        result = api.preprocess(
            args.path,
            low_hz=args.low,
            high_hz=args.high,
            notch_hz=args.notch,
            resample_hz=args.resample,
            channels=parsed,
            out=args.out,
        )
    except LocalToolError as exc:
        cli._fail(cli.LOCAL_FILE_CODE, exc.message, as_json=args.json)
        return cli.FAILURE
    except Exception as exc:  # noqa: BLE001
        if cli._debug():
            traceback.print_exc()
        cli._fail(
            cli.LOCAL_UNEXPECTED_CODE,
            f"Something went wrong while preprocessing the file: {type(exc).__name__}.",
            f"Check that it is a 16-bit EDF or EDF+ file. {cli.DEBUG_HINT}",
            as_json=args.json,
        )
        return cli.FAILURE
    if args.json:
        _out(result)
        return cli.OK
    f = result["filters"]
    steps = []
    if f["lowHz"] is not None or f["highHz"] is not None:
        lo = f"{f['lowHz']:g}" if f["lowHz"] is not None else "0"
        hi = f"{f['highHz']:g}" if f["highHz"] is not None else "Nyquist"
        steps.append(f"band-pass {lo}-{hi} Hz")
    if f["notchHz"] is not None:
        steps.append(f"notch {f['notchHz']:g} Hz")
    if f["resampleHz"] is not None:
        steps.append(f"resample to {f['resampleHz']:g} Hz")
    print(
        f"Wrote {result['output']} (id {result['outputId']}), "
        f"{cli._fmt_duration(result['durationSec'])}"
    )
    print(f"Applied: {', '.join(steps) or 'channel choice only'}. The input is unchanged.")
    print(
        render(
            ["#", "LABEL", "TYPE", "HZ IN", "HZ OUT", "FILTERED", "CLIPPED"],
            [
                [
                    ch["index"],
                    ch["label"],
                    ch["type"],
                    ch["sampleRateHz"],
                    ch["outputSampleRateHz"],
                    ch["filtered"],
                    ch["clippedSamples"],
                ]
                for ch in result["channels"]
            ],
            right=[0, 3, 4, 6],
        )
    )
    if result["channelsLeftOut"]:
        print(f"Left out {result['channelsLeftOut']} channels.")
    print("Annotations and the header's identity fields were not copied.")
    return cli.OK


# ---------------------------------------------------------------------------- parser


def add_parsers(add: Callable[..., argparse.ArgumentParser]) -> None:
    """Add the commands to the main parser. ``add`` is :func:`cli.build_parser`'s helper."""

    def json_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="print the raw JSON")

    def project_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument("--project", help="only this project id")

    p = add(
        "members",
        "list the workspace's members (opaque ids)",
        "List the workspace's active people as opaque member ids with their role. No names "
        "or emails. Use an id with `task give`.",
        epilog="example:\n  nexus-workbench members",
    )
    json_flag(p)
    p.set_defaults(func=cmd_members)

    p = add(
        "tasks",
        "list review tasks given to you",
        "List the review tasks given to you, newest first. With --all, also the ones you gave.",
        epilog="examples:\n  nexus-workbench tasks\n  nexus-workbench tasks --all --json",
    )
    p.add_argument("--all", action="store_true", help="also the tasks you gave others")
    json_flag(p)
    p.set_defaults(func=cmd_tasks)

    p = add(
        "nights",
        "list nights (recordings recorded together)",
        "List nights: two or more recordings recorded together, with the project and an "
        "opaque key for the person who wore the device when the recordings agree on one.",
        epilog="example:\n  nexus-workbench nights --project p_123",
    )
    project_flag(p)
    json_flag(p)
    p.set_defaults(func=cmd_nights)

    p = add(
        "layers",
        "list analysis and agent layers",
        "List the annotation layers (analysis layers and agent layers) and, per layer, how "
        "many recordings have rows, did not run, are missing or are hidden by blinding.",
        epilog="example:\n  nexus-workbench layers --project p_123",
    )
    project_flag(p)
    json_flag(p)
    p.set_defaults(func=cmd_layers)

    p = add(
        "events",
        "list a recording's machine events (detail scope)",
        "List one page of a recording's machine events. With --save, read every page and "
        "write them as a .csv or .json event file the local compare_events tool reads (an "
        "event on several channels is kept once). Needs the detail scope.",
        epilog=(
            "examples:\n  nexus-workbench events e_123 --kind spindle\n"
            "  nexus-workbench events e_123 --save machine_events.csv"
        ),
    )
    p.add_argument("id", help="recording id")
    p.add_argument("--kind", help="only this kind, for example spindle")
    p.add_argument("--limit", type=int, help="rows, 1 to 1000 (default 500)")
    p.add_argument("--cursor", help="cursor printed by the previous page")
    p.add_argument("--save", metavar="FILE", help="write every event to a new .csv or .json file")
    json_flag(p)
    p.set_defaults(func=cmd_events)

    p = add(
        "stages",
        "show a recording's stages per epoch (detail scope)",
        "Show minutes per stage from the machine staging, or from your own scoring with "
        "--source mine. With --save, write the stages as a hypnogram file (one epoch a line) "
        "the local compare_hypnograms tool reads. Needs the detail scope.",
        epilog=(
            "examples:\n  nexus-workbench stages e_123\n"
            "  nexus-workbench stages e_123 --source mine --save mine.txt"
        ),
    )
    p.add_argument("id", help="recording id")
    p.add_argument(
        "--source", choices=["machine", "mine"], default="machine", help="default machine"
    )
    p.add_argument("--save", metavar="FILE", help="write the stages to a new hypnogram file")
    json_flag(p)
    p.set_defaults(func=cmd_stages)

    p = add(
        "quality",
        "show a recording's signal quality (detail scope)",
        "Show a recording's signal quality grade, epochs per grade and a row per channel. "
        "Needs the detail scope.",
        epilog="example:\n  nexus-workbench quality e_123",
    )
    p.add_argument("id", help="recording id")
    json_flag(p)
    p.set_defaults(func=cmd_quality)

    p = add(
        "marks",
        "list a recording's marks (detail scope)",
        "List one page of a recording's marks: human marks, machine rows and agent rows, "
        "with members' agree and disagree counts. Needs the detail scope.",
        epilog="example:\n  nexus-workbench marks e_123 --origin agent",
    )
    p.add_argument("id", help="recording id")
    p.add_argument("--origin", choices=["human", "machine", "agent"], help="only one origin")
    p.add_argument("--limit", type=int, help="rows, 1 to 500 (default 200)")
    p.add_argument("--cursor", help="cursor printed by the previous page")
    json_flag(p)
    p.set_defaults(func=cmd_marks)

    p = add(
        "propose",
        "propose marks from a file as a review layer (marks:write scope)",
        "Propose marks from a .csv or .json file. They land as this token's own review layer, "
        "which members agree or disagree with; they never change anyone's scoring or marks. "
        "CSV columns: kind,t0,t1 and optionally channels (positions separated by ;), label, "
        "confidence, text. Files of more than 500 marks are sent in several calls; a rerun "
        "within 24 hours does not send them twice. --dry-run checks the file and sends "
        "nothing. Needs the marks:write scope.",
        epilog=(
            "examples:\n  nexus-workbench propose e_123 arousals.csv --dry-run\n"
            "  nexus-workbench propose e_123 arousals.csv"
        ),
    )
    p.add_argument("id", help="recording id")
    p.add_argument("file", help="marks file (.csv or .json)")
    p.add_argument("--dry-run", action="store_true", help="check the file, send nothing")
    p.add_argument("--op-id", help="retry key, 1 to 64 characters (default: made from the marks)")
    json_flag(p)
    p.set_defaults(func=cmd_propose)

    p = add(
        "withdraw",
        "withdraw marks this token proposed (marks:write scope)",
        "Withdraw marks this token proposed on a recording: the ones named with --mark, or all "
        "of them. Members' verdicts on them are kept. Needs the marks:write scope.",
        epilog=(
            "examples:\n  nexus-workbench withdraw e_123 --mark agent-tok_0123456789abcdef-4\n"
            "  nexus-workbench withdraw e_123 --yes"
        ),
    )
    p.add_argument("id", help="recording id")
    p.add_argument("--mark", action="append", metavar="ID", help="a mark id (repeat for more)")
    p.add_argument("--yes", "-y", action="store_true", help="withdraw all without asking")
    json_flag(p)
    p.set_defaults(func=cmd_withdraw)

    p = add(
        "task",
        "give review tasks (tasks:write scope)",
        "Give review tasks to a member, as you would from the Tasks page.",
    )
    task_sub = p.add_subparsers(
        dest="task_command", metavar="ACTION", required=True, help="give: give review tasks"
    )
    g = task_sub.add_parser(
        "give",
        help="give a member review tasks on recordings",
        description=(
            "Give a member review tasks on 1 to 100 recordings, as you would from the page. "
            "The member gets the page's one email, at most one a minute and five an hour. "
            "Recordings that cannot be given are listed with the reason. Exit 1 only when none "
            "was given. Needs the tasks:write scope."
        ),
        epilog=(
            "example:\n  nexus-workbench task give e_1 e_2 --member m_0123456789abcdef --mode blind"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g.add_argument("ids", nargs="+", help="recording ids (spaces or commas)")
    g.add_argument("--member", required=True, help="member id from `members`")
    g.add_argument("--mode", required=True, choices=["blind", "assisted"])
    g.add_argument(
        "--confirm-machine-seen",
        action="store_true",
        help="give blind tasks the member already saw machine output for (not truly blind)",
    )
    json_flag(g)
    g.set_defaults(func=cmd_task_give)

    p = add(
        "analyse",
        "ask for an analysis of a recording you uploaded (analysis:run scope)",
        "Ask Workbench to analyse a recording you uploaded that has no machine output yet, as "
        "Analyse in the recording editor does. Needs the analysis:run scope. For now Workbench "
        "checks the request and answers not_available without starting anything; use Analyse "
        "on the recording page.",
        epilog="example:\n  nexus-workbench analyse e_123",
        aliases=["analyze"],
    )
    p.add_argument("id", help="recording id")
    json_flag(p)
    p.set_defaults(func=cmd_analyse)

    p = add(
        "preprocess",
        "filter, notch, resample or pick channels of a local EDF into a new file",
        "Write a new EDF+C file from a local EDF: a zero-phase band-pass (--low and --high; "
        "either alone is a high-pass or low-pass), a 50 or 60 Hz notch, a resample and a "
        "channel choice. Filters apply to voltage channels only. The input is never changed "
        "and an existing file is never replaced. No account needed.",
        epilog=(
            "example:\n  nexus-workbench preprocess night.edf --low 0.3 --high 35 --notch 50 "
            "--resample 128"
        ),
    )
    p.add_argument("path", help="EDF or EDF+ file on this computer")
    p.add_argument("--low", type=float, help="high-pass edge in Hz")
    p.add_argument("--high", type=float, help="low-pass edge in Hz")
    p.add_argument("--notch", type=float, choices=[50.0, 60.0], help="mains notch, 50 or 60 Hz")
    p.add_argument("--resample", type=int, help="new sample rate in whole Hz")
    p.add_argument("--channels", action="append", help="labels or positions, comma separated")
    p.add_argument("--out", help="new file or folder (default <name>_preprocessed.edf)")
    json_flag(p)
    p.set_defaults(func=cmd_preprocess)
