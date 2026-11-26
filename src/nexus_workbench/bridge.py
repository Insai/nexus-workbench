"""Workspace detail in the local tools' file formats (contract 7.6 and docs/tools.md).

``get_stages`` output becomes a hypnogram that ``compare_hypnograms`` reads, and
``get_events`` output becomes an event list that ``compare_events`` reads. The MCP tools pass
the text straight to the local tools; ``nexus-workbench stages --save`` and
``events --save`` write it to a file.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .client import WorkbenchClient

STAGE_CODE = re.compile(r"W|N[123]|R|\?")
STAGES_SHAPE = re.compile(r"^(?:W|N[123]|R|\?)*$")
#: An event kind as the contract names them: plain words, no separators.
KIND_SHAPE = re.compile(r"^[A-Za-z0-9_]{1,40}$")
#: Pages of ``get_events`` read for one comparison (1,000 rows each). ``get_events`` is
#: limited to 10 calls a minute per token (contract 7.3), so one comparison stays inside it.
MAX_EVENT_PAGES = 10
EVENTS_PER_PAGE = 1000


class BridgeError(Exception):
    """The workspace cannot supply this side of a comparison. The message says why.

    ``code`` is the contract code a tool error carries: ``invalid_argument`` when that side
    cannot come from this recording, ``unexpected_response`` for a reply this version does
    not understand.
    """

    def __init__(self, message: str, *, code: str = "invalid_argument") -> None:
        self.code = code
        super().__init__(message)


def hidden_message(what: str) -> str:
    return (
        f"You are blinded on this recording, so its {what} is withheld. Nothing was compared. "
        "That is by design; do not try to infer it."
    )


def split_stages(stages: str) -> list[str]:
    """One code per epoch from the contract's run-together stage string."""
    if not isinstance(stages, str) or not STAGES_SHAPE.match(stages):
        raise BridgeError(
            "The workspace sent a stage sequence this version does not understand. "
            "Update with `uv tool upgrade nexus-workbench`, then try again.",
            code="unexpected_response",
        )
    return STAGE_CODE.findall(stages)


def stages_text(result: dict[str, Any]) -> tuple[str, float]:
    """A hypnogram (one code per line, ``?`` unscored) and the epoch length, from a
    ``get_stages`` result. Raises :class:`BridgeError` when there is none to compare."""
    source = result.get("source")
    what = "machine staging" if source == "machine" else "scoring"
    if result.get("hidden"):
        raise BridgeError(hidden_message(what))
    if not result.get("available") or not isinstance(result.get("stages"), str):
        if source == "mine":
            raise BridgeError(
                "You have not scored this recording yet, so there is nothing to compare."
            )
        raise BridgeError("This recording has no machine staging, so there is nothing to compare.")
    codes = split_stages(result["stages"])
    n = result.get("nEpochs")
    if isinstance(n, int) and n != len(codes):
        raise BridgeError(
            f"The workspace said {n} epochs but sent {len(codes)} stage codes. "
            "Update with `uv tool upgrade nexus-workbench`, then try again.",
            code="unexpected_response",
        )
    epoch = result.get("epochSec", 30)
    if not isinstance(epoch, (int, float)) or isinstance(epoch, bool) or epoch <= 0:
        epoch = 30
    return "stage\n" + "\n".join(codes) + "\n", float(epoch)


def fetch_events(
    client: WorkbenchClient, recording_id: str, kind: str | None = None
) -> list[dict[str, Any]]:
    """Every machine event of a recording (optionally one kind), page by page.

    Raises :class:`BridgeError` when the events are withheld, absent, or too many.
    """
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(MAX_EVENT_PAGES):
        page = client.get_events(recording_id, kind=kind, cursor=cursor, limit=EVENTS_PER_PAGE)
        if page.get("hidden"):
            raise BridgeError(hidden_message("machine event list"))
        if not page.get("available"):
            raise BridgeError(
                "This recording has no machine events, so there is nothing to compare."
            )
        got = page.get("events")
        if not isinstance(got, list):
            raise BridgeError(
                "The workspace sent events this version does not understand. "
                "Update with `uv tool upgrade nexus-workbench`, then try again.",
                code="unexpected_response",
            )
        rows.extend(e for e in got if isinstance(e, dict))
        cursor = page.get("nextCursor")
        if not cursor:
            return rows
    raise BridgeError(
        f"This recording has more than {MAX_EVENT_PAGES * EVENTS_PER_PAGE:,} machine event rows. "
        "Compare one kind at a time with label."
    )


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def events_records(
    rows: list[dict[str, Any]], *, per_channel: bool = False
) -> list[dict[str, Any]]:
    """Events as ``onset_sec``, ``duration_sec``, ``label`` records, in order.

    The contract sends one row per channel for an event seen on several channels. Unless
    ``per_channel``, rows with the same onset, duration and kind are kept once, so each event
    counts once, as it does in ``eventCounts``.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[float, float, str]] = set()
    for e in rows:
        onset, duration, kind = _num(e.get("onsetSec")), _num(e.get("durationSec")), e.get("kind")
        if onset is None or duration is None or not isinstance(kind, str):
            continue
        if not KIND_SHAPE.match(kind):
            continue
        key = (onset, duration, kind)
        if not per_channel:
            if key in seen:
                continue
            seen.add(key)
        record: dict[str, Any] = {"onset_sec": onset, "duration_sec": duration, "label": kind}
        if per_channel and isinstance(e.get("channel"), int):
            record["channel"] = e["channel"]
        out.append(record)
    return out


def events_json(records: list[dict[str, Any]]) -> str:
    return json.dumps(records, separators=(",", ":"))


def events_csv(records: list[dict[str, Any]]) -> str:
    lines = ["onset_sec,duration_sec,label"]
    for r in records:
        lines.append(f"{r['onset_sec']:.3f},{r['duration_sec']:.3f},{r['label']}")
    return "\n".join(lines) + "\n"
