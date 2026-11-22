"""Plain terminal tables: aligned columns, no dependencies, colour only where it is wanted.

Colour (a bold header row) is used only when the stream is a terminal and ``NO_COLOR`` is not
set (https://no-color.org). Every cell is plain one-line text before it is padded, so server
text cannot move the cursor or break the layout.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence
from typing import Any, TextIO

from .client import safe_text

BOLD = "\x1b[1m"
RESET = "\x1b[0m"
#: Widest a cell is shown; longer text ends in "…".
MAX_CELL = 60


def color_wanted(stream: TextIO | None = None) -> bool:
    """True when ``stream`` is a terminal and ``NO_COLOR`` is not set."""
    if os.environ.get("NO_COLOR", "") != "":
        return False
    stream = stream or sys.stdout
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def cell(value: Any, limit: int = MAX_CELL) -> str:
    """One cell as plain text: ``-`` for nothing, numbers as written, text cut to ``limit``."""
    if value is None or value == "":
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        text = f"{value:g}"
    else:
        text = " ".join(safe_text(value, limit=None).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(
    headers: Sequence[str],
    rows: Iterable[Sequence[Any]],
    *,
    right: Iterable[int] = (),
    stream: TextIO | None = None,
) -> str:
    """The table as text. ``right`` lists the columns (0-based) to right-align, for numbers."""
    body = [[cell(v) for v in row] for row in rows]
    table = [list(headers), *body]
    widths = [max(len(r[i]) for r in table) for i in range(len(headers))]
    align_right = set(right)

    def line(cells: Sequence[str]) -> str:
        parts = [
            c.rjust(w) if i in align_right else c.ljust(w)
            for i, (c, w) in enumerate(zip(cells, widths, strict=True))
        ]
        return "  ".join(parts).rstrip()

    head = line(table[0])
    if color_wanted(stream):
        head = BOLD + head + RESET
    return "\n".join([head, *(line(r) for r in body)])
