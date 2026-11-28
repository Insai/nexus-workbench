"""Sign-in flows: browser approval (contract section 5) and a pasted token."""

from __future__ import annotations

import contextlib
import getpass
import math
import socket
import sys
import time
import unicodedata
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TextIO
from urllib.parse import urlsplit

from . import auth
from . import scopes as sc
from .client import (
    CONNECT_PAGE_HINT,
    CodeWrong,
    Forbidden,
    InvalidArgument,
    LoginExpired,
    LoginNotAvailable,
    NetworkError,
    RateLimited,
    TokenExpired,
    Unauthenticated,
    UnexpectedResponse,
    WorkbenchClient,
    WorkbenchError,
    WriteAccessRequired,
    display_name,
    safe_text,
)

CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CODE_LENGTH = 8
MAX_LOCAL_RETRIES = 10
MAX_PENDING_REPLIES = 10
DEVICE_NAME_LIMIT = 64
#: Seconds between ``status`` calls while the person approves in the browser.
POLL_INTERVAL = 2.0
#: Polls before giving up: a sign-in lives 10 minutes (contract 5.1).
MAX_POLLS = 300
#: ``status`` calls in a row that may fail to reach Workbench before the code is asked for.
MAX_STATUS_NETWORK_FAILURES = 3
#: The longest pause between ``status`` calls after a network failure.
MAX_NETWORK_BACKOFF = 16.0
FALLBACK_DEVICE_NAME = "computer"
#: Printed when a sign-in asks for a write scope (contract 7.9).
WRITE_SCOPES_NOTE = (
    "Write scopes can be approved only in a workspace where Insai has switched agent writes "
    "on. Elsewhere the approval page refuses them; sign in again without them."
)


class LoginFailed(Exception):
    """Sign-in did not complete. The message says what to do next."""


class LoginCancelled(LoginFailed):
    """No token was given: the person cancelled (Ctrl-C, Ctrl-D, an empty answer) or there
    was no input. ``setup`` treats this as a skipped step, not a failure."""


CANCELLED_MESSAGE = "Sign-in was cancelled. Run `nexus-workbench login` to start again."
APPROVE_PROMPT = (
    "After you approve, Workbench shows a code like ABCD-EFGH. If it says none of your "
    "workspaces has agent access yet, request it on that page and run "
    "`nexus-workbench login` again once it's on."
)
WAITING_MESSAGE = "Waiting for you to approve in the browser..."
STILL_WAITING_NOTE = "Still waiting. Switch account and reload the page, or press Ctrl-C to stop."
STATUS_UNREACHABLE_NOTE = (
    "Could not check the sign-in with Workbench. Approve in the browser, then type the code "
    "shown there."
)


def workbench_home(approval_url: str) -> str | None:
    """The Workbench app the approval page belongs to: a page that does not expire.

    The approval URL is ``<workbench>/cli/login/<session>`` and lives only as long as the
    sign-in, so it is never the place to send someone later.
    """
    try:
        parts = urlsplit(approval_url)
    except ValueError:
        return None
    if parts.scheme != "https" or not parts.netloc:
        return None
    base = parts.path.split("/cli/login/", 1)[0].rstrip("/")
    return f"https://{parts.netloc}{base}/"


def no_agent_access_message(approval_url: str) -> str:
    home = workbench_home(approval_url)
    where = f"Workbench ({home})" if home else "Workbench"
    return (
        f"None of your workspaces has agent access yet. Request it in {where}: account menu, "
        "Connect an agent, Request agent access. Run `nexus-workbench login` again once it's on."
    )


NO_WORKSPACE_MESSAGE = (
    "You are not in a Workbench workspace yet. Workbench is invite-only: ask your workspace "
    "admin for an invite, then run `nexus-workbench login` again."
)
DENIED_MESSAGE = (
    "This sign-in was denied in the browser. Run `nexus-workbench login` to start again."
)
EXPIRED_MESSAGE = (
    "This sign-in ran out of time or was cancelled. Run `nexus-workbench login` to start again."
)


