"""A fake of the agent API's ``get_channels`` / ``set_channels`` (contract 7.12).

It follows the server's refusal order, so a test can switch on one condition and see the
command line's answer to exactly that refusal. Synthetic only: one DIN recording whose four
ports carry "frontal on Ec/Et" style sites; no real recording, person or token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PORTS = ("Ec1", "Et1", "Ec2", "Et2")
ROWS = (("Ec1", "Ec2"), ("Et1", "Et2"), ("Ec1", "Et1"), ("Ec2", "Et2"))
SITES = {s.lower(): s for s in ("Fp1", "Fp2", "AF7", "AF8", "F3", "F4", "Fz", "Cz", "M1", "M2")}
_P = ("Ec1", "Ec2", "Et1", "Et2")
PRESETS = [
    {"id": i, "label": label, "ports": dict(zip(_P, sites, strict=True))}
    for i, label, sites in (
        ("forehead", "Forehead", ("Fp1", "Fp2", "AF7", "AF8")),
        ("frontal_on_ec", "Frontal + mastoids (frontal on Ec)", ("AF7", "AF8", "M1", "M2")),
        ("frontal_on_et", "Frontal + mastoids (frontal on Et)", ("M1", "M2", "AF7", "AF8")),
    )
]
RECORDING = "e_din1"
DENIED = {"held", "insai_checked", "checked_needs_insai"}
INVALID = {"bad_port", "bad_site", "duplicate_site"}


@dataclass
class ChannelState:
    version: int = 0
    ports: dict[str, str] = field(default_factory=dict)
    checked: bool = False
    enabled: bool = True
    scopes: set[str] = field(default_factory=lambda: {"read", "channels:write"})
    agent_write: bool = True
    external: bool = False
    fixed: bool = False  # a headband with a known accessory
    held: bool = False
    combined: bool = False
    no_source_key: bool = False
    device_crossed: bool = False
    header_unparsed: bool = False
    header_unreadable: bool = False
    #: Bumps the version between the CLI's read and its write (a race).
    race: bool = False
    calls: list[dict[str, Any]] = field(default_factory=list)


def _error(status: int, code: str, message: str, **extra: Any) -> tuple[int, dict[str, Any]]:
    return status, {"error": {"code": code, "message": message}, **extra}


def _effective(st: ChannelState) -> dict[str, Any]:
    if st.fixed:
        ports, source, checked, version = (
            {"Ec1": "Fp1", "Ec2": "Fp2", "Et1": "AF7", "Et2": "AF8"},
            "device",
            True,
            None,
        )
    else:
        ports, source, checked, version = st.ports, "explicit", st.checked, st.version
    rows = []
    for i, (a, b) in enumerate(ROWS):
        site = f"{ports[a]}-{ports[b]}" if ports.get(a) and ports.get(b) else None
        rows.append(
            {
                "index": i,
                "fileLabel": f"{a}-{b}",
                "laneLabel": f"{a}-{b}",
                "site": site,
                "protocolInverted": site == "M1-AF7",
            }
        )
    state = "ok" if any(r["site"] for r in rows) else "not_set"
    out: dict[str, Any] = {
        "state": state,
        "source": source if state == "ok" else "none",
        "checked": checked,
        "fixed": st.fixed,
        "version": version if state == "ok" else None,
        "rows": rows,
    }
    return out


def _permissions(st: ChannelState) -> dict[str, Any]:
    for flag, code in (
        (st.fixed, "fixed_device"),
        (st.held, "held"),
        (st.header_unparsed, "header_unparsed"),
    ):
        if flag:
            return {"edit": False, "tick": False, "reason": code}
    if st.external and st.checked:
        return {"edit": False, "tick": False, "reason": "insai_checked"}
    if st.external:
        return {"edit": True, "tick": False, "reason": "checked_needs_insai"}
    return {"edit": True, "tick": True, "reason": None}


def _state(st: ChannelState) -> dict[str, Any]:
    return {"version": st.version, "ports": dict(st.ports), "checked": st.checked}


def respond(st: ChannelState, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """The fake server's answer to one authenticated agent API call."""
    st.calls.append(body)
    action = body.get("action")
    if action not in ("get_channels", "set_channels"):
        return _error(400, "invalid_argument", "Unknown action.")
    if action == "set_channels":
        if "channels:write" not in st.scopes:
            return _error(
                403,
                "scope_required",
                "This token cannot set channel sites. Make a token with the channels:write scope.",
                scope="channels:write",
            )
        if not st.agent_write:
            return _error(403, "write_access_required", "Agent writes are off.")
    if not st.enabled:
        return _error(501, "not_available", "Channel sites are not enabled here.")
    if action == "set_channels":
        return _set(st, body)
    if body.get("id") != RECORDING:
        return _error(404, "not_found", "Not found.")
    if st.header_unreadable:
        return _error(503, "unavailable", "The file header could not be read.")
    return 200, {
        "effective": _effective(st),
        "ports": dict(st.ports),
        "checked": st.checked,
        "version": st.version,
        "permissions": _permissions(st),
        "presets": PRESETS,
        "contractRevision": "bf3fcf2d2d0effeb",
    }


