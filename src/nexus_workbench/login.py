"""Sign-in flows: browser approval (contract section 5) and a pasted token."""

from __future__ import annotations

import contextlib
import getpass
import math
import socket
import sys
import unicodedata
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO

from . import auth
from .client import (
    CONNECT_PAGE_HINT,
    CodeWrong,
    Forbidden,
    InvalidArgument,
    LoginExpired,
    LoginNotAvailable,
    RateLimited,
    TokenExpired,
    Unauthenticated,
    WorkbenchClient,
    WorkbenchError,
    display_name,
    safe_text,
)

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CODE_LENGTH = 8
MAX_LOCAL_RETRIES = 10
MAX_PENDING_REPLIES = 10
DEVICE_NAME_LIMIT = 64
FALLBACK_DEVICE_NAME = "computer"


class LoginFailed(Exception):
    """Sign-in did not complete. The message says what to do next."""


def normalize_code(raw: str) -> str:
    """Uppercase and drop spaces and hyphens (contract 5.3)."""
    return "".join(ch for ch in raw.upper() if ch not in " -\t")


def code_problem(code: str) -> str | None:
    """A sentence describing what is wrong with a normalised code, or None if it looks right."""
    if len(code) != CODE_LENGTH:
        return f"A sign-in code has {CODE_LENGTH} characters, like ABCD-EFGH. Type it again."
    bad = sorted({ch for ch in code if ch not in CODE_ALPHABET})
    if bad:
        return (
            "A sign-in code never contains "
            + ", ".join(bad)
            + ". Check the code on the Workbench page and type it again."
        )
    return None


def device_name() -> str:
    """This computer's name, cleaned the way the server cleans it (contract 5.1).

    Control and invisible characters are dropped, other spaces become plain spaces, and the
    name is trimmed and cut to 64 characters. A name with no letter or digit left, or a
    host name that cannot be read, becomes ``computer``.
    """
    try:
        raw = socket.gethostname()
    except (OSError, UnicodeError):
        return FALLBACK_DEVICE_NAME
    if not isinstance(raw, str):
        return FALLBACK_DEVICE_NAME
    kept = []
    for ch in raw:
        if ch.isprintable():
            kept.append(ch)
        elif unicodedata.category(ch) == "Zs":
            kept.append(" ")
    name = "".join(kept).strip()[:DEVICE_NAME_LIMIT].strip()
    if not any(ch.isalnum() for ch in name):
        return FALLBACK_DEVICE_NAME
    return name


def sign_in_failure(exc: WorkbenchError) -> LoginFailed:
    """The sentence a person sees when the server refuses a sign-in step."""
    if isinstance(exc, LoginExpired):
        return LoginFailed(
            "This sign-in ran out of time or was cancelled. "
            "Run `nexus-workbench login` to start again."
        )
    if isinstance(exc, Forbidden):
        return LoginFailed(
            "Workbench refused this sign-in: you may no longer be a member of that workspace, "
            "or it is closed. Ask the workspace owner, then run `nexus-workbench login` again."
        )
    if isinstance(exc, RateLimited):
        if exc.retry_after is not None:
            seconds = max(1, math.ceil(exc.retry_after))
            noun = "second" if seconds == 1 else "seconds"
            wait = f"{seconds} {noun}"
        else:
            wait = "a minute"
        return LoginFailed(
            f"Too many sign-in requests to Workbench. Try again in {wait} "
            "with `nexus-workbench login`."
        )
    # Token limit, early access, network and the rest already read as one clear sentence.
    return LoginFailed(str(exc))


@dataclass
class LoginIO:
    """Everything the flows touch outside the process, so tests can replace it."""

    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    stdin: TextIO = field(default_factory=lambda: sys.stdin)
    # Looked up at call time, so the standard functions can be patched in tests.
    prompt: Callable[[str], str] = lambda text: input(text)  # noqa: E731
    hidden_prompt: Callable[[str], str] = lambda text: getpass.getpass(text)  # noqa: E731
    open_browser: Callable[[str], Any] = lambda url: webbrowser.open(url)  # noqa: E731

    def say(self, text: str = "") -> None:
        print(text, file=self.out)


@dataclass
class LoginResult:
    where: str  # "file" or "keyring"
    whoami: dict[str, Any] | None
    whoami_error: WorkbenchError | None


def read_token(io: LoginIO) -> str:
    """Read a token from a hidden prompt, or from stdin when stdin is not a terminal."""
    try:
        interactive = io.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if interactive:
        raw = io.hidden_prompt("Paste your Workbench token (it will not be shown): ")
    else:
        raw = io.stdin.read()
    token = raw.strip()
    if not token:
        raise LoginFailed(
            "No token was given. "
            + CONNECT_PAGE_HINT
            + " Then run `nexus-workbench login --token`."
        )
    return token


