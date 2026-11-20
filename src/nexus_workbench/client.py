"""HTTPS client for the Workbench agent API (contract sections 1, 3 and 5).

The token is sent only in the Authorization header and is never logged or put
into an exception message.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from . import __version__, auth

logger = logging.getLogger(__name__)

PRODUCTION_API_BASE = "https://europe-west4-insai-nexus.cloudfunctions.net"
EARLY_ACCESS_URL = "https://insai.tech/workbench"
AGENT_API_PATH = "/workbench_agent_api"
CLI_LOGIN_PATH = "/workbench_cli_login"

ENV_API_URL = "NEXUS_API_URL"
ENV_ALLOW_INSECURE_LOCAL = "NEXUS_ALLOW_INSECURE_LOCAL"

MAX_BODY_BYTES = 2048
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
    default_message = (
        "Agent access is in early access and is not switched on for this workspace yet."
    )
    default_hint = f"Ask for access: {EARLY_ACCESS_URL}"


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
        InternalError,
        LoginExpired,
        CodeWrong,
    )
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
    cleaned = " ".join(text.split())
    if not cleaned:
        return None
    return cleaned[:limit]


def error_from_response(
    response: httpx.Response, *, login_endpoint: bool = False
) -> WorkbenchError:
    """Map a non-200 response to a typed error (contract 3.1 and 5.1)."""
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
        return EarlyAccessRequired(
            "Agent access is not available on this Workbench server yet (early access).",
            status=status,
        )
    if 300 <= status < 400:
        return UnexpectedResponse(
            "Workbench answered with a redirect, which this client does not follow.",
            hint=f"Check {ENV_API_URL} if you set it.",
            status=status,
        )

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
    return cls(status=status)


class WorkbenchClient:
    """Calls the agent API. Pass ``token`` explicitly or let it be looked up."""

    def __init__(
        self,
        *,
        api_base: str | None = None,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.api_base = check_url(api_base.rstrip("/")) if api_base else resolve_api_base()
        self._token = token
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
        if len(payload) > MAX_BODY_BYTES:
            raise InvalidArgument(
                "The request is too large for Workbench.",
                hint="Use shorter arguments.",
            )
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = self.api_base + path
        action = body.get("action")

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
            raise error_from_response(response, login_endpoint=login_endpoint)
        try:
            data = response.json()
        except ValueError:
            raise UnexpectedResponse() from None
        if not isinstance(data, dict):
            raise UnexpectedResponse()
        return data

    # -- agent API -----------------------------------------------------------

    def call(self, action: str, **args: Any) -> dict[str, Any]:
        """Call one agent API action. Arguments use the contract's names (e.g. ``projectId``)."""
        token = self._resolve_token()
        body = {"action": action, **{k: v for k, v in args.items() if v is not None}}
        return self._post(AGENT_API_PATH, body, token=token)

    # -- terminal sign-in (contract section 5) -------------------------------

    def cli_login_start(
        self, *, device_name: str, client_version: str = __version__
    ) -> dict[str, Any]:
        body = {
            "action": "start",
            "deviceName": device_name[:64],
            "clientVersion": client_version[:32],
        }
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
