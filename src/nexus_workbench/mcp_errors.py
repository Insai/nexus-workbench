"""Tool error results of the MCP server: a message for a person and a structured copy.

Every tool error carries, besides its text, a structured object an agent can act on::

    {"ok": false, "code": "not_signed_in", "reason": "Not signed in.",
     "howToFix": ["nexus-workbench login"], "localAlternative": ["inspect_edf", ...],
     "requestId": null, "retryAfter": null}

``code`` is a code of the contract (sections 3.1, 3.3 and 7.10). ``howToFix`` lists the
steps in order; a step that is a command is the exact command and nothing else, for the
researcher to run in their own terminal. ``localAlternative`` names the tools that still
work, signed out included, when the workspace cannot be read. ``requestId`` is the id the
server gave the request, when it sent one, and ``retryAfter`` the seconds it asked to wait.

No step ever asks for a token in the chat: signing in happens in the terminal.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from mcp.types import CallToolResult, TextContent

from .client import ENV_API_URL, ScopeRequired, WorkbenchError

LOGIN = "nexus-workbench login"
DOCTOR = "nexus-workbench doctor"
UPGRADE = "uv tool upgrade nexus-workbench"
RESTART = "Restart the agent session so the tools of the new scopes are listed."
REQUEST_ACCESS = (
    "In Workbench, open the account menu, choose Connect an agent, then Request agent access."
)

#: The tools that need no sign-in and send nothing anywhere (``compare_hypnograms`` and
#: ``compare_events`` read Workbench only when given a ``recording_id``).
LOCAL_TOOLS: tuple[str, ...] = (
    "inspect_edf",
    "check_signal_quality",
    "compute_band_power",
    "compare_hypnograms",
    "compare_events",
    "preprocess",
)

#: The steps for each code that has fixed ones. Other codes use the error's own hint.
HOW_TO_FIX: dict[str, tuple[str, ...]] = {
    "not_signed_in": (LOGIN,),
    "unauthenticated": (LOGIN,),
    "token_expired": (LOGIN,),
    "early_access_required": (REQUEST_ACCESS,),
    "forbidden": ("Ask the workspace owner whether you are still a member.", LOGIN),
    "write_access_required": (
        "Ask Insai to switch on agent writes for this workspace. Reads still work.",
    ),
    "network": ("Check the internet connection, then try again.", DOCTOR),
    "config": (f"Fix or unset {ENV_API_URL}; it must start with https://.",),
    "unexpected_response": (UPGRADE,),
    "internal": ("Try again in a few minutes.", DOCTOR),
    "not_found": ("Check the id with list_projects or list_recordings.",),
    "invalid_argument": ("Check the arguments against the tool's input schema.",),
}

#: Codes after which the local tools are the useful thing left to offer.
OFFER_LOCAL: frozenset[str] = frozenset(
    {
        "not_signed_in",
        "unauthenticated",
        "token_expired",
        "early_access_required",
        "forbidden",
        "network",
        "config",
    }
)

#: Codes whose fix is a sign-in in the terminal.
SIGN_IN_CODES: frozenset[str] = frozenset(
    {"not_signed_in", "unauthenticated", "token_expired", "forbidden", "scope_required"}
)

#: What an agent is told when the fix is a sign-in, in the text copy.
RUN_IT_YOURSELF = (
    "The researcher runs any command in their own terminal; never ask for a token in the chat."
)


def payload(
    code: str,
    reason: str,
    *,
    how_to_fix: Sequence[str] = (),
    local_alternative: Sequence[str] | None = None,
    request_id: str | None = None,
    retry_after: float | None = None,
) -> dict[str, Any]:
    """The structured copy of a tool error."""
    alternative = LOCAL_TOOLS if local_alternative is None and code in OFFER_LOCAL else ()
    return {
        "ok": False,
        "code": code,
        "reason": reason,
        "howToFix": list(how_to_fix),
        "localAlternative": list(local_alternative or alternative),
        "requestId": request_id,
        "retryAfter": retry_after,
    }


def result(message: str, data: dict[str, Any]) -> CallToolResult:
    """A tool error result: the message for a person first, then the structured copy as
    compact JSON (for clients that show only text), and the same object as structured
    content."""
    text = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    return CallToolResult(
        content=[TextContent(type="text", text=message), TextContent(type="text", text=text)],
        structured_content=data,
        is_error=True,
    )


def tool_error(
    message: str,
    code: str,
    *,
    how_to_fix: Sequence[str] | None = None,
    local_alternative: Sequence[str] | None = None,
) -> CallToolResult:
    """A tool error the tools raise themselves (an argument, a local file, a bad mix)."""
    steps = HOW_TO_FIX.get(code, ()) if how_to_fix is None else how_to_fix
    return result(
        message,
        payload(code, message, how_to_fix=steps, local_alternative=local_alternative),
    )


def steps_for(exc: WorkbenchError) -> list[str]:
    """The steps that fix ``exc``, commands exact."""
    if isinstance(exc, ScopeRequired):
        login = exc.login or f"{LOGIN} --scope <scope>"
        return [login, RESTART]
    if exc.code == "rate_limited":
        wait = int(exc.retry_after) if exc.retry_after is not None else 60
        return [f"Wait {wait} seconds, then try again."]
    if exc.code in HOW_TO_FIX:
        return list(HOW_TO_FIX[exc.code])
    return [exc.hint] if exc.hint else []


def from_exception(exc: WorkbenchError, message: str | None = None) -> CallToolResult:
    """A tool error for a client or server error. ``message`` replaces the text copy."""
    text = message or str(exc)
    if exc.code in SIGN_IN_CODES:
        text = f"{text} {RUN_IT_YOURSELF}"
    data = payload(
        exc.code,
        exc.message,
        how_to_fix=steps_for(exc),
        request_id=exc.request_id,
        retry_after=exc.retry_after,
    )
    return result(text, data)
