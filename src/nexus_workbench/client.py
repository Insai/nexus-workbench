"""HTTPS client for the Workbench agent API (contract sections 1, 3, 5 and 7).

The token is sent only in the Authorization header and is never logged or put
into an exception message.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import __version__, auth
from . import scopes as sc

logger = logging.getLogger(__name__)

PRODUCTION_API_BASE = "https://europe-west4-insai-nexus.cloudfunctions.net"
REQUEST_ACCESS_HINT = (
    "Request it in Workbench: account menu, Connect an agent, Request agent access."
)
AGENT_API_PATH = "/workbench_agent_api"
CLI_LOGIN_PATH = "/workbench_cli_login"
#: What ``workbench_cli_login`` ``status`` may answer (contract 5.1).
LOGIN_STATUSES = frozenset({"pending", "approved", "denied", "expired", "used"})

ENV_API_URL = "NEXUS_API_URL"
ENV_ALLOW_INSECURE_LOCAL = "NEXUS_ALLOW_INSECURE_LOCAL"

MAX_BODY_BYTES = 2048
#: ``propose_marks`` alone may send a larger body (contract 7.7).
MAX_PROPOSE_BODY_BYTES = 262_144
MAX_RETRY_AFTER_SECONDS = 10.0
TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0)
USER_AGENT = f"nexus-workbench/{__version__}"

CONNECT_PAGE_HINT = (
    'In Workbench, open the account menu and choose "Connect an agent" to make a token.'
)
LOGIN_HINT = "Run `nexus-workbench login` in a terminal, then try again."


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WorkbenchError(Exception):
    """An error with a short message for a person and the next thing to do.

    ``str(error)`` is the full text to show. It never contains a token.
    """

    code: str = "error"
    default_message: str = "Something went wrong talking to Workbench."
    default_hint: str = ""
    #: The id the server gave this request, when it sent one (see :func:`request_id_of`).
    request_id: str | None = None
    #: Seconds the server asked the client to wait (``Retry-After``), when it said.
    retry_after: float | None = None

    def __init__(
        self,
        message: str | None = None,
        *,
        hint: str | None = None,
        status: int | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.hint = self.default_hint if hint is None else hint
        self.status = status
        super().__init__(self.text)

    @property
    def text(self) -> str:
        return f"{self.message} {self.hint}".strip()

    def __str__(self) -> str:
        return self.text


class ConfigError(WorkbenchError):
    code = "config"
    default_message = "The Workbench address is not usable."


class NotSignedIn(WorkbenchError):
    code = "not_signed_in"
    default_message = "Not signed in."
    default_hint = LOGIN_HINT


class InvalidArgument(WorkbenchError):
    code = "invalid_argument"
    default_message = "Workbench did not accept the request."
    default_hint = "Check the arguments and try again."


class Unauthenticated(WorkbenchError):
    code = "unauthenticated"
    default_message = "Workbench did not accept your token. It may have been revoked or mistyped."
    default_hint = "Run `nexus-workbench login`."


class TokenExpired(WorkbenchError):
    code = "token_expired"
    default_message = "Your Workbench token has expired."
    default_hint = "Run `nexus-workbench login` to sign in again."


class Forbidden(WorkbenchError):
    code = "forbidden"
    default_message = (
        "This token can no longer read the workspace: you may have left it, "
        "it may be closed, or the token is older than your current membership."
    )
    default_hint = "Ask the workspace owner, then run `nexus-workbench login` to make a new token."


class EarlyAccessRequired(WorkbenchError):
    code = "early_access_required"
    default_message = "Agent access is not on for this workspace yet."
    default_hint = REQUEST_ACCESS_HINT


class NotFound(WorkbenchError):
    code = "not_found"
    default_message = (
        "That recording or project does not exist in this workspace, or you cannot see it."
    )
    default_hint = "Check the id. Use list_projects or list_recordings to find valid ids."


class RateLimited(WorkbenchError):
    code = "rate_limited"
    default_message = "Too many requests to Workbench."
    default_hint = "Wait a minute and try again."

    def __init__(self, *args: Any, retry_after: float | None = None, **kwargs: Any) -> None:
        self.retry_after = retry_after
        super().__init__(*args, **kwargs)


class TokenLimit(WorkbenchError):
    code = "token_limit"
    default_message = "You have reached the limit of 10 tokens for this workspace."
    default_hint = (
        'Revoke one on the "Connect an agent" page in Workbench (account menu), '
        "then run `nexus-workbench login` again."
    )


class InternalError(WorkbenchError):
    code = "internal"
    default_message = "Workbench had a problem answering."
    default_hint = (
        "Try again in a few minutes. If it keeps happening, run `nexus-workbench doctor`."
    )


class LoginExpired(WorkbenchError):
    code = "login_expired"
    default_message = "This sign-in has expired or was already used."
    default_hint = "Run `nexus-workbench login` to start again."


class CodeWrong(WorkbenchError):
    code = "code_wrong"
    default_message = "That code is not right."

    def __init__(self, *args: Any, attempts_left: int | None = None, **kwargs: Any) -> None:
        self.attempts_left = attempts_left
        if "hint" not in kwargs:
            if attempts_left is None:
                kwargs["hint"] = "Check the code on the Workbench page and type it again."
            elif attempts_left <= 0:
                kwargs["hint"] = "No attempts are left. Run `nexus-workbench login` to start again."
            else:
                noun = "attempt" if attempts_left == 1 else "attempts"
                kwargs["hint"] = f"{attempts_left} {noun} left."
        super().__init__(*args, **kwargs)


class LoginNotAvailable(WorkbenchError):
    code = "login_not_available"
    default_message = "Signing in from the terminal is not available yet."
    default_hint = CONNECT_PAGE_HINT


class NetworkError(WorkbenchError):
    code = "network"
    default_message = "Could not reach Workbench."
    default_hint = "Check your internet connection and try again."


class UnexpectedResponse(WorkbenchError):
    code = "unexpected_response"
    default_message = "Workbench sent an answer this version does not understand."
    default_hint = "Update with `uv tool upgrade nexus-workbench`, then try again."


class ScopeRequired(WorkbenchError):
    """The token lacks the scope an action needs (contract 7.1). ``scope`` names it."""

    code = "scope_required"

    def __init__(
        self,
        scope: str | None = None,
        *args: Any,
        have: tuple[str, ...] | None = None,
        action: str | None = None,
        **kwargs: Any,
    ) -> None:
        self.scope = scope if scope in sc.ALL else None
        keep = [s for s in sc.normalise(have) if s not in (sc.READ, self.scope)]
        #: The exact ``login`` command that makes a token with the missing scope, keeping the
        #: scopes this token has (``None`` when the scope is not known).
        self.login = sc.login_command([self.scope, *keep]) if self.scope else None
        if "message" not in kwargs and not args:
            kwargs["message"] = (
                sc.cannot(self.scope, action)
                if self.scope
                else "This token lacks the scope this needs."
            )
        if "hint" not in kwargs:
            if self.scope:
                extra = " ".join(f"`--scope {s}`" for s in keep)
                kwargs["hint"] = f"Sign in again with `{sc.login_command([self.scope])}`" + (
                    f" (add {extra} to keep what this token has)." if keep else "."
                )
            else:
                kwargs["hint"] = "Sign in again with `nexus-workbench login --scope <scope>`."
        super().__init__(*args, **kwargs)

    @property
    def agent_text(self) -> str:
        """The text for an agent: the same, plus the restart the new scopes need."""
        hint = self.hint.rstrip(".")
        return f"{self.message} {hint}, then restart the agent session.".strip()


class WriteAccessRequired(WorkbenchError):
    code = "write_access_required"
    default_message = "This workspace does not take agent writes yet."
    default_hint = "Insai staff switch agent writes on per workspace. Reads still work."


class AlreadyAnalysed(WorkbenchError):
    code = "already_analysed"
    default_message = "This recording already has machine output, so no analysis was started."
    default_hint = "An agent cannot ask for a re-analysis."


class MarkLimit(WorkbenchError):
    code = "mark_limit"
    default_message = (
        "These marks would take this token past 5,000 marks on this recording, "
        "withdrawn ones included."
    )
    default_hint = "Nothing was written. Propose fewer marks."


class NotAvailable(WorkbenchError):
    """HTTP 501: the server checked the call but cannot do it yet (contract 7.7)."""

    code = "not_available"
    default_message = "Workbench cannot do this through the agent API yet."
    default_hint = "Do it on the Workbench page instead."


class TooLarge(WorkbenchError):
    code = "too_large"
    default_message = "This recording's events are too large to read through the agent API."
    default_hint = "Open the recording in Workbench instead."


class NotOnServer(InvalidArgument):
    """A version 1.1 action sent to a version 1 server (contract section 7)."""

    def __init__(self, action: str, **kwargs: Any) -> None:
        self.action = action
        super().__init__(
            f"This Workbench server does not offer {safe_text(action, 40)} yet.",
            hint="It is part of agent API version 1.1, which this server does not run yet.",
            **kwargs,
        )


class ServerRefusal(WorkbenchError):
    """A refusal whose code the server chose and the client passes through unchanged
    (contract 7.12): ``code`` is the server's, ``message`` its sentence, and ``extra`` the
    body's other top-level keys (``current`` on ``changed``, ``candidates`` on
    ``ambiguous_label``). The codes are in :data:`REFUSAL_CODES`."""

    default_message = "Workbench refused the request."

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        hint: str | None = None,
        status: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.extra = dict(extra or {})
        super().__init__(message, hint=hint, status=status)


#: Server codes passed through as :class:`ServerRefusal` (contract 7.12): the channel-site
#: refusals, ``unavailable`` and ``ambiguous_label``. ``scope_required``,
#: ``write_access_required`` and ``not_available`` have their own classes above.
REFUSAL_CODES = frozenset(
    {
        "unavailable",
        "held",
        "insai_checked",
        "checked_needs_insai",
        "bad_port",
        "bad_site",
        "duplicate_site",
        "changed",
        "fixed_device",
        "combined",
        "device_crossed",
        "no_source_key",
        "header_unparsed",
        "incomplete",
        "ambiguous_label",
    }
)


ERRORS_BY_CODE: dict[str, type[WorkbenchError]] = {
    cls.code: cls
    for cls in (
        InvalidArgument,
        Unauthenticated,
        TokenExpired,
        Forbidden,
        EarlyAccessRequired,
        NotFound,
        RateLimited,
        TokenLimit,
        InternalError,
        LoginExpired,
        CodeWrong,
        ScopeRequired,
        WriteAccessRequired,
        AlreadyAnalysed,
        MarkLimit,
        NotAvailable,
        TooLarge,
    )
}

#: The sentence and advice for ``not_available`` on the actions that send it.
NOT_AVAILABLE_BY_ACTION: dict[str, tuple[str, str]] = {
    "request_analysis": (
        "Starting an analysis from an agent is not available yet.",
        "Use Analyse on the recording page.",
    ),
    "get_channels": (
        "Channel sites are not switched on for this Workbench server yet.",
        "Read them on the Workbench page instead.",
    ),
    "set_channels": (
        "Channel sites are not switched on for this Workbench server yet.",
        "Set them on the Workbench page instead.",
    ),
}

#: A clearer sentence for ``forbidden`` on the actions where it has a second meaning.
FORBIDDEN_BY_ACTION: dict[str, str] = {
    "propose_marks": (
        "Workbench refused to write marks on this recording: you are held blind on it, or this "
        "token can no longer read the workspace."
    ),
    "withdraw_marks": (
        "Workbench refused to withdraw marks on this recording: you are held blind on it, or "
        "this token can no longer read the workspace."
    ),
    "request_analysis": (
        "Workbench refused to start the analysis: only the person who uploaded the recording "
        "can ask for one."
    ),
}


# ---------------------------------------------------------------------------
# API base
# ---------------------------------------------------------------------------


def _insecure_local_allowed(url: str) -> bool:
    parts = urlsplit(url)
    return (
        parts.scheme == "http"
        and parts.hostname in {"127.0.0.1", "localhost"}
        and os.environ.get(ENV_ALLOW_INSECURE_LOCAL) == "1"
    )


def check_url(url: str, *, what: str = "address") -> str:
    """Return ``url`` if it is https (or an allowed local test address), else raise ConfigError."""
    parts = urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if _insecure_local_allowed(url):
        return url
    raise ConfigError(
        f"The Workbench {what} must start with https://.",
        hint=f"Fix or unset {ENV_API_URL}." if what == "address" else "",
    )


def resolve_api_base() -> str:
    """The API base: ``NEXUS_API_URL`` if set, else the production constant."""
    raw = os.environ.get(ENV_API_URL, "").strip()
    if not raw:
        return PRODUCTION_API_BASE
    return check_url(raw.rstrip("/"))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


def _one_line(text: Any, limit: int = 200) -> str | None:
    if not isinstance(text, str):
        return None
    cleaned = " ".join(safe_text(text).split())
    if not cleaned:
        return None
    return cleaned[:limit]


#: Longest server text shown by :func:`safe_text` unless the caller asks for another limit.
SAFE_TEXT_LIMIT = 200


def safe_text(value: Any, limit: int | None = SAFE_TEXT_LIMIT) -> str:
    """Show server text as plain text on one line: control and format characters become spaces.

    At most ``limit`` characters are kept (``None`` keeps them all, for ids and cursors that
    must round-trip).
    """
    if value is None:
        return ""
    text = str(value)
    if limit is not None:
        text = text[:limit]
    return "".join(ch if ch.isprintable() else " " for ch in text)


def clean_name(value: Any) -> str | None:
    """A name typed by a person, as plain one-line text, or None when unknown.

    The server may send ``""`` for an unknown name; that is treated like an absent one.
    """
    if not isinstance(value, str):
        return None
    return safe_text(value).strip() or None


def display_name(value: Any) -> str:
    """A name for printing: plain one-line text, or ``unknown``."""
    return clean_name(value) or "unknown"


#: Response headers that may carry an id for the request, in the order they are read.
REQUEST_ID_HEADERS = ("X-Request-Id", "X-Cloud-Trace-Context")
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def request_id_of(response: httpx.Response) -> str | None:
    """The id the server gave this request, or None when it sent none.

    Read from ``X-Request-Id``, then ``X-Cloud-Trace-Context`` (the trace id before the
    ``/``), then ``requestId`` in the JSON body or its ``error`` object. Only a short id of
    plain characters is kept, so it is safe to print.
    """
    candidates: list[Any] = []
    for name in REQUEST_ID_HEADERS:
        value = response.headers.get(name)
        if value:
            candidates.append(value.split("/")[0].split(";")[0].strip())
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        candidates.append(body.get("requestId"))
        if isinstance(body.get("error"), dict):
            candidates.append(body["error"].get("requestId"))
    for value in candidates:
        if isinstance(value, str) and _REQUEST_ID.match(value):
            return value
    return None


def error_from_response(
    response: httpx.Response,
    *,
    login_endpoint: bool = False,
    action: str | None = None,
    have: tuple[str, ...] | None = None,
) -> WorkbenchError:
    """Map a non-200 response to a typed error (contract 3.1, 5.1 and 7.3).

    ``action`` is the agent API action that was sent and ``have`` the token's scopes when
    known, so the messages can be specific. The error carries the request id when the
    server sent one.
    """
    error = _error_from_response(response, login_endpoint=login_endpoint, action=action, have=have)
    error.request_id = request_id_of(response)
    if error.retry_after is None:
        error.retry_after = _parse_retry_after(response.headers.get("Retry-After"))
    return error


def _error_from_response(
    response: httpx.Response,
    *,
    login_endpoint: bool,
    action: str | None,
    have: tuple[str, ...] | None,
) -> WorkbenchError:
    status = response.status_code
    body: Any = None
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None

    if status == 404 and login_endpoint:
        return LoginNotAvailable(status=status)
    if status == 404 and not isinstance(code, str):
        return EarlyAccessRequired(status=status)
    if 300 <= status < 400:
        return UnexpectedResponse(
            "Workbench answered with a redirect, which this client does not follow.",
            hint=f"Check {ENV_API_URL} if you set it.",
            status=status,
        )

    if isinstance(code, str) and code in REFUSAL_CODES:
        extra = {k: v for k, v in body.items() if k != "error"} if isinstance(body, dict) else {}
        message = _one_line(error.get("message")) if isinstance(error, dict) else None
        return ServerRefusal(code, message, status=status, extra=extra)

    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
    cls = ERRORS_BY_CODE.get(code) if isinstance(code, str) else None
    if cls is None:
        if status == 401:
            cls = Unauthenticated
        elif status == 403:
            cls = Forbidden
        elif status == 429:
            cls = RateLimited
        elif status >= 500:
            cls = InternalError
        elif status in (400, 405):
            cls = InvalidArgument
        else:
            return UnexpectedResponse(status=status)

    if cls is InvalidArgument:
        detail = _one_line(error.get("message")) if isinstance(error, dict) else None
        if action in sc.V11_ACTIONS and detail and "unknown action" in detail.lower():
            return NotOnServer(action, status=status)
        return InvalidArgument(
            f"Workbench did not accept the request: {detail}" if detail else None, status=status
        )
    if cls is RateLimited:
        hint = (
            f"Wait {int(retry_after)} seconds and try again." if retry_after is not None else None
        )
        return RateLimited(hint=hint, status=status, retry_after=retry_after)
    if cls is CodeWrong:
        attempts = body.get("attemptsLeft") if isinstance(body, dict) else None
        if not isinstance(attempts, int) or isinstance(attempts, bool):
            attempts = None
        return CodeWrong(status=status, attempts_left=attempts)
    if cls is ScopeRequired:
        scope = body.get("scope") if isinstance(body, dict) else None
        if not isinstance(scope, str) and action is not None:
            scope = sc.needed(action)
        return ScopeRequired(
            scope if isinstance(scope, str) else None, have=have, action=action, status=status
        )
    if cls is Forbidden and action in FORBIDDEN_BY_ACTION:
        return Forbidden(FORBIDDEN_BY_ACTION[action], status=status)
    if cls is NotAvailable and action in NOT_AVAILABLE_BY_ACTION:
        message, hint = NOT_AVAILABLE_BY_ACTION[action]
        return NotAvailable(message, hint=hint, status=status)
    return cls(status=status)


class WorkbenchClient:
    """Calls the agent API. Pass ``token`` explicitly or let it be looked up."""

    def __init__(
        self,
        *,
        api_base: str | None = None,
        token: str | None = None,
        scopes: tuple[str, ...] | list[str] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_base = check_url(api_base.rstrip("/")) if api_base else resolve_api_base()
        self._token = token
        self._scopes: tuple[str, ...] | None = sc.normalise(scopes) if scopes is not None else None
        self._sleep = sleep
        self._transport = transport
        self._http = httpx.Client(
            timeout=TIMEOUT,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> WorkbenchClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def with_token(self, token: str) -> WorkbenchClient:
        """A client for the same server and transport that uses ``token``."""
        return WorkbenchClient(
            api_base=self.api_base, token=token, transport=self._transport, sleep=self._sleep
        )

    @property
    def scopes(self) -> tuple[str, ...] | None:
        """The token's scopes when this client knows them (saved with it), else None."""
        return self._scopes

    def __repr__(self) -> str:  # never show the token
        return f"WorkbenchClient(api_base={self.api_base!r})"

    # -- token ---------------------------------------------------------------

    def _resolve_token(self) -> str:
        if self._token:
            return self._token
        try:
            stored = auth.load_token(self.api_base)
        except (auth.CredentialsError, auth.InvalidTokenError) as exc:
            raise NotSignedIn(str(exc), hint="") from None
        if not stored.token:
            raise NotSignedIn()
        self._token = stored.token
        if self._scopes is None and stored.scopes is not None:
            self._scopes = stored.scopes
        return stored.token

    # -- transport -----------------------------------------------------------

    def _post(
        self,
        path: str,
        body: dict[str, Any],
        *,
        token: str | None,
        login_endpoint: bool = False,
    ) -> dict[str, Any]:
        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
        action = body.get("action")
        limit = MAX_PROPOSE_BODY_BYTES if action == "propose_marks" else MAX_BODY_BYTES
        if len(payload) > limit:
            raise InvalidArgument(
                "The request is too large for Workbench.",
                hint="Use shorter arguments.",
            )
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = self.api_base + path

        response: httpx.Response | None = None
        for attempt in (1, 2):
            try:
                response = self._http.post(url, content=payload, headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                logger.debug(
                    "connection to Workbench failed (attempt %d): %s", attempt, type(exc).__name__
                )
                if attempt == 1:
                    continue
                raise NetworkError() from None
            except httpx.TimeoutException:
                raise NetworkError(
                    "Workbench took too long to answer.", hint="Try again in a minute."
                ) from None
            except httpx.HTTPError as exc:
                logger.debug("HTTP error talking to Workbench: %s", type(exc).__name__)
                raise NetworkError() from None

            logger.debug("action=%s status=%d attempt=%d", action, response.status_code, attempt)
            if response.status_code == 429 and attempt == 1:
                wait = _parse_retry_after(response.headers.get("Retry-After"))
                if wait is not None and wait <= MAX_RETRY_AFTER_SECONDS:
                    self._sleep(wait)
                    continue
            break

        assert response is not None
        if response.status_code != 200:
            raise error_from_response(
                response,
                login_endpoint=login_endpoint,
                action=None if login_endpoint else action,
                have=self._scopes,
            )
        try:
            data = response.json()
        except ValueError:
            raise UnexpectedResponse() from None
        if not isinstance(data, dict):
            raise UnexpectedResponse()
        return data

    # -- agent API -----------------------------------------------------------

    def call(self, action: str, **args: Any) -> dict[str, Any]:
        """Call one agent API action. Arguments use the contract's names (e.g. ``projectId``).

        When the token's scopes are known and lack the one ``action`` needs, it raises
        :class:`ScopeRequired` before any call (contract 7.10). A ``None``, empty or "null"
        ``cursor`` is left out, so it asks for the first page.
        """
        token = self._resolve_token()
        self.require_scope(action)
        if isinstance(args.get("cursor"), str) and args["cursor"].strip() in ("", "null"):
            args["cursor"] = None  # an agent's "no cursor yet": the first page, not an error
        body = {"action": action, **{k: v for k, v in args.items() if v is not None}}
        return self._post(AGENT_API_PATH, body, token=token)

    def require_scope(self, action: str) -> None:
        """Raise :class:`ScopeRequired` when the known scopes do not cover ``action``."""
        need = sc.needed(action)
        if self._scopes is not None and need not in self._scopes:
            raise ScopeRequired(need, have=self._scopes, action=action)

    # -- version 1.1 actions (contract section 7) ------------------------------

    def list_members(self) -> dict[str, Any]:
        return self.call("list_members")

    def list_tasks(self, *, mine: bool = True) -> dict[str, Any]:
        return self.call("list_tasks", mine=mine)

    def list_nights(self, *, project_id: str | None = None) -> dict[str, Any]:
        return self.call("list_nights", projectId=project_id)

    def list_layers(self, *, project_id: str | None = None) -> dict[str, Any]:
        return self.call("list_layers", projectId=project_id)

    def get_events(
        self,
        recording_id: str,
        *,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self.call("get_events", id=recording_id, kind=kind, cursor=cursor, limit=limit)

    def get_stages(self, recording_id: str, *, source: str = "machine") -> dict[str, Any]:
        return self.call("get_stages", id=recording_id, source=source)

    def get_quality(self, recording_id: str) -> dict[str, Any]:
        return self.call("get_quality", id=recording_id)

    def list_marks(
        self,
        recording_id: str,
        *,
        origin: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return self.call("list_marks", id=recording_id, origin=origin, cursor=cursor, limit=limit)

    def propose_marks(
        self, recording_id: str, marks: list[dict[str, Any]], *, op_id: str | None = None
    ) -> dict[str, Any]:
        return self.call("propose_marks", id=recording_id, marks=marks, opId=op_id)

    def withdraw_marks(
        self, recording_id: str, *, mark_ids: list[str] | None = None
    ) -> dict[str, Any]:
        return self.call("withdraw_marks", id=recording_id, markIds=mark_ids)

    def create_review_task(
        self,
        recording_ids: list[str],
        *,
        member_id: str,
        mode: str,
        confirm_machine_seen: bool = False,
    ) -> dict[str, Any]:
        return self.call(
            "create_review_task",
            ids=recording_ids,
            memberId=member_id,
            mode=mode,
            confirmMachineSeen=confirm_machine_seen or None,
        )

    def request_analysis(self, recording_id: str) -> dict[str, Any]:
        return self.call("request_analysis", id=recording_id)

    # -- terminal sign-in (contract section 5) -------------------------------

    def cli_login_start(
        self,
        *,
        device_name: str,
        client_version: str = __version__,
        scopes: list[str] | tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "action": "start",
            "deviceName": device_name[:64],
            "clientVersion": client_version[:32],
        }
        if scopes is not None and tuple(scopes) != (sc.READ,):
            # A version 1 server ignores keys it does not know; read alone is the default.
            body["scopes"] = list(sc.normalise(scopes))
        data = self._post(CLI_LOGIN_PATH, body, token=None, login_endpoint=True)
        for key in ("sessionId", "pollSecret", "approvalUrl"):
            if not isinstance(data.get(key), str):
                raise UnexpectedResponse()
        check_url(data["approvalUrl"], what="sign-in page address")
        return data

    def cli_login_exchange(self, *, session_id: str, poll_secret: str, code: str) -> dict[str, Any]:
        body = {
            "action": "exchange",
            "sessionId": session_id,
            "pollSecret": poll_secret,
            "code": code,
        }
        data = self._post(CLI_LOGIN_PATH, body, token=None, login_endpoint=True)
        if not isinstance(data.get("token"), str):
            raise UnexpectedResponse()
        return data

    def cli_login_status(self, *, session_id: str, poll_secret: str) -> dict[str, Any]:
        """Where a sign-in stands, for the terminal that started it (contract 5.1).

        A server without ``status`` answers ``invalid_argument``; the caller then asks for the
        code straight away, as before.
        """
        body = {"action": "status", "sessionId": session_id, "pollSecret": poll_secret}
        data = self._post(CLI_LOGIN_PATH, body, token=None, login_endpoint=True)
        if data.get("status") not in LOGIN_STATUSES:
            raise UnexpectedResponse()
        reason = data.get("reason")
        return {"status": data["status"], "reason": reason if isinstance(reason, str) else None}
