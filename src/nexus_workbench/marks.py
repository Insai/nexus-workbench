"""Proposed marks (contract section 7.7): reading, checking and batching them on this computer.

The server checks every mark again. The checks here catch what can be caught without a call
(kinds, times, lengths, an email address in the text), so a bad file fails before anything
is sent, and ``propose --dry-run`` can check a file with no network at all.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from pathlib import Path
from typing import Any

KINDS: tuple[str, ...] = (
    "bad_signal",
    "movement",
    "electrode_pop",
    "flatline",
    "arousal",
    "spindle",
    "k_complex",
    "slow_wave",
    "sawtooth",
    "eye_movement",
)
#: Marks per ``propose_marks`` call.
MAX_PER_CALL = 500
#: Marks per token and recording, withdrawn ones included.
MAX_PER_RECORDING = 5000
MAX_CHANNELS = 16
LABEL_MAX = 40
TEXT_MAX = 200
#: Request body limit of ``propose_marks``, less room for the action, id and opId.
MAX_CALL_BYTES = 262_144 - 1024
#: Marks a file may hold (the cap per recording).
MAX_FILE_MARKS = MAX_PER_RECORDING
MAX_FILE_BYTES = 10 * 1024 * 1024

COLUMNS = ("kind", "t0", "t1", "channels", "label", "confidence", "text")
_EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
OP_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")


class MarksError(ValueError):
    """A mark or a marks file that cannot be sent. The message names the mark and the fix."""


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    elif isinstance(value, str) and value.strip():
        try:
            v = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return v if math.isfinite(v) else None


def clean_text(value: str) -> str:
    """Trim and drop control and invisible characters and line breaks, as the server does."""
    return "".join(ch for ch in value if ch.isprintable()).strip()


def _channels(value: Any, where: str) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        parts = [p for p in re.split(r"[;|\s]+", value.strip()) if p]
        value = parts
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        value = [value]
    if not isinstance(value, list):
        raise MarksError(f"{where}: channels must be a list of 1-based channel positions.")
    out: list[int] = []
    for item in value:
        n = _num(item)
        if n is None or n != int(n) or n < 1:
            raise MarksError(f"{where}: channels must be whole numbers of 1 or more.")
        out.append(int(n))
    if len(set(out)) != len(out):
        raise MarksError(f"{where}: a channel is listed twice.")
    if len(out) > MAX_CHANNELS:
        raise MarksError(f"{where}: at most {MAX_CHANNELS} channels per mark.")
    return out


def check_mark(raw: Any, position: int) -> dict[str, Any]:
    """One mark in the contract's shape, or :class:`MarksError`. ``position`` is 1-based."""
    where = f"Mark {position}"
    if not isinstance(raw, dict):
        raise MarksError(f"{where} is not an object with kind, t0 and t1.")
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind.strip() not in KINDS:
        raise MarksError(f"{where}: kind must be one of {', '.join(KINDS)}.")
    t0, t1 = _num(raw.get("t0")), _num(raw.get("t1"))
    if t0 is None or t1 is None:
        raise MarksError(f"{where}: t0 and t1 must be numbers of seconds from the start.")
    if t0 < 0 or t1 < t0:
        raise MarksError(f"{where}: t0 must be 0 or more and t1 at least t0.")
    mark: dict[str, Any] = {"kind": kind.strip(), "t0": t0, "t1": t1}
    channels = _channels(raw.get("channels"), where)
    if channels:
        mark["channels"] = channels
    for key, most in (("label", LABEL_MAX), ("text", TEXT_MAX)):
        value = raw.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise MarksError(f"{where}: {key} must be text.")
        cleaned = clean_text(value)
        if not cleaned:
            continue
        if len(cleaned) > most:
            raise MarksError(f"{where}: {key} has {len(cleaned)} characters; at most {most}.")
        if _EMAIL.search(cleaned):
            raise MarksError(f"{where}: {key} holds an email address, which is not allowed.")
        mark[key] = cleaned
    if raw.get("confidence") not in (None, ""):
        c = _num(raw.get("confidence"))
        if c is None or not 0 <= c <= 1:
            raise MarksError(f"{where}: confidence must be a number from 0 to 1.")
        mark["confidence"] = c
    return mark


def check_marks(raw: Any, *, most: int = MAX_PER_CALL) -> list[dict[str, Any]]:
    """Every mark checked, or :class:`MarksError` for the first bad one."""
    if not isinstance(raw, list) or not raw:
        raise MarksError("marks must be a list of at least one mark.")
    if len(raw) > most:
        raise MarksError(f"There are {len(raw)} marks; at most {most} fit in one call.")
    return [check_mark(m, i + 1) for i, m in enumerate(raw)]


def _rows_from_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip().lower() for h in reader.fieldnames or []]
    if "kind" not in header or "t0" not in header or "t1" not in header:
        raise MarksError(
            "The CSV needs a header row with kind, t0 and t1 (and optionally channels, label, "
            "confidence, text)."
        )
    extra = [h for h in header if h and h not in COLUMNS]
    if extra:
        raise MarksError(f"The CSV has columns this does not know: {', '.join(extra[:5])}.")
    rows = []
    for row in reader:
        clean = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not any(clean.values()):
            continue
        rows.append(clean)
    return rows


def read_marks_file(path: str) -> list[dict[str, Any]]:
    """Marks from a CSV (header ``kind,t0,t1[,channels,label,confidence,text]``) or JSON file
    (a list of marks, or ``{"marks": [...]}``), checked. Channels in CSV are separated by ``;``.
    """
    p = Path(path).expanduser()
    name = p.name or "the file"
    if not p.is_file():
        raise MarksError(f"{name} was not found. Check the path.")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise MarksError(f"{name} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
    try:
        text = p.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise MarksError(f"{name} could not be read as UTF-8 text.") from None
    if p.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except ValueError:
            raise MarksError(f"{name} is not valid JSON.") from None
        if isinstance(data, dict):
            data = data.get("marks")
        if not isinstance(data, list):
            raise MarksError(f"{name} must hold a list of marks, or an object with marks.")
        rows: list[Any] = data
    elif p.suffix.lower() == ".csv":
        rows = _rows_from_csv(text)
    else:
        raise MarksError(f"{name} must be a .csv or .json file.")
    if not rows:
        raise MarksError(f"{name} holds no marks.")
    return check_marks(rows, most=MAX_FILE_MARKS)


def batches(marks: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split marks into calls of at most 500 marks and under the body limit, in order."""
    out: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for mark in marks:
        n = len(json.dumps(mark, separators=(",", ":"))) + 1
        if current and (len(current) >= MAX_PER_CALL or size + n > MAX_CALL_BYTES):
            out.append(current)
            current, size = [], 0
        current.append(mark)
        size += n
    if current:
        out.append(current)
    return out


def op_id_for(recording_id: str, marks: list[dict[str, Any]], prefix: str = "auto") -> str:
    """A repeatable opId for one call: the same recording and marks give the same id, so a
    retry within 24 hours is answered from the first call and writes nothing twice."""
    digest = hashlib.sha256(
        json.dumps([recording_id, marks], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"{prefix}-{digest[:40]}"[:64]
