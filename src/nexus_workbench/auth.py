"""Token format checks and token storage.

Lookup order: the ``NEXUS_TOKEN`` environment variable, then the credentials
file. The OS keyring is optional and off by default, because an MCP server is
started without a terminal and a keychain prompt there would hang it.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import platformdirs

APP_NAME = "nexus-workbench"
TOKEN_PREFIX = "nxs_pat_"  # noqa: S105 - a prefix, not a secret
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
BODY_LENGTH = 43
CHECK_LENGTH = 6
TOKEN_LENGTH = len(TOKEN_PREFIX) + BODY_LENGTH + CHECK_LENGTH  # 57
TOKEN_RE = re.compile(r"^nxs_pat_[0-9A-Za-z]{49}$")
KEYRING_SERVICE = APP_NAME

ENV_TOKEN = "NEXUS_TOKEN"  # noqa: S105 - a variable name
ENV_KEYRING = "NEXUS_KEYRING"
ENV_CONFIG_DIR = "NEXUS_WORKBENCH_CONFIG_DIR"

_MAKE_ONE_HINT = 'Make a new one in Workbench: account menu, then "Connect an agent".'


class InvalidTokenError(ValueError):
    """The text is not a Workbench token. The message never contains the token."""


class CredentialsError(RuntimeError):
    """The credentials file cannot be used. The message says how to fix it."""


# ---------------------------------------------------------------------------
# Token format (contract section 2)
# ---------------------------------------------------------------------------


def base62_encode(value: int, width: int) -> str:
    """Encode a non-negative integer in base62, most significant digit first, padded."""
    if value < 0:
        raise ValueError("value must be non-negative")
    digits = []
    while value:
        value, rem = divmod(value, 62)
        digits.append(BASE62[rem])
    text = "".join(reversed(digits)) or "0"
    return text.rjust(width, "0")


def check_digits(body: str) -> str:
    """The 6-character base62 CRC-32 check of a token body."""
    return base62_encode(zlib.crc32(body.encode("ascii")) & 0xFFFFFFFF, CHECK_LENGTH)


def validate_token(token: str) -> None:
    """Raise InvalidTokenError unless ``token`` matches the contract's format and check digits."""
    if not isinstance(token, str) or not token.startswith(TOKEN_PREFIX):
        raise InvalidTokenError(
            f"That is not a Workbench token. A token starts with {TOKEN_PREFIX}. {_MAKE_ONE_HINT}"
        )
    if len(token) != TOKEN_LENGTH:
        raise InvalidTokenError(
            f"That token has {len(token)} characters, but a Workbench token has {TOKEN_LENGTH}. "
            "Copy the whole token and try again."
        )
    if not TOKEN_RE.match(token):
        raise InvalidTokenError(
            "That token contains characters a Workbench token never has. "
            "Copy the token again without extra spaces or quotes."
        )
    body = token[len(TOKEN_PREFIX) : len(TOKEN_PREFIX) + BODY_LENGTH]
    check = token[len(TOKEN_PREFIX) + BODY_LENGTH :]
    if check_digits(body) != check:
        raise InvalidTokenError(
            "That token fails its check, so it was probably mistyped or cut short. "
            "Copy it again, or make a new one in Workbench."
        )


def token_id(token: str) -> str:
    """Public token id: ``tok_`` plus the first 16 hex characters of the token's SHA-256."""
    return "tok_" + hashlib.sha256(token.encode("ascii")).hexdigest()[:16]


def mask_token(token: str | None) -> str:
    """Show a token as ``nxs_pat_…<last4>``. Never show more."""
    if not token:
        return "(none)"
    return f"{TOKEN_PREFIX}…{token[-4:]}"


# ---------------------------------------------------------------------------
# Credentials file
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    """Where the credentials file lives: ``NEXUS_WORKBENCH_CONFIG_DIR``, else the user config
    folder (macOS ``~/Library/Application Support/nexus-workbench``, Linux
    ``$XDG_CONFIG_HOME/nexus-workbench`` or ``~/.config/nexus-workbench``, Windows
    ``%LOCALAPPDATA%\\nexus-workbench``)."""
    override = os.environ.get(ENV_CONFIG_DIR, "").strip()
    if override:
        return Path(override).expanduser()
    return Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def _is_posix() -> bool:
    return os.name == "posix"


def _check_permissions(path: Path) -> None:
    if not _is_posix():
        return
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        raise CredentialsError(
            f"Your credentials file can be read by other users on this computer "
            f"(permissions {mode:o}), so it was not used. "
            f'Run: chmod 600 "{path}"  and then try again.'
        )