def _set(st: ChannelState, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    expected = body.get("expectedVersion")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
        return _error(400, "invalid_argument", "'expectedVersion' must be a whole number")
    checked = body.get("checked")
    if not isinstance(checked, bool):
        return _error(400, "invalid_argument", "'checked' must be true or false")
    raw = body.get("ports")
    if not isinstance(raw, dict):
        return _error(400, "bad_port", "'ports' must be an object.")
    ports: dict[str, str] = {}
    for key, value in raw.items():
        port = {p.lower(): p for p in PORTS}.get(str(key).lower())
        if port is None:
            return _error(400, "bad_port", f"{key} is not a port.")
        if not value:
            continue
        site = SITES.get(str(value).lower())
        if site is None:
            return _error(400, "bad_site", f"{value} is not a site.")
        if site in ports.values():
            return _error(400, "duplicate_site", f"{site} is on two ports.")
        ports[port] = site
    if body.get("id") != RECORDING:
        return _error(404, "not_found", "Not found.")
    if st.race:
        st.version += 1
        st.ports = {"Ec1": "Fp1", "Ec2": "Fp2", "Et1": "AF7", "Et2": "AF8"}
        st.race = False
    resulting = {"ports": ports, "checked": checked}
    current = {"ports": dict(st.ports), "checked": st.checked}
    for flag, status, code, message in (
        (st.no_source_key, 409, "no_source_key", "This recording has no source key."),
        (st.combined, 409, "combined", "Set it on its recordings."),
        (st.fixed, 409, "fixed_device", "This device's sites are fixed."),
        (st.held, 403, "held", "You are held blind on this recording."),
    ):
        if flag:
            return _error(status, code, message)
    if expected != st.version:
        return _error(409, "changed", "The sites changed since you read them.", current=_state(st))
    if st.device_crossed:
        return _error(409, "device_crossed", "The device does not match.")
    if st.external and st.checked and resulting != current:
        return _error(403, "insai_checked", "An Insai member checked these sites.")
    if st.external and checked:
        return _error(403, "checked_needs_insai", "Only an Insai member can tick.")
    if st.header_unparsed:
        return _error(409, "header_unparsed", "The header rows are not the Ec/Et ports.")
    if checked and len(ports) < 4:
        return _error(409, "incomplete", "A checked montage needs four ports.")
    prev = {"ports": dict(st.ports), "checked": st.checked}
    unchanged = resulting == current
    if not unchanged:
        st.version += 1
        st.ports, st.checked = ports, checked
    return 200, {
        "version": st.version,
        "ports": {p: st.ports[p] for p in PORTS if p in st.ports},
        "checked": st.checked,
        "effective": _effective(st),
        "prev": prev,
        "unchanged": unchanged,
    }
