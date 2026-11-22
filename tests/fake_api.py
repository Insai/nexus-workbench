"""An in-memory fake of the Workbench agent API (contract sections 3, 5 and 7).

Used through ``httpx.MockTransport``; no test touches the network. Version 1.1 (section 7)
is implemented as the 1.1 server behaves: scopes, the two workspace switches (access off
clears writes; approve refuses write scopes while writes are off), blinding, caps, the review
layer of agent marks, idempotent ``opId``, signed cursors, ``too_large`` events, the task
mail brake and ``mailed``, ``request_analysis`` answering 501 ``not_available``, the audit log
and the order of checks.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any

import httpx

from nexus_workbench import auth

API_BASE = "https://api.test.invalid"
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"

SCOPES = ("read", "detail", "marks:write", "tasks:write", "analysis:run")
WRITE_ACTIONS = {
    "propose_marks": "marks:write",
    "withdraw_marks": "marks:write",
    "create_review_task": "tasks:write",
    "request_analysis": "analysis:run",
}
DETAIL_ACTIONS = {"get_events", "get_stages", "get_quality", "list_marks"}
READ_V11_ACTIONS = {"list_members", "list_tasks", "list_nights", "list_layers"}
V11_ACTIONS = set(WRITE_ACTIONS) | DETAIL_ACTIONS | READ_V11_ACTIONS
MARK_KINDS = {
    "bad_signal",
    "movement",
    "electrode_pop",
    "flatline",
    "arousal",
    "spindle",
    "k_complex",
    "slow_wave",
    "sawtooth",
    "eye_movement",
}
MARK_CAP = 5000
MAX_BODY = 2048
MAX_PROPOSE_BODY = 262_144
OP_ID = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
MEMBER_ID = re.compile(r"^m_[0-9a-f]{16}$")
EMAIL = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
#: What the server refuses in a token name, and in a device name it then replaces.
NAMES_SOMEONE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+|\d{14}|[A-Za-z0-9]{28}")
CURSOR_KEY = b"fake-workspace-salt"
NOT_A_CURSOR = "'cursor' is not a cursor of this list."
#: Annotation rows one ``list_marks`` call reads (contract 7.6).
MARKS_READ = 5000
LAYER_RECORDINGS_MAX = 1000
MAIL_MIN_GAP = 60.0
MAIL_PER_HOUR = 5
STAGE_CODE = re.compile(r"W|N[123]|R|\?")

ME = "m_00000000000000a1"
OWNER = "m_00000000000000b2"
OTHER = "m_00000000000000c3"
SERVICE = "m_00000000000000ff"  # the analysis service member: not a person, never listed


def make_token() -> str:
    """A token built with the contract's algorithm (section 2)."""
    body = "".join(secrets.choice(auth.BASE62) for _ in range(auth.BODY_LENGTH))
    return auth.TOKEN_PREFIX + body + auth.check_digits(body)


def _error(status: int, code: str, message: str, **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}, **extra})


def _ok(body: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=body)


def _bad(message: str) -> httpx.Response:
    return _error(400, "invalid_argument", message)


