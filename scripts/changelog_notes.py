"""Print the CHANGELOG.md section for one version, for use as release notes.

Usage:
    python scripts/changelog_notes.py 0.2.0

Prints the text under '## [0.2.0]' up to the next version heading or the link list
at the end. Exits 1 if there is no such section. Standard library only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def section(version: str, text: str) -> str | None:
    """Return the body of the changelog section for a version, or None."""
    lines = text.splitlines()
    heading = re.compile(rf"^## \[{re.escape(version)}\]")
    start = next((i for i, line in enumerate(lines) if heading.match(line)), None)
    if start is None:
        return None
    body: list[str] = []
    for line in lines[start + 1 :]:
        # Stop at the next version heading or the link reference list.
        if line.startswith("## ") or re.match(r"^\[[^\]]+\]: ", line):
            break
        body.append(line)
    return "\n".join(body).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("Usage: python scripts/changelog_notes.py X.Y.Z", file=sys.stderr)
        return 2
    version = args[0].removeprefix("v")
    notes = section(version, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    if notes is None:
        print(f"CHANGELOG.md has no section '## [{version}]'.", file=sys.stderr)
        return 1
    sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
