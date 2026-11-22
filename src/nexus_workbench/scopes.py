"""Token scopes (contract section 7.1) and the scope each agent API action needs."""

from __future__ import annotations

from collections.abc import Iterable

READ = "read"
DETAIL = "detail"
MARKS_WRITE = "marks:write"
TASKS_WRITE = "tasks:write"
ANALYSIS_RUN = "analysis:run"

#: Every scope, in the contract's order.
ALL: tuple[str, ...] = (READ, DETAIL, MARKS_WRITE, TASKS_WRITE, ANALYSIS_RUN)
#: Scopes that let a token change something in Workbench. Each needs ``agentWrite``.
WRITE: frozenset[str] = frozenset({MARKS_WRITE, TASKS_WRITE, ANALYSIS_RUN})

#: The scope each action needs. Actions not listed need ``read``.
ACTION_SCOPE: dict[str, str] = {
    "get_events": DETAIL,
    "get_stages": DETAIL,
    "get_quality": DETAIL,
    "list_marks": DETAIL,
    "propose_marks": MARKS_WRITE,
    "withdraw_marks": MARKS_WRITE,
    "create_review_task": TASKS_WRITE,
    "request_analysis": ANALYSIS_RUN,
}

#: Actions added in version 1.1. A version 1 server answers them with "Unknown action".
V11_ACTIONS: frozenset[str] = frozenset(
    {"list_members", "list_tasks", "list_nights", "list_layers", *ACTION_SCOPE}
)

#: What a token without the scope cannot do, as the start of a sentence.
CANNOT: dict[str, str] = {
    READ: "This token cannot read the workspace.",
    DETAIL: "This token cannot read per-epoch and per-event detail.",
    MARKS_WRITE: "This token cannot propose marks.",
    TASKS_WRITE: "This token cannot give review tasks.",
    ANALYSIS_RUN: "This token cannot ask for an analysis.",
}

#: What each scope lets a token do, for sign-in output and the docs.
MEANING: dict[str, str] = {
    READ: "read projects, recordings, nights, layers, tasks and members",
    DETAIL: "read per-epoch stages and quality, per-event events and marks",
    MARKS_WRITE: "propose marks as a review layer, and withdraw them",
    TASKS_WRITE: "give review tasks to members",
    ANALYSIS_RUN: "ask for an analysis of a recording you uploaded",
}


def needed(action: str) -> str:
    """The scope ``action`` needs."""
    return ACTION_SCOPE.get(action, READ)


def normalise(scopes: Iterable[str] | None) -> tuple[str, ...]:
    """Known scope names in the contract's order, with ``read`` always included.

    Unknown names are dropped: a server may add scopes later.
    """
    given = {s for s in scopes or () if isinstance(s, str)}
    given.add(READ)
    return tuple(s for s in ALL if s in given)


def unknown(scopes: Iterable[str]) -> list[str]:
    """The names in ``scopes`` that are not scopes."""
    return [s for s in scopes if s not in ALL]


def login_command(scopes: Iterable[str]) -> str:
    """The ``login`` command that asks for ``scopes`` (``read`` need not be named)."""
    parts = ["nexus-workbench login"]
    parts += [f"--scope {s}" for s in normalise(scopes) if s != READ]
    return " ".join(parts)


def describe(scopes: Iterable[str]) -> str:
    """Scopes as one comma-separated line, in order."""
    return ", ".join(normalise(scopes))
