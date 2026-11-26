"""Token scopes (contract section 7.1) and the scope each agent API action needs."""

from __future__ import annotations

from collections.abc import Iterable

READ = "read"
DETAIL = "detail"
MARKS_WRITE = "marks:write"
TASKS_WRITE = "tasks:write"
ANALYSIS_RUN = "analysis:run"
CHANNELS_WRITE = "channels:write"

#: Every scope, in the contract's order.
ALL: tuple[str, ...] = (READ, DETAIL, MARKS_WRITE, TASKS_WRITE, ANALYSIS_RUN, CHANNELS_WRITE)
#: Scopes that let a token change something in Workbench. Each needs ``agentWrite``.
WRITE: frozenset[str] = frozenset({MARKS_WRITE, TASKS_WRITE, ANALYSIS_RUN, CHANNELS_WRITE})

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
    "set_channels": CHANNELS_WRITE,
}

#: Actions added in version 1.1. A version 1 server answers them with "Unknown action".
#: ``set_channels`` (contract 7.12) is not one: a server without channel sites says so itself.
V11_ACTIONS: frozenset[str] = frozenset(
    {"list_members", "list_tasks", "list_nights", "list_layers", *ACTION_SCOPE} - {"set_channels"}
)

#: What a token without the scope cannot do, as the start of a sentence.
CANNOT: dict[str, str] = {
    READ: "This token cannot read the workspace.",
    DETAIL: "This token cannot read per-epoch and per-event detail.",
    MARKS_WRITE: "This token cannot propose or withdraw marks.",
    TASKS_WRITE: "This token cannot give review tasks.",
    ANALYSIS_RUN: "This token cannot start an analysis.",
    CHANNELS_WRITE: "This token cannot set channel sites.",
}

#: The same sentence for one action, where the action says more than its scope.
ACTION_CANNOT: dict[str, str] = {
    "propose_marks": "This token cannot propose marks.",
    "withdraw_marks": "This token cannot withdraw marks.",
    "create_review_task": "This token cannot give review tasks.",
    "request_analysis": "This token cannot start an analysis.",
}


def cannot(scope: str, action: str | None = None) -> str:
    """What a token without ``scope`` cannot do, named for ``action`` when it is known."""
    if action is not None and ACTION_CANNOT.get(action) and needed(action) == scope:
        return ACTION_CANNOT[action]
    return CANNOT[scope]


#: What each scope lets a token do, for sign-in output and the docs.
MEANING: dict[str, str] = {
    READ: "read projects, recordings, nights, layers, tasks and members",
    DETAIL: "read per-epoch stages and quality, per-event events and marks",
    MARKS_WRITE: "propose marks as a review layer, and withdraw them",
    TASKS_WRITE: "give review tasks to members",
    ANALYSIS_RUN: "ask for an analysis of a recording you uploaded",
    CHANNELS_WRITE: "set the electrode sites of a recording's channels",
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