def _read_store(path: Path | None = None) -> dict[str, Any]:
    path = path or credentials_path()
    if not path.exists():
        return {}
    _check_permissions(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CredentialsError(
            f'Your credentials file could not be read. Delete "{path}" and run '
            "`nexus-workbench login` again."
        ) from exc
    if not isinstance(data, dict):
        raise CredentialsError(
            f'Your credentials file is not in the expected format. Delete "{path}" and run '
            "`nexus-workbench login` again."
        )
    return data


def _write_store(data: dict[str, Any], path: Path | None = None) -> None:
    """Write the store atomically: temp file in the same directory, then os.replace."""
    path = path or credentials_path()
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if _is_posix():
        os.chmod(directory, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=".credentials-", suffix=".tmp", dir=directory)
    try:
        if _is_posix():
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


# ---------------------------------------------------------------------------
# Keyring (optional)
# ---------------------------------------------------------------------------


def keyring_requested() -> bool:
    return os.environ.get(ENV_KEYRING, "").strip().lower() in {"1", "true", "yes", "on"}


def _notice(message: str) -> None:
    print(message, file=sys.stderr)


def _keyring_set(api_base: str, token: str) -> bool:
    try:
        import keyring  # noqa: PLC0415 - optional dependency, imported lazily

        keyring.set_password(KEYRING_SERVICE, api_base, token)
    except Exception:  # noqa: BLE001 - any keyring failure falls back to the file
        _notice("The system keyring is not available, so the token was saved to the file instead.")
        return False
    return True


def _keyring_get(api_base: str) -> str | None:
    try:
        import keyring  # noqa: PLC0415

        return keyring.get_password(KEYRING_SERVICE, api_base)
    except Exception:  # noqa: BLE001
        _notice("The system keyring could not be read.")
        return None


def _keyring_delete(api_base: str) -> None:
    try:
        import keyring  # noqa: PLC0415

        keyring.delete_password(KEYRING_SERVICE, api_base)
    except Exception:  # noqa: BLE001
        _notice("The token could not be removed from the system keyring.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredToken:
    token: str | None
    source: str  # "env", "file", "keyring" or "none"
    token_id: str | None = None
    workspace_id: str | None = None
    saved_at: str | None = None
    #: The token's scopes as saved with it (contract 7.10); None when not known.
    scopes: tuple[str, ...] | None = None

    @property
    def masked(self) -> str:
        return mask_token(self.token)


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _scopes_of(value: Any) -> tuple[str, ...] | None:
    from .scopes import normalise  # noqa: PLC0415 - avoids an import cycle

    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        return None
    return normalise(value)


def save_token(
    api_base: str,
    token: str,
    *,
    workspace_id: str | None = None,
    token_id_value: str | None = None,
    scopes: list[str] | tuple[str, ...] | None = None,
    use_keyring: bool = False,
    path: Path | None = None,
) -> str:
    """Validate and store a token for ``api_base``. Returns where it went: "file" or "keyring".

    ``scopes`` are saved beside the token when known, so commands and the MCP server can tell
    what it may do without a call.
    """
    validate_token(token)
    store = _read_store(path)
    record: dict[str, Any] = {
        "tokenId": token_id_value or token_id(token),
        "workspaceId": workspace_id,
        "savedAt": _now(),
    }
    known = _scopes_of(list(scopes)) if scopes is not None else None
    if known is not None:
        record["scopes"] = list(known)
    where = "file"
    if use_keyring and _keyring_set(api_base, token):
        record["token"] = None
        record["keyring"] = True
        where = "keyring"
    else:
        record["token"] = token
    store[api_base] = record
    _write_store(store, path)
    return where


def load_token(api_base: str, *, path: Path | None = None) -> StoredToken:
    """Find the token for ``api_base``: env first, then the keyring if chosen, then the file.

    Raises CredentialsError (file unusable) or InvalidTokenError (a malformed token).
    """
    env_token = os.environ.get(ENV_TOKEN, "").strip()
    if env_token:
        try:
            validate_token(env_token)
        except InvalidTokenError as exc:
            raise InvalidTokenError(
                f"The {ENV_TOKEN} environment variable is not usable. {exc}"
            ) from None
        return StoredToken(token=env_token, source="env", token_id=token_id(env_token))

    store = _read_store(path)
    record = store.get(api_base)
    if not isinstance(record, dict):
        return StoredToken(token=None, source="none")
    meta = {
        "token_id": record.get("tokenId"),
        "workspace_id": record.get("workspaceId"),
        "saved_at": record.get("savedAt"),
        "scopes": _scopes_of(record.get("scopes")),
    }
    if record.get("keyring"):
        token = _keyring_get(api_base)
        if token:
            return StoredToken(token=token, source="keyring", **meta)
        return StoredToken(token=None, source="none", **meta)
    token = record.get("token")
    if not isinstance(token, str) or not token:
        return StoredToken(token=None, source="none", **meta)
    try:
        validate_token(token)
    except InvalidTokenError:
        raise CredentialsError(
            "The saved token is damaged. "
            "Run `nexus-workbench logout`, then `nexus-workbench login`."
        ) from None
    return StoredToken(token=token, source="file", **meta)


def save_scopes(
    api_base: str,
    token_id_value: str,
    scopes: list[str] | tuple[str, ...],
    *,
    path: Path | None = None,
) -> bool:
    """Record the scopes the server reported for the saved token of ``api_base``.

    Only when the saved token is the one with ``token_id_value``. Returns True when the file
    changed. A token from ``NEXUS_TOKEN`` is not saved, so its scopes are not either.
    """
    known = _scopes_of(list(scopes))
    store = _read_store(path)
    record = store.get(api_base)
    if known is None or not isinstance(record, dict) or record.get("tokenId") != token_id_value:
        return False
    if _scopes_of(record.get("scopes")) == known:
        return False
    record["scopes"] = list(known)
    _write_store(store, path)
    return True


def delete_token(api_base: str, *, path: Path | None = None) -> bool:
    """Remove the stored token for ``api_base``. Returns True if one was removed."""
    store = _read_store(path)
    record = store.pop(api_base, None)
    if record is None:
        return False
    if isinstance(record, dict) and record.get("keyring"):
        _keyring_delete(api_base)
    _write_store(store, path)
    return True
