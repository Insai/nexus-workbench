"""Set or check the package version in every place it is written.

Usage:
    python scripts/bump_version.py 0.2.0        set the version everywhere
    python scripts/bump_version.py --show       print the version found in each file
    python scripts/bump_version.py --check v0.2.0
                                                exit 1 unless every file has this
                                                release version and the changelog
                                                has a dated section for it

Standard library only, so it runs before any install.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PYPROJECT = "pyproject.toml"
INIT = "src/nexus_workbench/__init__.py"
MANIFESTS = (".claude-plugin/plugin.json", ".codex-plugin/plugin.json")
#: The MCP Registry entry: the server's version and its PyPI package's version.
SERVER_JSON = "server.json"
CHANGELOG = "CHANGELOG.md"

# The version lines in the two Python-side files.
PATTERNS = {
    PYPROJECT: re.compile(r'^(version = ")([^"]+)(")$', re.MULTILINE),
    INIT: re.compile(r'^(__version__ = ")([^"]+)(")$', re.MULTILINE),
}

# A plain release version: X.Y.Z, nothing after it.
RELEASE = re.compile(r"^\d+\.\d+\.\d+$")
# Loose check for the set command: a release, or a release with a PEP 440 suffix.
ANY_VERSION = re.compile(r"^\d+\.\d+\.\d+((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?$")


def read_versions(root: Path = ROOT) -> dict[str, str]:
    """Return the version written in each file. server.json counts once and must agree with
    itself."""
    found: dict[str, str] = {}
    for name, pattern in PATTERNS.items():
        text = (root / name).read_text(encoding="utf-8")
        matches = pattern.findall(text)
        if len(matches) != 1:
            raise SystemExit(f"{name}: expected one version line, found {len(matches)}.")
        found[name] = matches[0][1]
    for name in MANIFESTS:
        data = json.loads((root / name).read_text(encoding="utf-8"))
        found[name] = str(data.get("version", ""))
    data = json.loads((root / SERVER_JSON).read_text(encoding="utf-8"))
    versions = {str(data.get("version", ""))}
    versions |= {str(p.get("version", "")) for p in data.get("packages") or []}
    if len(versions) != 1:
        raise SystemExit(f"{SERVER_JSON}: the server and package versions differ: {versions}.")
    found[SERVER_JSON] = versions.pop()
    return found


def write_version(version: str, root: Path = ROOT) -> None:
    """Write the version into every file."""
    for name, pattern in PATTERNS.items():
        path = root / name
        text = path.read_text(encoding="utf-8")
        new_text, count = pattern.subn(lambda m: f"{m.group(1)}{version}{m.group(3)}", text)
        if count != 1:
            raise SystemExit(f"{name}: expected one version line, found {count}.")
        path.write_text(new_text, encoding="utf-8")
    for name, expected in (*((m, 1) for m in MANIFESTS), (SERVER_JSON, 2)):
        path = root / name
        # Replace the value in place so the rest of the file keeps its layout.
        text = path.read_text(encoding="utf-8")
        new_text, count = re.subn(
            r'("version"\s*:\s*")[^"]*(")', lambda m: f"{m.group(1)}{version}{m.group(2)}", text
        )
        if count != expected:
            raise SystemExit(f"{name}: expected {expected} version fields, found {count}.")
        path.write_text(new_text, encoding="utf-8")


def changelog_heading(version: str, root: Path = ROOT) -> str | None:
    """Return the changelog heading line for a version, or None."""
    text = (root / CHANGELOG).read_text(encoding="utf-8")
    pattern = re.compile(rf"^## \[{re.escape(version)}\].*$", re.MULTILINE)
    match = pattern.search(text)
    return match.group(0) if match else None


def check_release(tag: str, root: Path = ROOT) -> list[str]:
    """Return the problems that stop this tag from being released. Empty means ready."""
    problems: list[str] = []
    if not tag.startswith("v"):
        problems.append(f"Tag {tag!r} must start with 'v', like v1.2.3.")
        return problems
    version = tag[1:]
    if "dev" in version:
        problems.append(f"Tag {tag!r} is a development version. Release tags have no 'dev'.")
    elif not RELEASE.match(version):
        problems.append(f"Tag {tag!r} is not a release version. Use vX.Y.Z.")
    for name, found in read_versions(root).items():
        if "dev" in found:
            problems.append(
                f"{name} has the development version {found}. "
                f"Run: python scripts/bump_version.py {version}"
            )
        elif found != version:
            problems.append(f"{name} has version {found}, but the tag is {tag}.")
    heading = changelog_heading(version, root)
    if heading is None:
        problems.append(f"{CHANGELOG} has no section '## [{version}]'.")
    elif "unreleased" in heading.lower():
        problems.append(f"{CHANGELOG} still says unreleased for {version}. Add the date.")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("version", nargs="?", help="the version to set, like 0.2.0")
    group.add_argument("--show", action="store_true", help="print the version in each file")
    group.add_argument("--check", metavar="TAG", help="check that TAG can be released")
    args = parser.parse_args(argv)

    if args.show:
        for name, found in read_versions().items():
            print(f"{found}  {name}")
        return 0

    if args.check:
        problems = check_release(args.check)
        if problems:
            print(f"Cannot release {args.check}:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            return 1
        print(f"{args.check} matches the version in every file and the changelog.")
        return 0

    if not ANY_VERSION.match(args.version):
        print(f"{args.version!r} is not a version like 1.2.3 or 1.2.3.dev0.", file=sys.stderr)
        return 2
    write_version(args.version)
    print(f"Set version {args.version} in {', '.join((PYPROJECT, INIT, *MANIFESTS, SERVER_JSON))}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
