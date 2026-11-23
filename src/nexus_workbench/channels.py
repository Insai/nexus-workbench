"""Channel sites of a recording: ``nexus-workbench channels show | set | clear``.

A thin client of the agent API's ``get_channels`` and ``set_channels`` (contract section 7.12).
Every write goes through the server's own policy: this module only parses the command line,
fills in what the server asks for (the version, a preset's ports) and prints the answer.

Exit codes for these commands: 0 ok, 1 failure, 2 usage, 3 refused by Workbench (the refusal
code is printed), 4 sign-in, token scope or workspace access.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

from .client import safe_text as _safe

#: The headband connector's ports, in firmware order (the contract's ``ports``).
PORTS = ("Ec1", "Et1", "Ec2", "Et2")
_PORT_BY_FOLD = {p.lower(): p for p in PORTS}

REFUSED, AUTH = 3, 4
#: An entry id as the agent API takes it; any other recording argument is a label.
ID_RE = re.compile(r"^[0-9a-fA-F]{24}$")

#: The server's refusal codes (contract 7.12) and what a person can do about each.
REFUSAL_FIXES: dict[str, str] = {
    "held": "You are held blind on this recording or its night. Ask someone who is not held "
    "to set the sites.",
    "insai_checked": "An Insai member checked these sites, so only Insai can change them. "
    "Ask Insai.",
    "checked_needs_insai": "Save the sites with --unchecked. An Insai member ticks them.",
    "bad_port": f"Name ports as {', '.join(PORTS)}.",
    "bad_site": "Use a 10-10 site such as Fp1, AF7 or M1 (mastoids M1/M2, in-ear RC/LC/RT/LK).",
    "duplicate_site": "Give each port its own site.",
    "changed": "Someone saved the sites after you read them. Run `channels show`, then set again.",
    "fixed_device": "This device's sites are fixed by the device. There is nothing to set; "
    "`channels show` reads them.",
    "combined": "This is a combined night. Set the sites on each of its recordings instead.",
    "device_crossed": "The recording's device does not match its file. Ask Insai to fix the "
    "device first.",
    "no_source_key": "Workbench cannot tell which file this recording is. Ask Insai.",
    "header_unparsed": "Only recordings whose rows are the Ec/Et ports (a DIN adapter) take sites.",
    "incomplete": "Place all four ports, or save with --unchecked.",
    "not_available": "Channel sites are not switched on for this Workbench server yet.",
    "unavailable": "Workbench could not read the recording's file header. Try again in a minute.",
}
#: Codes about naming the recording, not about the change.
LOOKUP_FIXES: dict[str, str] = {
    "not_found": "Check the label (exact and case-sensitive, as members see it in Workbench) or "
    "pass the recording's id.",
    "ambiguous_label": "Run again with one of these ids in place of the label.",
}
#: Sign-in, scope and workspace switch codes: exit 4.
AUTH_CODES = frozenset(
    {
        "not_signed_in",
        "unauthenticated",
        "token_expired",
        "forbidden",
        "early_access_required",
        "scope_required",
        "write_access_required",
    }
)
AUTH_FIXES: dict[str, str] = {
    "scope_required": "Make a token with the channels:write scope (Workbench: account menu, "
    "Connect an agent), then run `nexus-workbench login --token`.",
    "write_access_required": "This workspace does not take agent writes yet. Ask Insai to "
    "switch them on.",
}
#: Codes that are not refusals but still need the server's word (exit 1).
NOT_REFUSAL = frozenset({"not_available", "unavailable"})


class UsageError(ValueError):
    """A command line that cannot be sent. The message says what to type instead."""


def recording_ref(recording: str, *, by_id: bool = False) -> dict[str, str]:
    """``{"id": ...}`` for 24 hex characters (or ``--id``), else ``{"label": ...}``."""
    text = recording.strip()
    if not text:
        raise UsageError("Name the recording by its label or its id.")
    return {"id": text} if by_id or ID_RE.fullmatch(text) else {"label": text}


def exit_code(code: str) -> int:
    if code == "ambiguous_label":
        return 2
    if code in AUTH_CODES:
        return AUTH
    if code in REFUSAL_FIXES and code not in NOT_REFUSAL:
        return REFUSED
    return 1


def fix_for(code: str) -> str:
    return AUTH_FIXES.get(code) or REFUSAL_FIXES.get(code) or LOOKUP_FIXES.get(code, "")


# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------


def parse_ports(values: Sequence[str] | None) -> dict[str, str]:
    """``["Ec1=M1", "et2=af8", "Ec2="]`` -> ``{"Ec1": "M1", "Et2": "af8", "Ec2": ""}``.

    Port names are case-blind; a site is sent as typed (the server spells it). An empty site
    takes the port off.
    """
    out: dict[str, str] = {}
    for raw in values or ():
        port, sep, site = raw.partition("=")
        name = _PORT_BY_FOLD.get(port.strip().lower())
        if not sep or name is None:
            raise UsageError(
                f"--port takes PORT=SITE with PORT one of {', '.join(PORTS)}, got {raw!r}."
            )
        if name in out:
            raise UsageError(f"--port {name} is given twice.")
        out[name] = site.strip()
    return out


def plan_ports(
    current: Mapping[str, Any],
    presets: Sequence[Mapping[str, Any]],
    preset: str | None,
    overrides: Mapping[str, str],
) -> dict[str, str]:
    """The ports to send: the preset's (or the recording's current ones), then each --port.

    Ports left empty are not sent: the server reads a missing port as not placed.
    """
    if preset is not None:
        found = next((p for p in presets if p.get("id") == preset), None)
        if found is None:
            names = ", ".join(str(p.get("id")) for p in presets) or "none"
            raise UsageError(f"There is no preset {preset!r}. Presets: {names}.")
        base = dict(found.get("ports") or {})
    else:
        base = dict(current)
    base.update(overrides)
    return {p: str(base[p]) for p in PORTS if base.get(p)}


def port_flags(ports: Mapping[str, Any], checked: Any) -> str:
    """``--port Ec1=M1 --port Et1=AF7 ... --checked``, in firmware order."""
    parts = [f"--port {p}={_safe(ports[p])}" for p in PORTS if ports.get(p)]
    parts.append("--checked" if checked else "--unchecked")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_STATE_TEXT = {
    "ok": "set",
    "not_set": "not set",
    "stale": "file changed since the sites were set (treated as not set)",
    "conflict": "device unclear (treated as not set)",
}
_FIXED_TEXT = {
    "device": "fixed by the device",
    "header": "fixed by the file's channel labels",
    "combined": "fixed: read from the night's recordings",
}


def _table(rows: list[list[str]]) -> list[str]:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return ["  ".join(c.ljust(w) for c, w in zip(r, widths, strict=True)).rstrip() for r in rows]


def format_show(recording: str, data: Mapping[str, Any]) -> str:
    eff = data.get("effective") if isinstance(data.get("effective"), Mapping) else {}
    perms = data.get("permissions") if isinstance(data.get("permissions"), Mapping) else {}
    source = str(eff.get("source") or "none")
    fixed = bool(eff.get("fixed"))
    checked = bool(eff.get("checked")) if fixed else bool(data.get("checked"))
    lines = [
        f"Recording: {_safe(recording)}",
        f"Sites:     {_STATE_TEXT.get(str(eff.get('state')), _safe(eff.get('state')))}, "
        f"{'checked' if checked else 'NOT checked'}",
        "Device:    "
        + (_FIXED_TEXT.get(source, "fixed") if fixed else "not fixed: sites are set per recording"),
        f"Version:   {_safe(data.get('version'))}",
    ]
    ports = data.get("ports") if isinstance(data.get("ports"), Mapping) else {}
    if ports:
        lines.append(f"Ports:     {port_flags(ports, data.get('checked'))}")
    if eff.get("likely"):
        lines.append(f"Likely:    {_safe(eff.get('likely'))} (a hint only, not set)")
    rows = [r for r in eff.get("rows") or [] if isinstance(r, Mapping)]
    if rows:
        table = [["#", "APP LABEL", "FILE LABEL", "SITE"]]
        for r in rows:
            site = _safe(r.get("site")) if r.get("site") else "-"
            if r.get("protocolInverted") and r.get("site"):
                site += "  (inverted)"
            table.append(
                [_safe(r.get("index")), _safe(r.get("laneLabel")), _safe(r.get("fileLabel")), site]
            )
        lines += ["", *_table(table)]
    if not perms.get("edit"):
        reason = perms.get("reason")
        lines += ["", f"You cannot change these sites ({_safe(reason) or 'read only'})."]
        if isinstance(reason, str) and fix_for(reason):
            lines.append(fix_for(reason))
    elif not perms.get("tick"):
        lines += ["", "You can set the sites but not tick them: save with --unchecked."]
    return "\n".join(lines)


def undo_command(
    command: str,
    recording: str,
    prev_ports: Mapping[str, Any],
    prev: Mapping[str, Any],
    now_ports: Mapping[str, Any],
) -> str:
    """The command that puts the previous sites back. ``set`` keeps ports it is not given, so
    a port placed now but not before is named empty, and an empty unchecked state is ``clear``."""
    rec = shlex.quote(_safe(recording))
    if not prev_ports and not prev.get("checked"):
        return f"{command} channels clear {rec}"
    flags = [
        f"--port {p}={_safe(prev_ports[p])}" if prev_ports.get(p) else f"--port {p}="
        for p in PORTS
        if prev_ports.get(p) or now_ports.get(p)
    ]
    flags.append("--checked" if prev.get("checked") else "--unchecked")
    return f"{command} channels set {rec} {' '.join(flags)}"


def format_set(recording: str, data: Mapping[str, Any], *, command: str) -> str:
    version = _safe(data.get("version"))
    prev = data.get("prev") if isinstance(data.get("prev"), Mapping) else {}
    prev_ports = prev.get("ports") if isinstance(prev.get("ports"), Mapping) else {}
    now_ports = data.get("ports") if isinstance(data.get("ports"), Mapping) else {}
    if data.get("unchanged"):
        head = f"Unchanged: the sites already read this way (version {version})."
    elif not now_ports and not data.get("checked"):
        head = f"Cleared (version {version})."
    else:
        head = f"Saved (version {version})."
    lines = [
        head,
        f"was: {port_flags(prev_ports, prev.get('checked'))}",
        f"now: {port_flags(now_ports, data.get('checked'))}",
    ]
    if not data.get("unchanged"):
        lines.append(
            f"undo: {undo_command(command, recording, prev_ports, prev, now_ports)} "
            f"--expected-version {version}"
        )
    return "\n".join(lines)


def candidates_detail(extra: Mapping[str, Any]) -> str:
    """The line printed under an ``ambiguous_label`` refusal: the ids to pick from."""
    ids = [_safe(i, 64) for i in extra.get("candidates") or []]
    return f"Candidates: {', '.join(ids)}" if ids else ""


def changed_detail(extra: Mapping[str, Any]) -> str:
    """The line printed under a ``changed`` refusal: what the server holds now."""
    current = extra.get("current")
    if not isinstance(current, Mapping):
        return ""
    ports = current.get("ports") if isinstance(current.get("ports"), Mapping) else {}
    return (
        f"Now at version {_safe(current.get('version'))}: "
        f"{port_flags(ports, current.get('checked'))}"
    )