def _not_found() -> httpx.Response:
    return _error(404, "not_found", "Not found.")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class Refused(Exception):
    """A refusal of a page callable (``approve``, ``set_write_access``), with its code."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.message = message


def _names_someone(text: str) -> bool:
    return bool(NAMES_SOMEONE.search(text))


def _clean(text: str) -> str:
    """Trim and drop control, invisible characters and line breaks (contract 7.7)."""
    return "".join(ch for ch in text if ch.isprintable()).strip()


@dataclass
class TokenState:
    workspace_ok: bool = True  # False -> forbidden
    expired: bool = False
    early_access: bool = True  # False -> early_access_required
    scopes: list[str] = field(default_factory=lambda: ["read"])
    member_id: str = ME
    staff: bool = False  # Insai staff may ask for an analysis of any recording
    name: str | None = None  # None: FakeAPI.token_name


@dataclass
class LoginSession:
    session_id: str
    poll_secret: str
    code: str | None = None
    attempts_left: int = 5
    used: bool = False
    scopes: list[str] = field(default_factory=lambda: ["read"])
    device_name: str = ""


@dataclass
class AgentRow:
    n: int
    mark: dict[str, Any]
    created_at: str
    withdrawn: bool = False
    verdicts: dict[str, int] = field(default_factory=lambda: {"agree": 0, "disagree": 0})


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
    # Retry-After values to send first; None sends a 429 without the header.
    rate_limit_next: list[str | None] = field(default_factory=list)
    fail_next: list[str] = field(default_factory=list)  # "connect" -> raise ConnectError
    workspace_name: str = "Sleep lab"
    token_name: str = "laptop"
    token_limit: bool = False  # True -> exchange with the right code returns 409 token_limit
    # Refusals to send first for one sign-in action: {"start": [(403, "forbidden")], ...}.
    login_errors: dict[str, list[tuple[int, str]]] = field(default_factory=dict)

    # -- version 1.1 (contract section 7) ---------------------------------------------------
    v11: bool = True  # False: a version 1 server, which answers 1.1 actions "Unknown action"
    agent_access: bool = True  # the workspace's agentAccess switch (section 7.2)
    agent_write: bool = False  # the agentWrite switch (section 7.2)
    members: list[dict[str, Any]] = field(default_factory=list)
    held: set[str] = field(default_factory=set)  # recordings the caller is held blind on
    blind_desk: set[str] = field(default_factory=set)  # the caller's desk hides the machine
    person_keys: dict[str, str] = field(default_factory=dict)  # recording id -> personKey
    events: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    machine_stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    my_stages: dict[str, dict[str, Any]] = field(default_factory=dict)
    quality: dict[str, dict[str, Any]] = field(default_factory=dict)
    human_marks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    machine_marks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    analysis_layers: list[dict[str, Any]] = field(default_factory=list)
    agent_rows: dict[tuple[str, str], list[AgentRow]] = field(default_factory=dict)
    op_ids: dict[tuple[str, str], tuple[str, dict[str, Any]]] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    source_unknown: set[str] = field(default_factory=set)  # recordings with no known source
    staged: set[tuple[str, str]] = field(default_factory=set)  # (member, recording)
    machine_seen: set[tuple[str, str]] = field(default_factory=set)  # (member, recording)
    uploader: dict[str, str] = field(default_factory=dict)  # recording id -> member id
    not_a_file: set[str] = field(default_factory=set)  # recordings not uploaded as a file
    pending_analysis: set[str] = field(default_factory=set)
    # False, as the 1.1 server today: request_analysis checks, then answers 501 not_available.
    # True: a later server that starts the run.
    analysis_available: bool = False
    analysis_service_down: bool = False
    too_large: set[str] = field(default_factory=set)  # recordings whose events are over 20 MB
    marks_read: int = MARKS_READ  # annotation rows one list_marks call reads
    audit: list[dict[str, Any]] = field(default_factory=list)
    workspace_audit: list[str] = field(default_factory=list)  # agent_write_off and the like
    write_calls: int = 0
    emails: list[str] = field(default_factory=list)  # member ids sent a task email
    clock: float = 0.0  # seconds; tests move it to pass the task mail brake
    _mail_times: dict[str, list[float]] = field(default_factory=dict)
    _batches: int = 0

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
                headers={} if retry_after is None else {"Retry-After": retry_after},
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

    def _body(
        self, request: httpx.Request, limit: int = MAX_BODY
    ) -> dict[str, Any] | httpx.Response:
        if request.method != "POST":
            return _error(405, "invalid_argument", "Use POST.")
        if len(request.content) > MAX_PROPOSE_BODY:
            return _bad("The request is too large.")
        try:
            body = json.loads(request.content)
        except ValueError:
            return _bad("The body is not JSON.")
        if not isinstance(body, dict):
            return _bad("The body is not a JSON object.")
        allowed = MAX_PROPOSE_BODY if body.get("action") == "propose_marks" else limit
        if len(request.content) > allowed:
            return _bad("The request is too large.")
        return body

    # -- sections 3 and 7: the order of checks of section 7.3 ---------------------------------

    def _token(self, request: httpx.Request) -> tuple[str, TokenState] | httpx.Response:
        header = request.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ") if header.startswith("Bearer ") else ""
        state = self.tokens.get(token)
        if state is None:
            return _error(401, "unauthenticated", "Sign in with a valid token.")
        if state.expired:
            return _error(401, "token_expired", "The token has expired.")
        return token, state

    def _known(self, action: Any) -> bool:
        v1 = {"whoami", "list_projects", "list_recordings", "get_recording"}
        return action in v1 or (self.v11 and action in V11_ACTIONS)

    def _agent(self, request: httpx.Request) -> httpx.Response:
        # The server's order: method and the largest body; a body past 2,048 bytes is parsed
        # for a verified token only; then the action's name and its own body limit.
        if request.method != "POST":
            return _error(405, "invalid_argument", "Use POST.")
        raw = request.content
        if len(raw) > MAX_PROPOSE_BODY:
            return _bad(f"The body is at most {MAX_PROPOSE_BODY} bytes.")
        verified = self._token(request) if len(raw) > MAX_BODY else None
        if isinstance(verified, httpx.Response):
            return verified
        try:
            body = json.loads(raw)
        except ValueError:
            return _bad("The body must be a JSON object.")
        if not isinstance(body, dict):
            return _bad("The body must be a JSON object.")
        action = body.get("action")
        if not self._known(action):
            return _bad("Unknown action.")
        if len(raw) > MAX_BODY and action != "propose_marks":
            return _bad(f"The body of {action} is at most {MAX_BODY} bytes.")
        if verified is None:
            verified = self._token(request)
            if isinstance(verified, httpx.Response):
                return verified
        token, state = verified
        if not state.workspace_ok:
            return _error(403, "forbidden", "No access.")
        if not state.early_access or not self.agent_access:
            return _error(403, "early_access_required", "Agent access is off.")
        need = WRITE_ACTIONS.get(str(action)) or ("detail" if action in DETAIL_ACTIONS else None)
        if need and need not in state.scopes:
            return _error(
                403,
                "scope_required",
                f"This token lacks the {need} scope. Make a token with the {need} scope.",
                scope=need,
            )
        if action in WRITE_ACTIONS and not self.write_on:
            return _error(
                403,
                "write_access_required",
                "Agent writes are not switched on for this workspace. Ask Insai.",
            )
        tid = auth.token_id(token)
        if action in WRITE_ACTIONS:
            self.write_calls += 1
        if action == "whoami":
            return _ok(
                {
                    "workspace": {"id": "w_1", "name": self.workspace_name},
                    "role": "member",
                    "scopes": [s for s in SCOPES if s in state.scopes],
                    "memberId": state.member_id,
                    "token": {
                        "id": tid,
                        "name": self.token_name if state.name is None else state.name,
                        "createdAt": "2026-11-20T09:15:00Z",
                        "expiresAt": "2027-02-18T09:15:00Z",
                    },
                }
            )
        if action == "list_projects":
            return _ok({"projects": self.projects})
        if action == "list_recordings":
            return self._list_recordings(body)
        if action == "get_recording":
            rec_id = body.get("id")
            if not isinstance(rec_id, str):
                return _bad("id is required.")
            detail = self.details.get(rec_id)
            if detail is None:
                return _not_found()
            out = dict(detail)
            if out.get("held"):
                for key in ("stageMinutes", "eventCounts", "humanCounts"):
                    out.pop(key, None)
            return _ok(out)
        handler = getattr(self, f"_a_{action}")
        return handler(body, state, tid)

    @property
    def write_on(self) -> bool:
        """``agentWrite`` in effect: never on while ``agentAccess`` is off (section 7.2)."""
        return self.agent_access and self.agent_write

    def _list_recordings(self, body: dict[str, Any]) -> httpx.Response:
        project_id = body.get("projectId")
        limit = body.get("limit", 50)
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            return _bad("limit must be 1 to 100.")
        if project_id is not None and project_id not in {p["id"] for p in self.projects}:
            return _not_found()
        rows = [r for r in self.recordings if project_id is None or r["projectId"] == project_id]
        rows.sort(key=lambda r: (r["addedAt"], r["id"]), reverse=True)
        offset = 0
        cursor = body.get("cursor")
        if cursor is not None:
            prefix, _, rest = str(cursor).partition(":")
            if prefix != str(project_id) or not rest.isdigit():
                return _bad("The cursor is stale.")
            offset = int(rest)
        page = rows[offset : offset + limit]
        nxt = offset + limit
        return _ok(
            {
                "recordings": page,
                "nextCursor": f"{project_id}:{nxt}" if nxt < len(rows) else None,
                "truncated": False,
            }
        )

    # -- helpers ----------------------------------------------------------------------------

    def _rec(self, rec_id: Any) -> dict[str, Any] | None:
        if not isinstance(rec_id, str):
            return None
        return next((r for r in self.recordings if r["id"] == rec_id), None)

    def _hidden(self, rec_id: str) -> bool:
        """Machine hidden: held, or a blind desk (section 7.4)."""
        return rec_id in self.held or rec_id in self.blind_desk

    def _project_rows(self, body: dict[str, Any]) -> list[dict[str, Any]] | httpx.Response:
        project_id = body.get("projectId")
        if project_id is not None and not isinstance(project_id, str):
            return _bad("projectId must be text.")
        if project_id is not None and project_id not in {p["id"] for p in self.projects}:
            return _not_found()
        return [r for r in self.recordings if project_id is None or r["projectId"] == project_id]

    @staticmethod
    def _cursor_args(action: str, body: dict[str, Any], keys: tuple[str, ...]) -> str:
        return action + "|" + json.dumps([body.get(k) for k in keys])

    def _page(
        self,
        action: str,
        body: dict[str, Any],
        rows: list[Any],
        keys: tuple[str, ...],
        default: int,
        most: int,
    ) -> tuple[list[Any], str | None] | httpx.Response:
        limit = body.get("limit", default)
        if not _is_int(limit) or not 1 <= limit <= most:
            return _bad(f"limit must be 1 to {most}.")
        offset = self._read_cursor(action, body, keys)
        if isinstance(offset, httpx.Response):
            return offset
        nxt = offset + limit
        return rows[offset:nxt], (
            self._cursor(action, body, keys, nxt) if nxt < len(rows) else None
        )

    def _cursor(self, action: str, body: dict[str, Any], keys: tuple[str, ...], at: int) -> str:
        """A signed cursor: bound to the action and its arguments (as the server's are)."""
        text = f"{self._cursor_args(action, body, keys)}|{at}"
        sig = hmac.new(CURSOR_KEY, text.encode(), hashlib.sha256).hexdigest()[:16]
        return f"{at}.{sig}"

    def _read_cursor(
        self, action: str, body: dict[str, Any], keys: tuple[str, ...]
    ) -> int | httpx.Response:
        cursor = body.get("cursor")
        if cursor is None:
            return 0
        at, _, sig = str(cursor).partition(".")
        if not at.isdigit():
            return _bad(NOT_A_CURSOR)
        text = f"{self._cursor_args(action, body, keys)}|{at}"
        want = hmac.new(CURSOR_KEY, text.encode(), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(sig, want):
            return _bad(NOT_A_CURSOR)  # modified, or made for other arguments
        return int(at)

    def _audit(
        self,
        tid: str,
        state: TokenState,
        action: str,
        rec: str | None,
        recordings: list[str] | None = None,
        **counts: int,
    ) -> None:
        row: dict[str, Any] = {
            "action": action,
            "by": state.member_id,
            "via": "agent",
            "tokenId": tid,
            "recordingId": rec,
            "counts": counts,
        }
        if recordings is not None:
            row["recordingIds"] = list(recordings)
        self.audit.append(row)

    # -- 7.6 reads -----------------------------------------------------------------------------

    def _a_list_members(self, body: dict[str, Any], state: TokenState, tid: str) -> httpx.Response:
        rows = []
        for m in sorted(self.members, key=lambda m: m["id"]):
            row = {"id": m["id"], "role": m["role"]}
            if m["id"] == state.member_id:
                row["you"] = True
            rows.append(row)
        return _ok({"members": rows})

    def _a_list_tasks(self, body: dict[str, Any], state: TokenState, tid: str) -> httpx.Response:
        mine = body.get("mine", True)
        if not isinstance(mine, bool):
            return _bad("mine must be true or false.")
        me = state.member_id
        rows = [t for t in self.tasks if t["assignee"] == me or (not mine and t["givenBy"] == me)]
        rows.sort(key=lambda t: t["id"])
        rows.sort(key=lambda t: t["givenAt"], reverse=True)
        keys = ("id", "recordingId", "projectId", "assignee", "givenBy", "mode", "state")
        out = [{k: t[k] for k in (*keys, "givenAt", "doneAt") if k in t} for t in rows[:500]]
        return _ok({"tasks": out})

    def _a_list_nights(self, body: dict[str, Any], state: TokenState, tid: str) -> httpx.Response:
        rows = self._project_rows(body)
        if isinstance(rows, httpx.Response):
            return rows
        by_pair: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if r.get("pairId"):
                by_pair.setdefault(r["pairId"], []).append(r)
        nights = []
        for pair, recs in sorted(by_pair.items()):
            if len(recs) < 2:
                continue
            night: dict[str, Any] = {"id": pair, "recordingIds": sorted(r["id"] for r in recs)}
            projects = {r["projectId"] for r in recs}
            if len(projects) == 1 and None not in projects:
                night["projectId"] = projects.pop()
            keys = {self.person_keys.get(r["id"]) for r in recs}
            if len(keys) == 1 and None not in keys:
                night["personKey"] = keys.pop()
            nights.append(night)
        return _ok({"nights": nights, "truncated": False})

    def _a_list_layers(self, body: dict[str, Any], state: TokenState, tid: str) -> httpx.Response:
        rows = self._project_rows(body)
        if isinstance(rows, httpx.Response):
            return rows
        # Layers are found on, and list, the newest 1,000 recordings (section 7.6).
        newest = sorted(rows, key=lambda r: (r["addedAt"], r["id"]), reverse=True)
        ids = [r["id"] for r in newest[:LAYER_RECORDINGS_MAX]]
        more = len(newest) > LAYER_RECORDINGS_MAX
        layers = []
        for layer in self.analysis_layers:
            recs = []
            for rec_id in ids:
                if self._hidden(rec_id):
                    recs.append({"id": rec_id, "hidden": "blind"})
                    continue
                entry = layer["recordings"].get(rec_id, {"state": "missing"})
                recs.append({"id": rec_id, **entry})
            layers.append(
                {
                    "source": layer["source"],
                    "name": layer["name"],
                    "version": layer["version"],
                    "origin": "analysis",
                    "status": layer.get("status", "unknown"),
                    "recordings": recs,
                    "truncated": more,
                }
            )
        agent_tokens = sorted({t for (t, _r) in self.agent_rows})
        for token_id in agent_tokens:
            recs = []
            for rec_id in ids:
                if self._hidden(rec_id):
                    recs.append({"id": rec_id, "hidden": "blind"})
                    continue
                live = [a for a in self.agent_rows.get((token_id, rec_id), []) if not a.withdrawn]
                if (token_id, rec_id) not in self.agent_rows:
                    recs.append({"id": rec_id, "state": "missing"})
                    continue
                counts: dict[str, int] = {}
                for a in live:
                    counts[a.mark["kind"]] = counts.get(a.mark["kind"], 0) + 1
                recs.append({"id": rec_id, "state": "have", "rows": len(live), "counts": counts})
            layers.append(
                {
                    "source": f"agent:{token_id}",
                    "name": "agent",
                    "version": token_id,
                    "origin": "agent",
                    "status": "proposed",
                    "recordings": recs,
                    "truncated": more,
                }
            )
        return _ok({"layers": layers, "truncated": False})

    def _detail_rec(self, body: dict[str, Any]) -> dict[str, Any] | httpx.Response:
        rec_id = body.get("id")
        if not isinstance(rec_id, str) or not rec_id:
            return _bad("id is required.")
        rec = self._rec(rec_id)
        if rec is None:
            return _not_found()
        return rec

    def _a_get_events(self, body: dict[str, Any], state: TokenState, tid: str) -> httpx.Response:
        rec = self._detail_rec(body)
        if isinstance(rec, httpx.Response):
            return rec
        kind = body.get("kind")
        if kind is not None and not isinstance(kind, str):
            return _bad("kind must be text.")
        if self._hidden(rec["id"]):
            return _ok({"hidden": "blind"})
        if not rec.get("hasEvents"):
            return _ok({"available": False})
        if rec["id"] in self.too_large:
            return _error(
                409,
                "too_large",
                "This recording's events are too large to read through the agent API. "
                "Open it in Workbench.",
            )
        events = self.events.get(rec["id"], [])
        rows = sorted(
            (e for e in events if kind is None or e["kind"] == kind),
            key=lambda e: (e["onsetSec"], e.get("channel", 0), e["kind"]),
        )
        page = self._page("get_events", body, rows, ("id", "kind"), 500, 1000)
        if isinstance(page, httpx.Response):
            return page
        out, nxt = page
        return _ok(
            {
                "available": True,
                "kinds": sorted({e["kind"] for e in events}),
                "events": out,
                "nextCursor": nxt,
            }
        )

    def _a_get_stages(self, body: dict[str, Any], state: TokenState, tid: str) -> httpx.Response:
        rec = self._detail_rec(body)
        if isinstance(rec, httpx.Response):
            return rec
        source = body.get("source")
        if source not in ("machine", "mine"):
            return _bad("source must be machine or mine.")
        if source == "machine":
            if self._hidden(rec["id"]):
                return _ok({"source": "machine", "hidden": "blind"})
            got = self.machine_stages.get(rec["id"])
            if got is None:
                return _ok({"source": "machine", "available": False})
            stages = got["stages"]
            return _ok(
                {
                    "source": "machine",
                    "available": True,
                    "model": got.get("model", "usleep"),
                    "epochSec": got.get("epochSec", 30),
                    "nEpochs": len(STAGE_CODE.findall(stages)),
                    "stages": stages,
                }
            )
        mine = self.my_stages.get(rec["id"])
        if mine is None:
            return _ok({"source": "mine", "available": False})
        return _ok(
            {
                "source": "mine",
                "available": True,
                "status": mine.get("status", "in_progress"),
                "epochSec": mine.get("epochSec", 30),
                "nEpochs": len(STAGE_CODE.findall(mine["stages"])),
                "stages": mine["stages"],
                "questionable": mine.get("questionable", []),
                "noSignal": mine.get("noSignal", []),
            }
        )

    def _a_get_quality(self, body: dict[str, Any], state: TokenState, tid: str) -> httpx.Response:
        rec = self._detail_rec(body)
        if isinstance(rec, httpx.Response):
            return rec
        got = self.quality.get(rec["id"])  # not blinded (section 7.4)
        if got is None:
            return _ok({"available": False})
        return _ok({"available": True, **got})

    def _a_list_marks(self, body: dict[str, Any], state: TokenState, tid: str) -> httpx.Response:
        rec = self._detail_rec(body)
        if isinstance(rec, httpx.Response):
            return rec
        origin = body.get("origin")
        if origin not in (None, "human", "machine", "agent"):
            return _bad("origin must be human, machine or agent.")
        rec_id = rec["id"]
        held = rec_id in self.held
        hidden = self._hidden(rec_id)
        if hidden and origin in ("machine", "agent"):
            return _ok({"marks": [], "nextCursor": None, "hidden": "blind"})
        # Every stored row, in (t0, id) order, with whether this call lists it: the server reads
        # up to ``marks_read`` stored rows a call, so a page may come back short with a cursor.
        stored: list[tuple[dict[str, Any], bool]] = []
        for m in self.human_marks.get(rec_id, []):
            keep = not (m.get("deleted") or m.get("kind") == "note")
            if held and m.get("author") != state.member_id:
                keep = False
            stored.append(({k: v for k, v in m.items() if k not in ("deleted", "text")}, keep))
        for m in self.machine_marks.get(rec_id, []):
            stored.append((dict(m, origin="machine"), not hidden))
        rows: list[dict[str, Any]] = []
        if not hidden:
            for (token_id, r), agent in self.agent_rows.items():
                if r != rec_id:
                    continue
                for a in agent:
                    if a.withdrawn:
                        continue
                    channels = a.mark.get("channels") or []
                    row = {
                        "id": f"agent-{token_id}-{a.n}",
                        "origin": "agent",
                        "source": f"agent:{token_id}",
                        "kind": a.mark["kind"],
                        "target": "channel" if channels else "epoch",
                        "t0": a.mark["t0"],
                        "t1": a.mark["t1"],
                        "channels": channels,
                        "verdicts": dict(a.verdicts),
                        "createdAt": a.created_at,
                    }
                    for key in ("label", "text", "confidence"):
                        if key in a.mark:
                            row[key] = a.mark[key]
                    rows.append(row)
        stored += [(r, True) for r in rows]
        stored = [(r, keep and (origin is None or r["origin"] == origin)) for r, keep in stored]
        stored.sort(key=lambda pair: (pair[0]["t0"], pair[0]["id"]))
        limit = body.get("limit", 200)
        if not _is_int(limit) or not 1 <= limit <= 500:
            return _bad("limit must be 1 to 500.")
        offset = self._read_cursor("list_marks", body, ("id", "origin"))
        if isinstance(offset, httpx.Response):
            return offset
        out: list[dict[str, Any]] = []
        at = offset
        for row, keep in stored[offset : offset + self.marks_read]:
            if len(out) == limit:
                break
            at += 1
            if keep:
                out.append(row)
        nxt = self._cursor("list_marks", body, ("id", "origin"), at) if at < len(stored) else None
        result: dict[str, Any] = {"marks": out, "nextCursor": nxt}
        if hidden:
            result["hidden"] = "blind"
        return _ok(result)

    # -- 7.7 writes ----------------------------------------------------------------------------

    def _check_mark(self, i: int, mark: Any, rec: dict[str, Any]) -> str | dict[str, Any]:
        where = f"Mark {i + 1}"
        if not isinstance(mark, dict):
            return f"{where} is not an object."
        kind = mark.get("kind")
        if kind not in MARK_KINDS:
            return f"{where}: kind is not one of the annotation kinds."
        t0, t1 = mark.get("t0"), mark.get("t1")
        if not _is_num(t0) or not _is_num(t1):
            return f"{where}: t0 and t1 must be numbers."
        duration = rec.get("durationSec")
        if not 0 <= t0 <= t1 or (duration is not None and t1 > duration):
            return f"{where}: t0 and t1 must satisfy 0 <= t0 <= t1 <= durationSec."
        out: dict[str, Any] = {"kind": kind, "t0": t0, "t1": t1}
        channels = mark.get("channels")
        if channels is not None:
            count = (self.details.get(rec["id"], {}).get("channels") or {}).get("count", 4)
            if (
                not isinstance(channels, list)
                or len(channels) > 16
                or not all(_is_int(c) and 1 <= c <= count for c in channels)
                or len(set(channels)) != len(channels)
            ):
                return f"{where}: channels must be distinct positions within the channel count."
            if channels:
                out["channels"] = channels
        for key, most in (("label", 40), ("text", 200)):
            if key not in mark:
                continue
            value = mark[key]
            if not isinstance(value, str):
                return f"{where}: {key} must be text."
            cleaned = _clean(value)
            if not cleaned or len(cleaned) > most:
                return f"{where}: {key} must be 1 to {most} characters."
            if EMAIL.search(cleaned) or "tok_" in cleaned or self._rec_id_in(cleaned):
                return f"{where}: {key} may not hold an email address, account id or recording id."
            out[key] = cleaned
        if "confidence" in mark:
            c = mark["confidence"]
            if not _is_num(c) or not 0 <= c <= 1:
                return f"{where}: confidence must be a number from 0 to 1."
            out["confidence"] = c
        return out

    def _rec_id_in(self, text: str) -> bool:
        return any(r["id"] in text for r in self.recordings if len(r["id"]) >= 3)

    def _a_propose_marks(self, body: dict[str, Any], state: TokenState, tid: str) -> httpx.Response:
        rec = self._detail_rec(body)
        if isinstance(rec, httpx.Response):
            return rec
        if rec["id"] in self.held:
            return _error(403, "forbidden", "You are held on this recording.")
        marks = body.get("marks")
        if not isinstance(marks, list) or not 1 <= len(marks) <= 500:
            return _bad("marks must be a list of 1 to 500 marks.")
        op_id = body.get("opId")
        if op_id is not None and (not isinstance(op_id, str) or not OP_ID.match(op_id)):
            return _bad("opId must be 1 to 64 characters from 0-9A-Za-z_-.")
        checked = []
        for i, mark in enumerate(marks):
            got = self._check_mark(i, mark, rec)
            if isinstance(got, str):
                return _bad(got)
            checked.append(got)
        digest = hashlib.sha256(json.dumps([rec["id"], marks], sort_keys=True).encode()).hexdigest()
        if op_id is not None:
            seen = self.op_ids.get((tid, op_id))
            if seen is not None:
                if seen[0] != digest:
                    return _bad("This opId was used with a different body.")
                return _ok(seen[1])
        rows = self.agent_rows.setdefault((tid, rec["id"]), [])
        if len(rows) + len(checked) > MARK_CAP:
            if not rows:
                del self.agent_rows[(tid, rec["id"])]
            return _error(409, "mark_limit", "Too many marks on this recording.")
        start = len(rows)
        for k, mark in enumerate(checked):
            rows.append(AgentRow(n=start + k + 1, mark=mark, created_at="2026-11-22T10:00:00Z"))
        ids = [f"agent-{tid}-{start + k + 1}" for k in range(len(checked))]
        live = sum(1 for a in rows if not a.withdrawn)
        result = {"source": f"agent:{tid}", "accepted": len(checked), "ids": ids, "total": live}
        if op_id is not None:
            self.op_ids[(tid, op_id)] = (digest, result)
        self._audit(tid, state, "propose_marks", rec["id"], accepted=len(checked))
        return _ok(result)

    def _a_withdraw_marks(
        self, body: dict[str, Any], state: TokenState, tid: str
    ) -> httpx.Response:
        rec = self._detail_rec(body)
        if isinstance(rec, httpx.Response):
            return rec
        if rec["id"] in self.held:
            return _error(403, "forbidden", "You are held on this recording.")
        rows = self.agent_rows.get((tid, rec["id"]), [])
        mark_ids = body.get("markIds")
        if mark_ids is None:
            chosen = [a for a in rows if not a.withdrawn]
        else:
            if (
                not isinstance(mark_ids, list)
                or not 1 <= len(mark_ids) <= 500
                or not all(isinstance(m, str) for m in mark_ids)
            ):
                return _bad("markIds must be a list of 1 to 500 mark ids.")
            by_id = {f"agent-{tid}-{a.n}": a for a in rows}
            if any(m not in by_id for m in mark_ids):
                return _not_found()
            chosen = [by_id[m] for m in dict.fromkeys(mark_ids)]
        changed = [a for a in chosen if not a.withdrawn]
        for a in changed:
            a.withdrawn = True
        if changed:
            self._audit(tid, state, "withdraw_marks", rec["id"], withdrawn=len(changed))
        live = sum(1 for a in rows if not a.withdrawn)
        return _ok({"withdrawn": len(chosen), "total": live})

    def _a_create_review_task(
        self, body: dict[str, Any], state: TokenState, tid: str
    ) -> httpx.Response:
        ids = body.get("ids")
        member = body.get("memberId")
        mode = body.get("mode")
        confirm = body.get("confirmMachineSeen", False)
        if (
            not isinstance(ids, list)
            or not 1 <= len(ids) <= 100
            or not all(isinstance(i, str) and i for i in ids)
            or len(set(ids)) != len(ids)
        ):
            return _bad("ids must be 1 to 100 distinct recording ids.")
        if mode not in ("blind", "assisted"):
            return _bad("mode must be blind or assisted.")
        if not isinstance(confirm, bool):
            return _bad("confirmMachineSeen must be true or false.")
        if not isinstance(member, str) or not MEMBER_ID.match(member) or member == SERVICE:
            return _bad("memberId is not a member id.")
        if member not in {m["id"] for m in self.members}:
            return _not_found()
        given: list[str] = []
        refused: list[dict[str, str]] = []
        for rec_id in ids:
            code = None
            if self._rec(rec_id) is None:
                code = "not_found"
            elif any(
                t["assignee"] == member and t["recordingId"] == rec_id and t["state"] == "open"
                for t in self.tasks
            ):
                code = "open_task"
            elif mode == "blind" and rec_id in self.source_unknown:
                code = "no_source"
            elif mode == "blind" and (member, rec_id) in self.staged:
                code = "staged"
            elif mode == "blind" and (member, rec_id) in self.machine_seen and not confirm:
                code = "machine_seen"
            if code:
                refused.append({"id": rec_id, "code": code})
            else:
                given.append(rec_id)
        batch = None
        if given:
            self._batches += 1
            batch = f"batch_{self._batches:04d}"
            for rec_id in given:
                rec = self._rec(rec_id) or {}
                self.tasks.append(
                    {
                        "id": "task_"
                        + hashlib.sha256(f"{member}{rec_id}{batch}".encode()).hexdigest()[:16],
                        "recordingId": rec_id,
                        "projectId": rec.get("projectId"),
                        "assignee": member,
                        "givenBy": state.member_id,
                        "mode": mode,
                        "state": "open",
                        "givenAt": "2026-11-22T10:00:00Z",
                        "doneAt": None,
                    }
                )
            mailed = member != state.member_id and self._mail_allowed(member)
            if mailed:
                self.emails.append(member)
            # One row per call, naming every recording given.
            self._audit(tid, state, "create_review_task", None, recordings=given, given=len(given))
        else:
            mailed = False
        return _ok({"batchId": batch, "given": given, "refused": refused, "mailed": mailed})

    def _mail_allowed(self, member: str) -> bool:
        """The task mail brake: 1 a minute and 5 an hour per member (section 7.7)."""
        times = [t for t in self._mail_times.get(member, []) if self.clock - t < 3600]
        if (times and self.clock - times[-1] < MAIL_MIN_GAP) or len(times) >= MAIL_PER_HOUR:
            self._mail_times[member] = times
            return False
        self._mail_times[member] = [*times, self.clock]
        return True

    def _a_request_analysis(
        self, body: dict[str, Any], state: TokenState, tid: str
    ) -> httpx.Response:
        rec = self._detail_rec(body)
        if isinstance(rec, httpx.Response):
            return rec
        rec_id = rec["id"]
        # The server's order: already analysed, already running, not a file, not the uploader,
        # then 501 until the analysis service takes a service identity.
        if rec.get("machineAvailable") or rec.get("hasEvents"):
            return _error(409, "already_analysed", "This recording already has machine output.")
        if rec_id in self.pending_analysis:
            return _ok({"status": "already"})
        if rec_id in self.not_a_file:
            return _bad("This recording was not uploaded as a file, so it cannot be analysed here.")
        if not state.staff and self.uploader.get(rec_id) != state.member_id:
            return _error(
                403,
                "forbidden",
                "Only the person who uploaded this recording, or Insai staff, can analyse it.",
            )
        if not self.analysis_available:
            return _error(
                501,
                "not_available",
                "Asking for an analysis is not available through the agent API yet. "
                "Use Analyse in Workbench.",
            )
        if self.analysis_service_down:
            return _error(500, "internal", "Try again.")
        self.pending_analysis.add(rec_id)
        rec["analysing"] = True
        self._audit(tid, state, "request_analysis", rec_id)
        return _ok({"status": "started"})

    # -- section 5 ------------------------------------------------------------

    def _login(self, request: httpx.Request) -> httpx.Response:
        body = self._body(request)
        if isinstance(body, httpx.Response):
            return body
        action = body.get("action")
        queued = self.login_errors.get(str(action))
        if queued:
            status, code = queued.pop(0)
            return _error(status, code, "Refused.")
        if action == "start":
            asked = body.get("scopes", ["read"])
            if not isinstance(asked, list) or any(s not in SCOPES for s in asked):
                return _bad("Unknown scope.")
            session = LoginSession(
                session_id=secrets.token_hex(16),
                poll_secret="".join(secrets.choice(auth.BASE62) for _ in range(43)),
                scopes=[s for s in SCOPES if s in {"read", *asked}],
                device_name=str(body.get("deviceName", ""))[:64],
            )
            self.sessions[session.session_id] = session
            return _ok(
                {
                    "sessionId": session.session_id,
                    "pollSecret": session.poll_secret,
                    "approvalUrl": f"https://workbench.test.invalid/cli/login/{session.session_id}",
                    "expiresAt": "2026-11-20T09:25:00Z",
                }
            )
        if action == "exchange":
            session = self.sessions.get(str(body.get("sessionId")))
            if (
                session is None
                or session.used
                or session.attempts_left <= 0
                or body.get("pollSecret") != session.poll_secret
            ):
                return _error(410, "login_expired", "This sign-in has expired.")
            if session.code is None:  # not approved yet: refused, no attempt used
                return _bad("Approve the sign-in first.")
            given = "".join(ch for ch in str(body.get("code", "")).upper() if ch not in " -")
            if given != session.code:
                session.attempts_left -= 1
                return _error(
                    400, "code_wrong", "The code is wrong.", attemptsLeft=session.attempts_left
                )
            if self.token_limit:  # the session stays approved
                return _error(409, "token_limit", "You already have 10 tokens.")
            session.used = True
            # The device name names the token, unless it holds an email or an id.
            name = session.device_name.strip()
            if not name or _names_someone(name):
                name = "nexus-workbench"
            token = self.add_token(scopes=list(session.scopes), name=name)
            return _ok(
                {
                    "token": token,
                    "tokenId": auth.token_id(token),
                    "workspaceId": "w_1",
                    "scopes": list(session.scopes),
                    "expiresAt": "2027-02-18T09:25:00Z",
                }
            )
        return _bad("Unknown action.")

    def approve(self, url: str, scopes: list[str] | None = None) -> str:
        """What a person does in the browser: approve (optionally fewer scopes), read the code.

        Raises :class:`Refused` as the callable refuses: ``early_access_required`` when agent
        access is off, ``write_access_required`` when the approved scopes hold a write scope
        and the workspace's writes are off.
        """
        session_id = url.rstrip("/").rsplit("/", 1)[-1]
        session = self.sessions[session_id]
        if not self.agent_access:
            raise Refused("early_access_required", "Agent access is not switched on.")
        approved = list(session.scopes)
        if scopes is not None:
            assert "read" in scopes and set(scopes) <= set(session.scopes)
            approved = [s for s in SCOPES if s in scopes]
        if set(approved) & set(WRITE_ACTIONS.values()) and not self.write_on:
            raise Refused(
                "write_access_required",
                "Agent writes are not switched on for this workspace. Ask Insai.",
            )
        session.scopes = approved
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        session.code = code
        return f"{code[:4]}-{code[4:]}"

    def review_get(self, url: str) -> dict[str, Any]:
        """``workbench_cli_login_review get`` for a pending session, as the page reads it."""
        session = self.sessions[url.rstrip("/").rsplit("/", 1)[-1]]
        return {
            "status": "used" if session.used else ("approved" if session.code else "pending"),
            "deviceName": session.device_name,
            "scopes": list(session.scopes),
            "workspaces": [
                {
                    "id": "w_1",
                    "name": self.workspace_name,
                    "agentAccess": self.agent_access,
                    "agentWrite": self.write_on,
                }
            ],
        }

    def token_list(self) -> dict[str, Any]:
        """``workbench_agent_token list``: the switches as the page reads them."""
        return {"agentAccess": self.agent_access, "agentWrite": self.write_on}

    def create_token(self, name: str, scopes: list[str] | None = None) -> str:
        """``workbench_agent_token create``: write scopes are allowed while writes are off."""
        name = name.strip()
        if not 1 <= len(name) <= 64:
            raise Refused("invalid_argument", "A name is 1 to 64 characters.")
        if _names_someone(name):
            raise Refused("invalid_argument", "A name may not hold an email address or an id.")
        if not self.agent_access:
            raise Refused("early_access_required", "Agent access is not switched on.")
        asked = scopes or ["read"]
        if any(s not in SCOPES for s in asked):
            raise Refused("invalid_argument", "Unknown scope.")
        return self.add_token(scopes=[s for s in SCOPES if s in {"read", *asked}], name=name)

    def set_access(self, on: bool) -> None:
        """Staff ``set_access``: off also turns writes off; on leaves them off."""
        if self.agent_access != on:
            self.agent_access = on
            self.workspace_audit.append("agent_access_on" if on else "agent_access_off")
        if not on and self.agent_write:
            self.agent_write = False
            self.workspace_audit.append("agent_write_off")

    def set_write_access(self, on: bool) -> None:
        """Staff ``set_write_access``: on is refused while access is off; off always works."""
        if on and not self.agent_access:
            raise Refused("early_access_required", "Agent access is not switched on.")
        if self.agent_write != on:
            self.agent_write = on
            self.workspace_audit.append("agent_write_on" if on else "agent_write_off")


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


def sample_data_v11(api: FakeAPI) -> None:
    """Section 7 data on top of :func:`sample_data`.

    ``e_0`` and ``e_1`` are one night of one person; ``e_2`` is held; ``e_3`` has a blind desk;
    ``e_1`` has machine stages, events, quality and marks; ``e_4`` has no machine output yet
    and was uploaded by the caller.
    """
    sample_data(api)
    for rec in api.recordings:
        rec.pop("futureKey", None)
    api.recordings[0]["pairId"] = "n_1"
    api.recordings[1]["pairId"] = "n_1"
    api.recordings[1]["machineAvailable"] = True
    api.recordings[2]["pairId"] = "n_2"
    api.recordings[3]["pairId"] = "n_2"
    api.recordings[4]["hasEvents"] = False
    api.person_keys = {"e_0": "pk_0123456789abcdef", "e_1": "pk_0123456789abcdef"}
    api.members = [
        {"id": ME, "role": "member"},
        {"id": OWNER, "role": "owner"},
        {"id": OTHER, "role": "member"},
    ]
    api.held = {"e_2"}
    api.blind_desk = {"e_3"}
    stages = "W" * 4 + "N1" * 2 + "N2" * 10 + "N3" * 6 + "R" * 4 + "W" * 2 + "?" * 2
    api.machine_stages = {"e_1": {"stages": stages, "model": "usleep", "epochSec": 30}}
    api.my_stages = {
        "e_1": {
            "status": "in_progress",
            "stages": "W" * 4 + "N2" * 12 + "N3" * 6 + "R" * 4 + "W" * 2 + "?" * 2,
            "questionable": [5],
            "noSignal": [29],
        }
    }
    api.events = {
        "e_1": [
            {
                "onsetSec": 300.0,
                "durationSec": 1.0,
                "kind": "spindle",
                "channel": 1,
                "confidence": 0.8,
            },
            {"onsetSec": 300.0, "durationSec": 1.0, "kind": "spindle", "channel": 2},
            {"onsetSec": 420.5, "durationSec": 0.9, "kind": "spindle", "channel": 1},
            {"onsetSec": 600.0, "durationSec": 3.0, "kind": "arousal"},
        ]
    }
    api.quality = {
        "e_1": {
            "grade": "marginal",
            "score": 0.71,
            "epochSec": 30,
            "perEpoch": "g" * 20 + "m" * 6 + "b" * 2 + "?" * 2,
            "channels": [
                {
                    "index": 1,
                    "grade": "good",
                    "score": 0.9,
                    "shares": {"ok": 0.96, "suspect": 0.03, "flat": 0.01},
                },
                {
                    "index": 2,
                    "grade": "bad",
                    "score": 0.3,
                    "shares": {"ok": 0.4, "suspect": 0.2, "flat": 0.4},
                    "excluded": "flat",
                },
            ],
        }
    }
    api.human_marks = {
        "e_1": [
            {
                "id": "h1",
                "origin": "human",
                "kind": "arousal",
                "target": "channel",
                "t0": 100.0,
                "t1": 104.0,
                "channels": [1],
                "author": ME,
                "createdAt": "2026-11-20T09:15:00Z",
            },
            {
                "id": "h2",
                "origin": "human",
                "kind": "movement",
                "target": "epoch",
                "t0": 50.0,
                "t1": 80.0,
                "channels": [],
                "author": OTHER,
                "createdAt": "2026-11-20T09:16:00Z",
                "text": "a person's own words",
            },
            {
                "id": "h3",
                "origin": "human",
                "kind": "note",
                "target": "epoch",
                "t0": 1.0,
                "t1": 1.0,
                "channels": [],
                "author": ME,
            },
        ],
        "e_2": [
            {
                "id": "h4",
                "origin": "human",
                "kind": "arousal",
                "target": "epoch",
                "t0": 10.0,
                "t1": 12.0,
                "channels": [],
                "author": ME,
            },
            {
                "id": "h5",
                "origin": "human",
                "kind": "arousal",
                "target": "epoch",
                "t0": 20.0,
                "t1": 22.0,
                "channels": [],
                "author": OTHER,
            },
        ],
        "e_3": [
            {
                "id": "h6",
                "origin": "human",
                "kind": "arousal",
                "target": "epoch",
                "t0": 10.0,
                "t1": 12.0,
                "channels": [],
                "author": OTHER,
            },
        ],
    }
    api.machine_marks = {
        "e_1": [
            {
                "id": "analysis-ladder-3-00017",
                "source": "analysis:ladder-3",
                "kind": "spindle",
                "target": "channel",
                "t0": 300.0,
                "t1": 301.0,
                "channels": [1],
                "label": "sigma burst",
                "confidence": 0.81,
                "verdicts": {"agree": 2, "disagree": 0},
            },
        ],
        "e_3": [
            {
                "id": "analysis-ladder-3-00001",
                "source": "analysis:ladder-3",
                "kind": "spindle",
                "target": "epoch",
                "t0": 1.0,
                "t1": 2.0,
                "channels": [],
            },
        ],
    }
    api.analysis_layers = [
        {
            "source": "analysis:ladder-3",
            "name": "ladder",
            "version": "3",
            "status": "stable",
            "recordings": {
                "e_1": {"state": "have", "rows": 1, "counts": {"spindle": 1}},
                "e_0": {"state": "notRun"},
                "e_3": {"state": "have", "rows": 1, "counts": {"spindle": 1}},
            },
        }
    ]
    api.tasks = [
        {
            "id": "task_00000000000000a1",
            "recordingId": "e_0",
            "projectId": "p_1",
            "assignee": ME,
            "givenBy": OWNER,
            "mode": "blind",
            "state": "open",
            "givenAt": "2026-11-21T10:00:00Z",
            "doneAt": None,
        },
        {
            "id": "task_00000000000000b2",
            "recordingId": "e_1",
            "projectId": "p_1",
            "assignee": OTHER,
            "givenBy": ME,
            "mode": "assisted",
            "state": "done",
            "givenAt": "2026-11-20T10:00:00Z",
            "doneAt": "2026-11-21T08:00:00Z",
        },
        {
            "id": "task_00000000000000c3",
            "recordingId": "e_1",
            "projectId": "p_1",
            "assignee": OTHER,
            "givenBy": OWNER,
            "mode": "blind",
            "state": "open",
            "givenAt": "2026-11-19T10:00:00Z",
            "doneAt": None,
        },
    ]
    api.uploader = {"e_4": ME, "e_1": ME, "e_0": OWNER}
    api.source_unknown = {"e_3"}
    api.staged = {(OTHER, "e_4")}
    api.machine_seen = {(OTHER, "e_0")}
    api.agent_write = True
