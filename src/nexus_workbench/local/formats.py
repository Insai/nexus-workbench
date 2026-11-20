"""Readers for the two small text formats the local tools take: hypnograms and event lists.

Both readers are tolerant about layout and strict about values: a value they cannot read is
an error that names the line, never a silent guess. See ``docs/tools.md`` for the formats.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import numpy as np

from .errors import LocalToolError
from .events import Event

MAX_EPOCHS = 200_000
MAX_EVENTS = 200_000

_STAGE_WORDS: dict[str, int] = {
    "W": 0, "WAKE": 0, "WK": 0, "AWAKE": 0,
    "N1": 1, "S1": 1, "NREM1": 1,
    "N2": 2, "S2": 2, "NREM2": 2,
    "N3": 3, "S3": 3, "S4": 3, "N4": 3, "NREM3": 3, "SWS": 3,
    "R": 4, "REM": 4,
}  # fmt: skip
_UNSCORED_WORDS = {
    "", "?", "-", "-1", "U", "UNS", "UNSCORED", "UNKNOWN", "NS", "NOTSCORED", "MT", "MOVEMENT",
    "A", "ART", "ARTIFACT", "ARTEFACT", "X", "NAN", "NONE", "NULL",
}  # fmt: skip
_STAGE_HEADERS = {"STAGE", "STAGES", "SLEEPSTAGE", "LABEL", "HYPNOGRAM", "SCORE", "ANNOTATION"}
_EPOCH_HEADERS = {"EPOCH", "EPOCHS", "EPOCHINDEX", "EPOCHNO", "EPOCHNUMBER", "INDEX", "IDX", "N"}
_UNSCORED = -1
_INT_STAGE = {0: 0, 1: 1, 2: 2, 3: 3, 5: 4}  # 4 handled separately

_ONSET_KEYS = ("onset_sec", "onset", "onset_s", "start_sec", "start", "start_s", "time", "t")
_DURATION_KEYS = ("duration_sec", "duration", "duration_s", "dur", "dur_s", "length")
_END_KEYS = ("end_sec", "end", "end_s", "offset_sec", "stop")
_LABEL_KEYS = ("label", "description", "type", "kind", "event", "name")


def _norm(tok: str) -> str:
    t = tok.strip().strip('"').strip("'").strip().upper()
    t = re.sub(r"^(SLEEP\s*STAGE|STAGE)\s*", "", t)
    return re.sub(r"[\s_]+", "", t)


def _hnorm(tok: str) -> str:
    """Normalised header cell: upper case, letters and digits only."""
    return re.sub(r"[^A-Z0-9]", "", tok.upper())


def _is_int(s: str) -> bool:
    return re.fullmatch(r"[+-]?\d+", s) is not None


def _stage_ok(tok: str) -> bool:
    t = _norm(tok)
    return (
        t in _STAGE_WORDS or t in _UNSCORED_WORDS or (_is_int(t) and int(t) in (0, 1, 2, 3, 4, 5))
    )


def _delim(line: str) -> str | None:
    for d in (",", ";", "\t"):
        if d in line:
            return d
    return None


def parse_hypnogram(text: str, name: str = "the hypnogram") -> np.ndarray:
    """Stage codes (0=W 1=N1 2=N2 3=N3 4=R, -1 unscored), one per epoch."""
    lines = [(n + 1, ln.rstrip("\r")) for n, ln in enumerate(text.split("\n"))]
    lines = [(n, ln) for n, ln in lines if not ln.lstrip().startswith("#")]
    while lines and not lines[-1][1].strip():
        lines.pop()
    while lines and not lines[0][1].strip():
        lines.pop(0)
    if not lines:
        raise LocalToolError(f"{name} is empty. A hypnogram has one sleep stage per epoch.")
    d = _delim(lines[0][1]) or (_delim(lines[1][1]) if len(lines) > 1 else None)

    raw: list[tuple[int, str]] = []  # (line number, stage token)
    epochs: list[int] | None = None
    if d is None:
        first = True
        for n, ln in lines:
            if first and _hnorm(ln) in _STAGE_HEADERS:
                first = False
                continue
            first = False
            if _stage_ok(ln):
                raw.append((n, ln))
            else:
                for tok in ln.split():
                    raw.append((n, tok))
    else:
        rows = [(n, [c.strip() for c in ln.split(d)]) for n, ln in lines]
        head = [_hnorm(c) for c in rows[0][1]]
        stage_col: int | None = None
        epoch_col: int | None = None
        if any(h in _STAGE_HEADERS for h in head):
            stage_col = next(i for i, h in enumerate(head) if h in _STAGE_HEADERS)
            epoch_col = next((i for i, h in enumerate(head) if h in _EPOCH_HEADERS), None)
            rows = rows[1:]
        elif not all(_stage_ok(c) or _is_int(c) or _is_number(c) for c in rows[0][1]):
            raise LocalToolError(
                f"Line {rows[0][0]} of {name} looks like a header but has no 'stage' column. "
                "Name the column 'stage', or drop the header."
            )
        else:
            stage_col = len(rows[0][1]) - 1
            if stage_col > 0 and all(
                len(r) > 0 and _is_int(r[0].strip()) for _, r in rows if any(c for c in r)
            ):
                epoch_col = 0
        epochs = [] if epoch_col is not None else None
        for n, r in rows:
            if len(r) <= stage_col:
                if not any(c for c in r):
                    raw.append((n, ""))
                    if epochs is not None:
                        raise LocalToolError(
                            f"Line {n} of {name} is empty but the file numbers its epochs. "
                            "Give every row an epoch number and a stage."
                        )
                    continue
                raise LocalToolError(
                    f"Line {n} of {name} has {len(r)} columns; the stage is in column "
                    f"{stage_col + 1}. Check the separators."
                )
            raw.append((n, r[stage_col]))
            if epochs is not None and epoch_col is not None:
                v = r[epoch_col].strip()
                if not _is_int(v):
                    raise LocalToolError(
                        f"Line {n} of {name}: the epoch number is not a whole number."
                    )
                epochs.append(int(v))

    ints = {int(_norm(t)) for _, t in raw if _is_int(_norm(t)) and _norm(t) != "-1"}
    four_is_n3 = 4 in ints and 5 in ints
    if 4 in ints and 5 not in ints:
        raise LocalToolError(
            f"{name} uses the number 4, which means REM in some tools and stage 4 (now N3) in "
            "others. Write the stages as W, N1, N2, N3, R, or use the coding 0=W 1=N1 2=N2 "
            "3=N3 5=REM."
        )
    codes: list[int] = []
    for n, tok in raw:
        t = _norm(tok)
        if t in _STAGE_WORDS:
            codes.append(_STAGE_WORDS[t])
        elif t in _UNSCORED_WORDS:
            codes.append(_UNSCORED)
        elif _is_int(t) and int(t) in _INT_STAGE:
            codes.append(_INT_STAGE[int(t)])
        elif _is_int(t) and int(t) == 4 and four_is_n3:
            codes.append(3)
        else:
            raise LocalToolError(
                f"Line {n} of {name} holds a value that is not a sleep stage. Use W, N1, N2, "
                "N3, R (or REM), the numbers 0, 1, 2, 3, 5, or ? for an unscored epoch."
            )
    if epochs is not None:
        if any(b <= a for a, b in zip(epochs, epochs[1:], strict=False)):
            raise LocalToolError(
                f"The epoch numbers in {name} do not increase from row to row. Sort the rows "
                "by epoch."
            )
        base = 0 if min(epochs) == 0 else 1
        if min(epochs) < base or max(epochs) - base + 1 > MAX_EPOCHS:
            raise LocalToolError(f"The epoch numbers in {name} are out of range.")
        out = np.full(max(epochs) - base + 1, _UNSCORED, dtype=int)
        out[np.asarray(epochs) - base] = codes
        return out
    if len(codes) > MAX_EPOCHS:
        raise LocalToolError(f"{name} has more than {MAX_EPOCHS} epochs.")
    return np.asarray(codes, dtype=int)


def _is_number(s: str) -> bool:
    try:
        return math.isfinite(float(s))
    except ValueError:
        return False


def _num(v: object, what: str, where: str) -> float:
    if isinstance(v, bool) or v is None:
        raise LocalToolError(f"{where}: the {what} is missing or not a number.")
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        raise LocalToolError(f"{where}: the {what} is not a number.") from None
    if not math.isfinite(x):
        raise LocalToolError(f"{where}: the {what} is not a finite number.")
    return x


def _make_event(onset: object, duration: object, end: object, label: object, where: str) -> Event:
    on = _num(onset, "onset", where)
    if duration is not None and duration != "":
        du = _num(duration, "duration", where)
    elif end is not None and end != "":
        du = _num(end, "end", where) - on
    else:
        du = 0.0
    if on < 0 or du < 0:
        raise LocalToolError(f"{where}: onsets and durations must not be negative.")
    return Event(on, du, str(label).strip() if label is not None else "")


def _pick(keys: dict[str, Any], options: tuple[str, ...]) -> Any:
    for k in options:
        if k in keys:
            return keys[k]
    return None


def parse_events(text: str, name: str = "the event file") -> list[Event]:
    """Events from CSV (``onset_sec,duration_sec[,label]``, optional header) or JSON."""
    s = text.strip()
    if not s:
        return []
    if s[0] in "[{":
        try:
            doc = json.loads(s)
        except json.JSONDecodeError as exc:
            raise LocalToolError(
                f"{name} looks like JSON but could not be parsed (line {exc.lineno}). Check it "
                "is a list of objects with onset_sec and duration_sec."
            ) from None
        if isinstance(doc, dict) and isinstance(doc.get("events"), list):
            doc = doc["events"]
        if not isinstance(doc, list):
            raise LocalToolError(
                f"{name} must hold a JSON list of events, or an object with an 'events' list."
            )
        if len(doc) > MAX_EVENTS:
            raise LocalToolError(f"{name} has more than {MAX_EVENTS} events.")
        out = []
        for k, item in enumerate(doc):
            where = f"Event {k + 1} of {name}"
            if isinstance(item, dict):
                lower = {str(a).lower(): a for a in item}
                ok = _pick(lower, _ONSET_KEYS)
                dk = _pick(lower, _DURATION_KEYS)
                ek = _pick(lower, _END_KEYS)
                lk = _pick(lower, _LABEL_KEYS)
                if ok is None:
                    raise LocalToolError(f"{where} has no onset_sec.")
                out.append(
                    _make_event(
                        item[ok],
                        item[dk] if dk is not None else None,
                        item[ek] if ek is not None else None,
                        item[lk] if lk is not None else None,
                        where,
                    )
                )
            elif isinstance(item, list) and 1 <= len(item) <= 3:
                out.append(
                    _make_event(
                        item[0],
                        item[1] if len(item) > 1 else None,
                        None,
                        item[2] if len(item) > 2 else None,
                        where,
                    )
                )
            else:
                raise LocalToolError(f"{where} is not an object with onset_sec and duration_sec.")
        return out

    lines = [(n + 1, ln.strip()) for n, ln in enumerate(text.split("\n"))]
    lines = [(n, ln) for n, ln in lines if ln and not ln.startswith("#")]
    d = _delim(lines[0][1])
    rows = [(n, [c.strip() for c in (ln.split(d) if d else ln.split())]) for n, ln in lines]
    col_on = 0
    col_du: int | None = 1
    col_end: int | None = None
    col_lab: int | None = 2
    if not _is_number(rows[0][1][0]):
        head = [c.strip().strip('"').lower() for c in rows[0][1]]
        idx = {h: i for i, h in enumerate(head)}
        on = _pick(idx, _ONSET_KEYS)
        if on is None:
            raise LocalToolError(
                f"The header of {name} has no onset column. Name it onset_sec, or drop the "
                "header and put onset, duration and label in that order."
            )
        col_on = int(on)
        du = _pick(idx, _DURATION_KEYS)
        col_du = int(du) if du is not None else None
        en = _pick(idx, _END_KEYS)
        col_end = int(en) if en is not None else None
        la = _pick(idx, _LABEL_KEYS)
        col_lab = int(la) if la is not None else None
        rows = rows[1:]
    if len(rows) > MAX_EVENTS:
        raise LocalToolError(f"{name} has more than {MAX_EVENTS} events.")

    def cell(r: list[str], c: int | None) -> str | None:
        return r[c] if c is not None and c < len(r) else None

    return [
        _make_event(
            cell(r, col_on),
            cell(r, col_du),
            cell(r, col_end),
            cell(r, col_lab),
            f"Line {n} of {name}",
        )
        for n, r in rows
    ]