def _finish(
    client: WorkbenchClient,
    token: str,
    *,
    use_keyring: bool,
    token_id_value: str | None = None,
    workspace_id: str | None = None,
) -> LoginResult:
    """Check the token with whoami, then save it. A token the server refuses is not saved."""
    auth.validate_token(token)
    probe = client.with_token(token)
    whoami: dict[str, Any] | None = None
    whoami_error: WorkbenchError | None = None
    try:
        whoami = probe.call("whoami")
    except (Unauthenticated, TokenExpired) as exc:
        raise LoginFailed(f"{exc.message} {CONNECT_PAGE_HINT}") from None
    except WorkbenchError as exc:
        whoami_error = exc
    finally:
        probe.close()
    if whoami and isinstance(whoami.get("workspace"), dict):
        workspace_id = workspace_id or whoami["workspace"].get("id")
    if whoami and isinstance(whoami.get("token"), dict):
        token_id_value = token_id_value or whoami["token"].get("id")
    where = auth.save_token(
        client.api_base,
        token,
        workspace_id=workspace_id,
        token_id_value=token_id_value,
        use_keyring=use_keyring,
    )
    return LoginResult(where=where, whoami=whoami, whoami_error=whoami_error)


def login_with_token(client: WorkbenchClient, io: LoginIO, *, use_keyring: bool) -> LoginResult:
    token = read_token(io)
    try:
        auth.validate_token(token)
    except auth.InvalidTokenError as exc:
        raise LoginFailed(str(exc)) from None
    return _finish(client, token, use_keyring=use_keyring)


def login_with_browser(client: WorkbenchClient, io: LoginIO, *, use_keyring: bool) -> LoginResult:
    """Browser approval flow. Raises LoginNotAvailable when the server does not offer it."""
    try:
        session = client.cli_login_start(device_name=device_name())
    except LoginNotAvailable:
        raise
    except WorkbenchError as exc:
        raise sign_in_failure(exc) from None
    url = session["approvalUrl"]
    io.say("To sign in, approve this computer in Workbench:")
    io.say(f"  {url}")
    # The URL is printed above, so a browser that does not open is not an error.
    with contextlib.suppress(Exception):
        io.open_browser(url)
    io.say("After you approve, Workbench shows a code like ABCD-EFGH.")

    local_retries = 0
    pending_replies = 0
    while True:
        try:
            raw = io.prompt("Code: ")
        except (EOFError, KeyboardInterrupt):
            raise LoginFailed(
                "Sign-in was cancelled. Run `nexus-workbench login` to start again."
            ) from None
        code = normalize_code(raw)
        if not code:
            raise LoginFailed("Sign-in was cancelled. Run `nexus-workbench login` to start again.")
        problem = code_problem(code)
        if problem:
            local_retries += 1
            if local_retries > MAX_LOCAL_RETRIES:
                raise LoginFailed(
                    "Too many unreadable codes. Run `nexus-workbench login` to start again."
                )
            print(problem, file=io.err)
            continue
        try:
            data = client.cli_login_exchange(
                session_id=session["sessionId"], poll_secret=session["pollSecret"], code=code
            )
        except CodeWrong as exc:
            pending_replies = 0
            print(str(exc), file=io.err)
            if exc.attempts_left is not None and exc.attempts_left <= 0:
                raise LoginFailed(
                    "No attempts are left. Run `nexus-workbench login` to start again."
                ) from None
            continue
        except InvalidArgument:
            # Contract 5.1: a code sent before the person approves is refused this way and
            # uses no attempt. Our codes are always 8 checked characters, so nothing else
            # we send to exchange can be refused like this.
            pending_replies += 1
            if pending_replies >= MAX_PENDING_REPLIES:
                raise LoginFailed(
                    "This computer was not approved in the browser. "
                    "Run `nexus-workbench login` to start again."
                ) from None
            print(
                "Approve this computer in your browser first, then type the code shown there.",
                file=io.err,
            )
            continue
        except LoginNotAvailable:
            raise
        except WorkbenchError as exc:
            raise sign_in_failure(exc) from None
        break

    token = data["token"]
    try:
        auth.validate_token(token)
    except auth.InvalidTokenError:
        raise LoginFailed(
            "Workbench sent a token this version cannot read. "
            "Update with `uv tool upgrade nexus-workbench` and try again."
        ) from None
    return _finish(
        client,
        token,
        use_keyring=use_keyring,
        token_id_value=data.get("tokenId"),
        workspace_id=data.get("workspaceId"),
    )


def run_login(
    client: WorkbenchClient,
    io: LoginIO,
    *,
    token_prompt: bool = False,
    use_keyring: bool = False,
) -> LoginResult:
    """``login``: the browser flow first, else (or with --token) a pasted token."""
    if not token_prompt:
        try:
            return login_with_browser(client, io, use_keyring=use_keyring)
        except LoginNotAvailable:
            io.say("Signing in from the terminal is not available yet, so use a token instead.")
            io.say(CONNECT_PAGE_HINT)
    return login_with_token(client, io, use_keyring=use_keyring)


def describe_result(result: LoginResult, io: LoginIO) -> None:
    where = "the system keyring" if result.where == "keyring" else "your credentials file"
    if result.whoami:
        workspace = result.whoami.get("workspace") or {}
        token = result.whoami.get("token") or {}
        io.say(f"Signed in to workspace: {display_name(workspace.get('name'))}")
        io.say(f"Token expires: {safe_text(token.get('expiresAt')) or 'unknown'}")
    else:
        io.say("Token saved.")
        if result.whoami_error is not None:
            print(f"Workbench could not confirm it yet: {result.whoami_error}", file=io.err)
    io.say(f"Saved in {where}.")
