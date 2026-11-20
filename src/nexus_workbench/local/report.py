"""Shared plumbing for the local tools: safe file access, opaque ids and JSON-safe results.

Results name a file only by an id derived from its bytes. Error messages may name the base
name the caller passed, never the folder it sits in.
"""

from __future__ import annotations

import errno
import hashlib
import math
import os
import stat
from collections import OrderedDict
from typing import Any

import numpy as np

from .errors import LocalToolError

#: Text inputs (hypnograms, event lists) larger than this are refused.
TEXT_CAP_BYTES = 10 * 1024 * 1024
#: Length of the opaque id: this many hex characters of the SHA-256 of the file's bytes.
ID_HEX = 12

_HASH_CHUNK = 4 << 20
_ID_CACHE: OrderedDict[tuple[str, int, int, int, int], str] = OrderedDict()
_ID_CACHE_SIZE = 32


def display_name(path: str) -> str:
    """The base name of what the caller passed, for error messages only."""
    if not isinstance(path, str):
        return "the file"
    base = os.path.basename(path.rstrip("/\\"))
    return base or "the file"


def check_regular_file(path: object, what: str = "file") -> str:
    """Return a usable path for ``path`` or raise :class:`LocalToolError`.

    Refuses anything that is not a regular file: folders, devices, pipes, sockets, missing
    files and symbolic link loops. Symbolic links to a regular file are followed.
    """
    if not isinstance(path, str) or not path.strip():
        raise LocalToolError(f"No {what} path was given. Pass the path to the {what} as text.")
    p = os.path.expanduser(path.strip())
    name = display_name(p)
    try:
        st = os.stat(p)
    except FileNotFoundError:
        raise LocalToolError(
            f"{name} was not found. Check the path and that the file exists."
        ) from None
    except PermissionError:
        raise LocalToolError(
            f"{name} could not be opened: permission denied. Check the file's permissions."
        ) from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LocalToolError(
                f"{name} is a symbolic link that loops back on itself. Pass the real file."
            ) from None
        raise LocalToolError(
            f"{name} could not be opened ({errno.errorcode.get(exc.errno or 0, 'OS error')}). "
            "Check the path."
        ) from None
    if stat.S_ISDIR(st.st_mode):
        raise LocalToolError(f"{name} is a folder, not a file. Pass the path to one {what}.")
    if not stat.S_ISREG(st.st_mode):
        raise LocalToolError(
            f"{name} is not a regular file (it is a device, pipe or socket). Pass a {what} "
            "saved on disk."
        )
    return p


def read_text_file(path: str, what: str) -> tuple[str, str]:
    """Read a small text input. Returns ``(text, file_id)``.

    Refuses files over :data:`TEXT_CAP_BYTES` and files that are not UTF-8 or Latin-1 text.
    """
    p = check_regular_file(path, what)
    name = display_name(p)
    size = os.stat(p).st_size
    if size > TEXT_CAP_BYTES:
        raise LocalToolError(
            f"{name} is {size / 1e6:.1f} MB; {what} files are limited to "
            f"{TEXT_CAP_BYTES // (1024 * 1024)} MB. Check that this is the right file."
        )
    try:
        with open(p, "rb") as f:
            raw = f.read(TEXT_CAP_BYTES + 1)
    except OSError:
        raise LocalToolError(f"{name} could not be read. Check the file's permissions.") from None
    if b"\x00" in raw:
        raise LocalToolError(f"{name} is not a text file. A {what} must be plain text or CSV.")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text, hashlib.sha256(raw).hexdigest()[:ID_HEX]


def content_id(path: str) -> str:
    """First :data:`ID_HEX` hex characters of the SHA-256 of the whole file, read in chunks.

    Cached per process on the file's identity and modification time, so several tools run on
    one file hash it once.
    """
    st = os.stat(path)
    key = (os.path.realpath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    hit = _ID_CACHE.get(key)
    if hit is not None:
        _ID_CACHE.move_to_end(key)
        return hit
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                buf = f.read(_HASH_CHUNK)
                if not buf:
                    break
                h.update(buf)
    except OSError:
        raise LocalToolError(
            f"{display_name(path)} could not be read. Check the file's permissions."
        ) from None
    rid = h.hexdigest()[:ID_HEX]
    _ID_CACHE[key] = rid
    while len(_ID_CACHE) > _ID_CACHE_SIZE:
        _ID_CACHE.popitem(last=False)
    return rid


def sig(x: float | None, digits: int = 4) -> float | None:
    """``x`` rounded to ``digits`` significant digits; ``None`` for missing or non-finite."""
    if x is None:
        return None
    v = float(x)
    if not math.isfinite(v):
        return None
    if v == 0.0:
        return 0.0
    return float(f"{v:.{digits}g}")


def rnd(x: float | None, places: int = 4) -> float | None:
    """``x`` rounded to ``places`` decimals; ``None`` for missing or non-finite."""
    if x is None:
        return None
    v = float(x)
    if not math.isfinite(v):
        return None
    return round(v, places) + 0.0  # + 0.0 turns -0.0 into 0.0


def pct(part: int, whole: int) -> float | None:
    """A percentage to two decimals, ``None`` when ``whole`` is zero."""
    return rnd(100.0 * part / whole, 2) if whole else None


def json_safe(x: Any) -> Any:
    """Recursively convert to plain JSON types. Non-finite floats become ``None``."""
    if isinstance(x, dict):
        return {str(k): json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [json_safe(v) for v in x]
    if isinstance(x, np.ndarray):
        return json_safe(x.tolist())
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        v = float(x)
        return v if math.isfinite(v) else None
    if x is None or isinstance(x, str):
        return x
    raise TypeError(f"not JSON-safe: {type(x).__name__}")
