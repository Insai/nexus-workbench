"""An in-memory fake of the Workbench agent API (contract sections 3 and 5).

Used through ``httpx.MockTransport``; no test touches the network.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx

from nexus_workbench import auth

API_BASE = "https://api.test.invalid"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"


def make_token() -> str:
    """A token built with the contract's algorithm (section 2)."""
    body = "".join(secrets.choice(auth.BASE62) for _ in range(auth.BODY_LENGTH))
    return auth.TOKEN_PREFIX + body + auth.check_digits(body)


def _error(status: int, code: str, message: str, **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}, **extra})


@dataclass
class TokenState:
    workspace_ok: bool = True  # False -> forbidden
    expired: bool = False
    early_access: bool = True  # False -> early_access_required


@dataclass
class LoginSession:
    session_id: str
    poll_secret: str
    code: str | None = None
    attempts_left: int = 5
    used: bool = False


@dataclass
class FakeAPI:
    tokens: dict[str, TokenState] = field(default_factory=dict)
    projects: list[dict[str, Any]] = field(default_factory=list)
    recordings: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, dict[str, Any]] = field(default_factory=dict)
    sessions: dict[str, LoginSession] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    login_available: bool = True
    agent_api_available: bool = True
    rate_limit_next: list[str] = field(default_factory=list)  # Retry-After values to send first
    fail_next: list[str] = field(default_factory=list)  # "connect" -> raise ConnectError

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def add_token(self, **state: Any) -> str:
        token = make_token()
        self.tokens[token] = TokenState(**state)
        return token

    # -- dispatch -------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            kind = self.fail_next.pop(0)
            if kind == "connect":
                raise httpx.ConnectError("connection refused", request=request)
        if self.rate_limit_next:
            retry_after = self.rate_limit_next.pop(0)
            return httpx.Response(
                429,
                headers={"Retry-After": retry_after},
                json={"error": {"code": "rate_limited", "message": "Too many calls."}},
            )
        path = request.url.path
        if path == "/workbench_agent_api":
            if not self.agent_api_available:
                return httpx.Response(404, text="Not Found")
            return self._agent(request)
        if path == "/workbench_cli_login":
            if not self.login_available:
                return httpx.Response(404, text="Not Found")
            return self._login(request)
        return httpx.Response(404, text="Not Found")

    def _body(self, request: httpx.Request) -> dict[str, Any] | httpx.Response:
        if request.method != "POST":
            return _error(405, "invalid_argument", "Use POST.")
        if len(request.content) > 2048:
            return _error(400, "invalid_argument", "The request is too large.")
        try:
            body = json.loads(request.content)
        except ValueError:
            return _error(400, "invalid_argument", "The body is not JSON.")
        if not isinstance(body, dict):
            return _error(400, "invalid_argument", "The body is not a JSON object.")
        return body

    # -- section 3 ------------------------------------------------------------

    def _agent(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        if isinstance(body, httpx.Response):
            return body
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        state = self.tokens.get(token)
        if state is None:
            return _error(401, "unauthenticated", "Sign-in failed.")
        if state.expired:
            return _error(401, "token_expired", "The token has expired.")
        if not state.workspace_ok:
            return _error(403, "forbidden", "No access.")
        if not state.early_access:
            return _error(403, "early_access_required", "Agent access is off.")
        action = body.get("action")
        if action == "whoami":
            return httpx.Response(
                200,
                json={
                    "workspace": {"id": "w_1", "name": "Sleep lab"},
                    "role": "member",
                    "scopes": ["read"],
                    "token": {
                        "id": auth.token_id(token),
                        "name": "laptop",
                        "createdAt": "2026-11-20T09:15:00Z",
                        "expiresAt": "2027-02-18T09:15:00Z",
                    },
                },
            )
        if action == "list_projects":
            return httpx.Response(200, json={"projects": self.projects})
        if action == "list_recordings":
            return self._list_recordings(body)
        if action == "get_recording":
            rec_id = body.get("id")
            if not isinstance(rec_id, str):
                return _error(400, "invalid_argument", "id is required.")
            detail = self.details.get(rec_id)
            if detail is None:
                return _error(404, "not_found", "Not found.")
            out = dict(detail)
            if out.get("held"):
                for key in ("stageMinutes", "eventCounts", "humanCounts"):
                    out.pop(key, None)
            return httpx.Response(200, json=out)
        return _error(400, "invalid_argument", "Unknown action.")

    def _list_recordings(self, body: dict[str, Any]) -> httpx.Response:
        project_id = body.get("projectId")
        limit = body.get("limit", 50)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            return _error(400, "invalid_argument", "limit must be 1 to 100.")
        if project_id is not None and project_id not in {p["id"] for p in self.projects}:
            return _error(404, "not_found", "Not found.")
        rows = [r for r in self.recordings if project_id is None or r["projectId"] == project_id]
        rows.sort(key=lambda r: (r["addedAt"], r["id"]), reverse=True)
        offset = 0
        cursor = body.get("cursor")
        if cursor is not None:
            prefix, _, rest = str(cursor).partition(":")
            if prefix != str(project_id) or not rest.isdigit():
                return _error(400, "invalid_argument", "The cursor is stale.")
            offset = int(rest)
        page = rows[offset : offset + limit]
        nxt = offset + limit
        return httpx.Response(
            200,
            json={
                "recordings": page,
                "nextCursor": f"{project_id}:{nxt}" if nxt < len(rows) else None,
                "truncated": False,
            },
        )

    # -- section 5 ------------------------------------------------------------

    def _login(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        if isinstance(body, httpx.Response):
            return body
        action = body.get("action")
        if action == "start":
            session = LoginSession(
                session_id=secrets.token_hex(16),
                poll_secret="".join(secrets.choice(auth.BASE62) for _ in range(43)),
            )
            self.sessions[session.session_id] = session
            return httpx.Response(
                200,
                json={
                    "sessionId": session.session_id,
                    "pollSecret": session.poll_secret,
                    "approvalUrl": f"https://workbench.test.invalid/cli/login/{session.session_id}",
                    "expiresAt": "2026-11-20T09:25:00Z",
                },
            )
        if action == "exchange":
            session = self.sessions.get(str(body.get("sessionId")))
            if (
                session is None
                or session.used
                or session.code is None
                or session.attempts_left <= 0
                or body.get("pollSecret") != session.poll_secret
            ):
                return _error(410, "login_expired", "This sign-in has expired.")
            given = "".join(ch for ch in str(body.get("code", "")).upper() if ch not in " -")
            if given != session.code:
                session.attempts_left -= 1
                return _error(
                    400, "code_wrong", "The code is wrong.", attemptsLeft=session.attempts_left
                )
            session.used = True
            token = self.add_token()
            return httpx.Response(
                200,
                json={
                    "token": token,
                    "tokenId": auth.token_id(token),
                    "workspaceId": "w_1",
                    "scopes": ["read"],
                    "expiresAt": "2027-02-18T09:25:00Z",
                },
            )
        return _error(400, "invalid_argument", "Unknown action.")

    def approve(self, url: str) -> str:
        """What a person does in the browser: approve, and read the code shown."""
        session_id = url.rstrip("/").rsplit("/", 1)[-1]
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        self.sessions[session_id].code = code
        return f"{code[:4]}-{code[4:]}"


def sample_data(api: FakeAPI) -> None:
    api.projects = [
        {
            "id": "p_1",
            "name": "Pilot nights",
            "createdAt": "2026-10-01T08:00:00Z",
            "updatedAt": "2026-11-18T14:02:11Z",
            "recordingCount": 3,
        }
    ]
    api.recordings = [
        {
            "id": f"e_{i}",
            "projectId": "p_1" if i < 3 else None,
            "addedAt": f"2026-11-{10 + i:02d}T08:00:00Z",
            "url": f"https://workbench.test.invalid/r/e_{i}",
            "durationSec": 28800.0,
            "nEpochs": 960,
            "device": "headband",
            "hasEvents": True,
            "futureKey": "ignored",
        }
        for i in range(5)
    ]
    api.details = {
        "e_1": {
            **api.recordings[1],
            "channels": {"count": 2, "types": {"eeg": 2}, "labels": ["F3", "ch2"]},
            "stageMinutes": {"W": 41.5, "N2": 201.5},
            "eventCounts": {"spindle": 812},
            "humanCounts": {"marks": 120, "notes": 3},
        },
        "e_2": {**api.recordings[2], "held": True, "stageMinutes": {"W": 1.0}},
    }
