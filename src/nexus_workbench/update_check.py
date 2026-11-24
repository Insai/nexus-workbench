"""A one-line notice when a newer nexus-workbench is on PyPI.

At most once a day this asks PyPI's JSON API for the newest release, with a short timeout,
and keeps the answer in the user cache folder. Any failure (offline, a timeout, a cache
that cannot be read or written) is silent and simply means no notice. Nothing is sent but
the request for the package's public page; there is no telemetry.

The MCP server starts the check in the background on its first tool call, so a call never
waits for it, and adds the notice to one tool result per process. ``doctor`` prints it.

Set ``NEXUS_WORKBENCH_NO_UPDATE_CHECK=1`` to turn it off: then nothing is fetched at all.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path

import platformdirs

from . import __version__

logger = logging.getLogger(__name__)

ENV_OPT_OUT = "NEXUS_WORKBENCH_NO_UPDATE_CHECK"
PYPI_URL = "https://pypi.org/pypi/nexus-workbench/json"
TIMEOUT_SECONDS = 2.0
#: How long one answer from PyPI (or one failed attempt) is kept.
CHECK_EVERY_SECONDS = 24 * 3600
CACHE_FILE = "update-check.json"
UPGRADE = "uv tool upgrade nexus-workbench"

_RELEASE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

Fetcher = Callable[[], "str | None"]


def disabled() -> bool:
    """Whether the person turned the check off."""
    return os.environ.get(ENV_OPT_OUT, "").strip() not in ("", "0")


def cache_path() -> Path:
    return Path(platformdirs.user_cache_dir("nexus-workbench", appauthor=False)) / CACHE_FILE


def release(version: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` of a version, or None. A suffix such as ``.dev0`` is ignored,
    so ``0.2.1.dev0`` counts as ``0.2.1``."""
    match = _RELEASE.match(version.strip()) if isinstance(version, str) else None
    if not match:
        return None
    major, minor, patch = (int(g) for g in match.groups())
    return major, minor, patch


def fetch_latest() -> str | None:
    """The newest version on PyPI, or None. Never raises."""
    try:
        import httpx  # noqa: PLC0415

        response = httpx.get(
            PYPI_URL,
            timeout=TIMEOUT_SECONDS,
            follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": f"nexus-workbench/{__version__}"},
        )
        if response.status_code != 200:
            return None
        version = response.json()["info"]["version"]
        return version if isinstance(version, str) and release(version) else None
    except Exception:  # noqa: BLE001  (offline, a timeout, an odd answer: no notice)
        return None


def _read_cache(path: Path, now: float) -> tuple[bool, str | None]:
    """``(fresh, latest)`` from the cache file; ``(False, None)`` when it is missing or old."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        checked = float(data["checkedAt"])
        latest = data.get("latest")
    except Exception:  # noqa: BLE001
        return False, None
    if not 0 <= now - checked < CHECK_EVERY_SECONDS:
        return False, None
    return True, latest if isinstance(latest, str) else None


def _write_cache(path: Path, now: float, latest: str | None) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checkedAt": now, "latest": latest}), encoding="utf-8")
    except Exception:  # noqa: BLE001  (a cache that cannot be written: ask again next time)
        logger.debug("could not write the update check cache")


def latest_version(
    *, fetch: Fetcher | None = None, path: Path | None = None, now: float | None = None
) -> str | None:
    """The newest release, from the cache when it is less than a day old, else from PyPI.

    A failed fetch is cached too, so an offline computer asks at most once a day.
    """
    if disabled():
        return None
    try:
        path = path or cache_path()
        now = time.time() if now is None else now
        fresh, latest = _read_cache(path, now)
        if fresh:
            return latest
        latest = (fetch or fetch_latest)()
        _write_cache(path, now, latest)
        return latest
    except Exception:  # noqa: BLE001
        return None


def notice_for(latest: str | None, current: str = __version__) -> str | None:
    """The notice text when ``latest`` is newer than ``current``, else None."""
    new, have = release(latest or ""), release(current)
    if new is None or have is None or new <= have:
        return None
    return f"nexus-workbench {latest} is available (this is {current})."


def agent_notice(notice: str) -> str:
    """The notice as an MCP tool result carries it."""
    return (
        f"Notice: {notice} To update, the researcher runs `{UPGRADE}` in a terminal, then "
        "restarts the agent session. Mention this once; it needs no other action."
    )


def check(*, fetch: Fetcher | None = None, path: Path | None = None) -> str | None:
    """The notice, or None. Waits for PyPI at most ``TIMEOUT_SECONDS`` (for ``doctor``)."""
    return notice_for(latest_version(fetch=fetch, path=path))


class Notifier:
    """Runs the check once in the background and hands out its notice once.

    ``start`` returns at once; ``take`` returns the notice the first time it is ready and
    None after that, so one process shows it at most once.
    """

    def __init__(self, fetch: Fetcher | None = None, path: Path | None = None) -> None:
        self._fetch = fetch
        self._path = path
        self._lock = threading.Lock()
        self._started = False
        self._notice: str | None = None
        self._taken = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if disabled():
            return
        with self._lock:
            if self._started:
                return
            self._started = True
        self._thread = threading.Thread(target=self._run, name="update-check", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        notice = check(fetch=self._fetch, path=self._path)
        with self._lock:
            self._notice = notice

    def join(self, timeout: float | None = None) -> None:
        """Wait for the background check (tests only)."""
        if self._thread is not None:
            self._thread.join(timeout)

    def take(self) -> str | None:
        with self._lock:
            if self._taken or self._notice is None:
                return None
            self._taken = True
            return self._notice