def expired_while_blocked_message(reason: str, approval_url: str) -> str:
    """The expiry sentence when the page was showing a "cannot approve" reason at the end."""
    if reason == "no_agent_access":
        home = workbench_home(approval_url)
        where = f"Workbench ({home})" if home else "Workbench"
        return (
            "This sign-in ran out of time while the page said none of your workspaces has "
            f"agent access yet. Request it in {where}: account menu, Connect an agent, "
            "Request agent access. Run `nexus-workbench login` again once it's on."
        )
    return (
        "This sign-in ran out of time while the page said you are not in a Workbench "
        "workspace yet. Ask your workspace admin for an invite, then run "
        "`nexus-workbench login` again."
    )


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
        return LoginFailed(EXPIRED_MESSAGE)
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
    if isinstance(exc, WriteAccessRequired):
        return LoginFailed(
            "Workbench refused to approve write scopes: this workspace does not take agent "
            "writes yet. Ask Insai to switch them on, or sign in without write scopes, for "
            "example `nexus-workbench login --scope detail`."
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
    sleep: Callable[[float], Any] = lambda seconds: time.sleep(seconds)  # noqa: E731

    def say(self, text: str = "") -> None:
        print(text, file=self.out)


@dataclass
class LoginResult:
    where: str  # "file" or "keyring"
    whoami: dict[str, Any] | None
    whoami_error: WorkbenchError | None
    #: The token's scopes as the server reported them, or None when it did not say.
    scopes: tuple[str, ...] | None = None
    #: The scopes this sign-in asked for (browser flow only).
    requested: tuple[str, ...] | None = None


def read_token(io: LoginIO) -> str:
    """Read a token from a hidden prompt, or from stdin when stdin is not a terminal."""
    try:
        interactive = io.stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    try:
        if interactive:
            raw = io.hidden_prompt("Paste your Workbench token (it will not be shown): ")
        else:
            raw = io.stdin.read()
    except (EOFError, KeyboardInterrupt):
        io.say()
        raise LoginCancelled(CANCELLED_MESSAGE) from None
    token = (raw or "").strip()
    if not token:
        raise LoginCancelled(
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
    scopes: Any = None,
    requested: tuple[str, ...] | None = None,
) -> LoginResult:
    """Check the token with whoami, then save it. A token the server refuses is not saved.

    ``scopes`` are the ones ``exchange`` returned; otherwise ``whoami``'s are saved.
    """
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
    granted = _scope_list(scopes)
    if granted is None and whoami:
        granted = _scope_list(whoami.get("scopes"))
    where = auth.save_token(
        client.api_base,
        token,
        workspace_id=workspace_id,
        token_id_value=token_id_value,
        scopes=granted,
        use_keyring=use_keyring,
    )
    return LoginResult(
        where=where,
        whoami=whoami,
        whoami_error=whoami_error,
        scopes=granted,
        requested=requested,
    )


def _scope_list(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        return None
    return sc.normalise(value)


def login_with_token(client: WorkbenchClient, io: LoginIO, *, use_keyring: bool) -> LoginResult:
    token = read_token(io)
    try:
        auth.validate_token(token)
    except auth.InvalidTokenError as exc:
        raise LoginFailed(str(exc)) from None
    return _finish(client, token, use_keyring=use_keyring)


def login_with_browser(
    client: WorkbenchClient,
    io: LoginIO,
    *,
    use_keyring: bool,
    scopes: tuple[str, ...] = (sc.READ,),
) -> LoginResult:
    """Browser approval flow. Raises LoginNotAvailable when the server does not offer it."""
    requested = sc.normalise(scopes)
    try:
        session = client.cli_login_start(device_name=device_name(), scopes=requested)
    except LoginNotAvailable:
        raise
    except WorkbenchError as exc:
        raise sign_in_failure(exc) from None
    url = session["approvalUrl"]
    io.say(f"Asking for scopes: {sc.describe(requested)}")
    if sc.WRITE & set(requested):
        io.say(WRITE_SCOPES_NOTE)
    io.say("To sign in, approve this computer in Workbench:")
    io.say(f"  {url}")
    # The URL is printed above, so a browser that does not open is not an error.
    with contextlib.suppress(Exception):
        io.open_browser(url)
    io.say(APPROVE_PROMPT)
    _wait_for_approval(client, io, session, url)

    local_retries = 0
    pending_replies = 0
    while True:
        try:
            raw = io.prompt("Code: ")
        except (EOFError, KeyboardInterrupt):
            io.say()
            raise LoginCancelled(CANCELLED_MESSAGE) from None
        code = normalize_code(raw)
        if not code:
            raise LoginCancelled(CANCELLED_MESSAGE)
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
            if exc.attempts_left is not None and exc.attempts_left <= 0:
                # The CLI's own sentence ends the sign-in; the server's would repeat it.
                raise LoginFailed(
                    "That code is not right. No attempts are left. "
                    "Run `nexus-workbench login` to start again."
                ) from None
            print(str(exc), file=io.err)
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
        scopes=data.get("scopes"),
        requested=requested,
    )


def _blocked_message(reason: str | None, url: str) -> str | None:
    """What a known "cannot approve" reason means, or None for no reason or an unknown one."""
    if reason == "no_agent_access":
        return no_agent_access_message(url)
    if reason == "no_workspace":
        return NO_WORKSPACE_MESSAGE
    return None


def _wait_for_approval(
    client: WorkbenchClient, io: LoginIO, session: dict[str, Any], url: str
) -> None:
    """Poll ``status`` until the person approves, then return so the code can be typed.

    A "cannot approve" reason (no workspace, none with agent access) is explained, again
    only when it changes, and the wait goes on. The reason is advisory: it belongs to
    whoever opened the page last, so it clears when the person switches accounts or someone
    who can approve opens the link, and someone else opening the page must not end this
    sign-in. Only approval, denial or the end of the session (10 minutes) stop the wait. A
    server without ``status`` returns at once (the code prompt follows, as before), and so
    do a few network failures in a row.
    """
    said_waiting = False
    shown: str | None = None  # the last reason explained
    showing: str | None = None  # the known reason the page shows now
    network_failures = 0

    def expired() -> LoginFailed:
        if showing is not None:
            return LoginFailed(expired_while_blocked_message(showing, url))
        return LoginFailed(EXPIRED_MESSAGE)

    try:
        for _ in range(MAX_POLLS):
            try:
                state = client.cli_login_status(
                    session_id=session["sessionId"], poll_secret=session["pollSecret"]
                )
            except (InvalidArgument, LoginNotAvailable, UnexpectedResponse):
                return  # an older server: ask for the code straight away
            except NetworkError:
                network_failures += 1
                if network_failures >= MAX_STATUS_NETWORK_FAILURES:
                    print(STATUS_UNREACHABLE_NOTE, file=io.err)
                    return
                io.sleep(min(POLL_INTERVAL * 2**network_failures, MAX_NETWORK_BACKOFF))
                continue
            except LoginExpired:
                raise expired() from None
            except WorkbenchError as exc:
                raise sign_in_failure(exc) from None
            network_failures = 0
            status, reason = state["status"], state["reason"]
            if status == "approved":
                if said_waiting:
                    io.say("Approved.")
                return
            if status == "denied":
                raise LoginFailed(DENIED_MESSAGE)
            if status in ("expired", "used"):
                raise expired()
            message = _blocked_message(reason, url) if status == "pending" else None
            showing = reason if message is not None else None
            if message is not None and reason != shown:
                shown = reason
                io.say(message)
                io.say(STILL_WAITING_NOTE)
                said_waiting = True
            if not said_waiting:
                io.say(WAITING_MESSAGE)
                said_waiting = True
            io.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        io.say()
        raise LoginCancelled(CANCELLED_MESSAGE) from None
    raise expired()


def run_login(
    client: WorkbenchClient,
    io: LoginIO,
    *,
    token_prompt: bool = False,
    use_keyring: bool = False,
    scopes: tuple[str, ...] = (sc.READ,),
) -> LoginResult:
    """``login``: the browser flow first, else (or with --token) a pasted token.

    ``scopes`` are asked for in the browser flow. A pasted token has the scopes it was made
    with on the page.
    """
    if not token_prompt:
        try:
            return login_with_browser(client, io, use_keyring=use_keyring, scopes=scopes)
        except LoginNotAvailable:
            io.say("Signing in from the terminal is not available yet, so use a token instead.")
            io.say(CONNECT_PAGE_HINT)
    if sc.normalise(scopes) != (sc.READ,):
        io.say(
            "A pasted token keeps the scopes it was made with on the page; "
            "--scope does not change them."
        )
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
    if result.scopes is not None:
        io.say(f"Scopes: {sc.describe(result.scopes)}")
        left_out = [s for s in result.requested or () if s not in result.scopes]
        if left_out:
            io.say(
                "Workbench approved fewer scopes than asked; left out: " + ", ".join(left_out) + "."
            )
    io.say(f"Saved in {where}.")
