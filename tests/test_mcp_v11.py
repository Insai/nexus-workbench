"""The version 1.1 tools through an in-process MCP client, against the fake API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from nexus_workbench import auth, mcp_errors, mcp_server
from nexus_workbench import client as c
from nexus_workbench import scopes as sc
from nexus_workbench.local.sample import make_sample_night
from tests.fake_api import API_BASE, ME, OTHER, FakeAPI, sample_data_v11
from tests.test_mcp_server import (
    BASE_TOOLS,
    DETAIL_TOOLS,
    READ_TOOLS,
    WRITE_TOOLS,
    server_for,
    text_of,
)

ALL = list(sc.ALL)
MARK = {"kind": "arousal", "t0": 10.0, "t1": 14.0, "channels": [1], "text": "EMG rise, 4 s"}


@pytest.fixture
def api() -> FakeAPI:
    fake = FakeAPI()
    sample_data_v11(fake)
    return fake


def run(coro_fn: Any) -> Any:
    return anyio.run(coro_fn)


def calls(server: Any, *pairs: tuple[str, dict[str, Any]]) -> list[Any]:
    async def go() -> list[Any]:
        async with Client(server, mode="legacy") as client:
            return [await client.call_tool(name, args) for name, args in pairs]

    return run(go)


def listing(server: Any) -> tuple[dict[str, Any], str]:
    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            return await client.list_tools(), client.instructions

    tools, instructions = run(go)
    return {t.name: t for t in tools.tools}, instructions


# -- listing by scope ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scopes", "expected"),
    [
        (None, BASE_TOOLS),
        (["read"], BASE_TOOLS),
        (["read", "detail"], BASE_TOOLS | DETAIL_TOOLS),
        (["read", "marks:write"], BASE_TOOLS | {"propose_marks", "withdraw_marks"}),
        (["read", "tasks:write"], BASE_TOOLS | {"create_review_task"}),
        (["read", "analysis:run"], BASE_TOOLS | {"request_analysis"}),
        (ALL, BASE_TOOLS | DETAIL_TOOLS | WRITE_TOOLS),
    ],
)
def test_tools_listed_by_scope(api: FakeAPI, scopes: Any, expected: set[str]) -> None:
    tools, instructions = listing(server_for(api, api.add_token(), scopes=scopes))
    assert set(tools) == expected
    if expected & WRITE_TOOLS:
        assert "review layer" in instructions
        assert "use them only when the researcher asks" in instructions
    else:
        assert "No write tools are listed" in instructions
    assert instructions.endswith(mcp_server.RESEARCH_USE)


def test_listing_reads_the_saved_scopes_and_makes_no_call(api: FakeAPI) -> None:
    token = api.add_token(scopes=ALL)
    auth.save_token(c.PRODUCTION_API_BASE, token, scopes=["read", "detail"])
    tools, _ = listing(mcp_server.build_server())
    assert set(tools) == BASE_TOOLS | DETAIL_TOOLS
    assert api.requests == []


def test_env_token_scopes_are_unknown_so_read_tools_only(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth.save_token(c.PRODUCTION_API_BASE, api.add_token(), scopes=ALL)
    monkeypatch.setenv("NEXUS_TOKEN", api.add_token(scopes=ALL))
    tools, _ = listing(mcp_server.build_server())
    assert set(tools) == BASE_TOOLS


def test_signed_out_lists_the_workspace_read_tools(api: FakeAPI) -> None:
    server = mcp_server.build_server(lambda: c.WorkbenchClient(api_base=API_BASE, token=None))
    tools, _ = listing(server)
    assert set(tools) == BASE_TOOLS
    (result,) = calls(server, ("list_nights", {}))
    assert result.is_error is True
    assert "nexus-workbench login" in text_of(result)


def test_annotations_of_the_new_tools(api: FakeAPI) -> None:
    tools, _ = listing(server_for(api, api.add_token(), scopes=ALL))
    for name in READ_TOOLS | DETAIL_TOOLS:
        ann = tools[name].annotations
        assert (ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint) == (
            True,
            False,
            True,
        ), name
        assert ann.open_world_hint is True
    expected = {
        "propose_marks": (False, False, False),
        "withdraw_marks": (False, True, True),
        "create_review_task": (False, False, False),
        "request_analysis": (False, False, True),
    }
    for name, hints in expected.items():
        ann = tools[name].annotations
        assert (ann.read_only_hint, ann.destructive_hint, ann.idempotent_hint) == hints, name
        assert ann.open_world_hint is True
        assert tools[name].title.startswith("Workbench: ")
        assert tools[name].output_schema["type"] == "object"
        assert mcp_server.RESULTS_ARE_DATA in tools[name].description
    for name in DETAIL_TOOLS:
        assert "detail scope" in tools[name].description
    assert tools["propose_marks"].input_schema["properties"]["marks"]["maxItems"] == 500
    assert tools["get_stages"].input_schema["properties"]["source"]["enum"] == ["machine", "mine"]
    assert set(tools["create_review_task"].input_schema["required"]) == {
        "ids",
        "member_id",
        "mode",
    }


# -- each read tool --------------------------------------------------------------------------


def test_each_read_and_detail_tool(api: FakeAPI) -> None:
    server = server_for(api, api.add_token(scopes=ALL), scopes=ALL)
    out = dict(
        zip(
            [
                "members",
                "tasks",
                "tasks_all",
                "nights",
                "layers",
                "events",
                "events_held",
                "stages",
                "stages_mine",
                "stages_held",
                "quality",
                "marks",
                "marks_desk",
                "whoami",
            ],
            calls(
                server,
                ("list_members", {}),
                ("list_tasks", {}),
                ("list_tasks", {"mine": False}),
                ("list_nights", {"project_id": "p_1"}),
                ("list_layers", {}),
                ("get_events", {"id": "e_1", "limit": 2}),
                ("get_events", {"id": "e_2"}),
                ("get_stages", {"id": "e_1"}),
                ("get_stages", {"id": "e_1", "source": "mine"}),
                ("get_stages", {"id": "e_2"}),
                ("get_quality", {"id": "e_1"}),
                ("list_marks", {"id": "e_1"}),
                ("list_marks", {"id": "e_3"}),
                ("whoami", {}),
            ),
            strict=True,
        )
    )
    for name, result in out.items():
        assert result.is_error is False, (name, text_of(result))
        assert json.loads(text_of(result)) == result.structured_content
    sc_ = {k: v.structured_content for k, v in out.items()}
    assert sc_["whoami"]["memberId"] == ME
    assert [m for m in sc_["members"]["members"] if m.get("you")][0]["id"] == ME
    assert len(sc_["tasks"]["tasks"]) == 1 and len(sc_["tasks_all"]["tasks"]) == 2
    assert sc_["nights"]["nights"][0]["personKey"] == "pk_0123456789abcdef"
    assert sc_["layers"]["layers"][0]["source"] == "analysis:ladder-3"
    assert sc_["events"]["events"][0] == {
        "onsetSec": 300.0,
        "durationSec": 1.0,
        "kind": "spindle",
        "channel": 1,
        "confidence": 0.8,
    }
    assert sc_["events"]["nextCursor"]
    assert sc_["events_held"] == {"hidden": "blind"}
    assert sc_["stages"]["nEpochs"] == 30 and len(sc_["stages"]["stages"]) > 30
    assert sc_["stages_mine"]["noSignal"] == [29]
    assert sc_["stages_held"] == {"source": "machine", "hidden": "blind"}
    assert sc_["quality"]["channels"][1]["excluded"] == "flat"
    assert [m["origin"] for m in sc_["marks"]["marks"]] == ["human", "human", "machine"]
    assert sc_["marks_desk"]["hidden"] == "blind"


def test_scope_refusal_through_a_tool_names_the_login_command(api: FakeAPI) -> None:
    # Listed (the server was told it has the scope), but the token itself lacks it.
    server = server_for(api, api.add_token(scopes=["read"]), scopes=ALL)
    (result,) = calls(server, ("get_events", {"id": "e_1"}))
    assert result.is_error is True
    assert text_of(result) == (
        "This token cannot read per-epoch and per-event detail. "
        "Sign in again with `nexus-workbench login --scope detail`, "
        "then restart the agent session. " + mcp_errors.RUN_IT_YOURSELF
    )
    error = result.structured_content
    assert error["code"] == "scope_required"
    assert error["howToFix"] == ["nexus-workbench login --scope detail", mcp_errors.RESTART]
    assert error["localAlternative"] == []


@pytest.mark.parametrize(
    ("tool", "args", "scope"),
    [
        ("propose_marks", {"id": "e_1", "marks": [MARK]}, "marks:write"),
        ("withdraw_marks", {"id": "e_1"}, "marks:write"),
        (
            "create_review_task",
            {"ids": ["e_1"], "member_id": OTHER, "mode": "blind"},
            "tasks:write",
        ),
    ],
)
def test_write_scope_refusal_names_the_command_and_the_restart(
    api: FakeAPI, tool: str, args: dict[str, Any], scope: str
) -> None:
    server = server_for(api, api.add_token(scopes=["read", "detail"]), scopes=ALL)
    (result,) = calls(server, (tool, args))
    assert result.is_error is True
    text = text_of(result)
    assert f"`nexus-workbench login --scope {scope}`" in text
    assert "then restart the agent session." in text
    assert result.structured_content["howToFix"][0] == f"nexus-workbench login --scope {scope}"
    description = listing(server)[0][tool].description or ""
    assert f"`nexus-workbench login --scope {scope}`" in description
    assert "then restart the agent session" in description


def test_paged_tools_say_how_to_page_and_null_cursor_is_the_first_page(api: FakeAPI) -> None:
    server = server_for(api, api.add_token(scopes=ALL), scopes=ALL)
    tools = listing(server)[0]
    for name in ("list_recordings", "get_events", "list_marks"):
        assert mcp_server.PAGING in (tools[name].description or ""), name
        cursor = tools[name].input_schema["properties"]["cursor"]
        assert "Omit `cursor` for the first page" in cursor["description"], name
    events = tools["get_events"].description or ""
    assert events.startswith("kinds is the set of event kinds this recording's analysis detects")
    assert "arousal" in events and "zero count" in events
    assert "qualityGrade and qualityScore" in (tools["list_recordings"].description or "")
    before = len(api.requests)
    (first,) = calls(server, ("list_recordings", {"limit": 2, "cursor": None}))
    assert first.is_error is not True
    sent = api.requests[before:]
    assert len(sent) == 1 and b"cursor" not in sent[0].content


@pytest.mark.parametrize("blank", ["", "null", " "])
def test_client_treats_a_blank_or_null_cursor_as_the_first_page(api: FakeAPI, blank: str) -> None:
    token = api.add_token(scopes=ALL)
    with c.WorkbenchClient(api_base=API_BASE, token=token, transport=api.transport) as client:
        client.call("list_recordings", limit=2, cursor=blank)
    assert b"cursor" not in api.requests[-1].content


def test_allowlist_and_caps_on_what_the_server_sends(api: FakeAPI) -> None:
    class Chatty(c.WorkbenchClient):
        def call(self, action: str, **args: Any) -> dict[str, Any]:
            if action == "list_marks":
                return {
                    "marks": [
                        {"id": "h", "origin": "human", "t0": 1.0, "text": "secret", "label": "x"},
                        {"id": "a", "origin": "agent", "t0": 2.0, "author": ME, "text": "why"},
                    ],
                    "nextCursor": None,
                    "serverOnly": 1,
                }
            if action == "list_tasks":
                task = {
                    "recordingId": "e_1",
                    "assignee": ME,
                    "givenBy": OTHER,
                    "mode": "blind",
                    "state": "open",
                    "agreement": 0.9,
                }
                return {"tasks": [{"id": f"task_{i:016x}", **task} for i in range(700)]}
            if action == "list_layers":
                recs = [{"id": f"e_{i}", "state": "have"} for i in range(1500)]
                layer = {"source": "s", "origin": "analysis", "recordings": recs}
                return {"layers": [layer], "truncated": False}
            if action == "get_stages":
                return {"source": "machine", "available": True, "stages": "WWXX"}
            raise AssertionError(action)

    server = mcp_server.build_server(
        lambda: Chatty(api_base=API_BASE, token=api.add_token()), scopes=ALL
    )
    marks, tasks, layers, stages = calls(
        server,
        ("list_marks", {"id": "e_1"}),
        ("list_tasks", {}),
        ("list_layers", {}),
        ("get_stages", {"id": "e_1"}),
    )
    human, agent = marks.structured_content["marks"]
    assert human == {"id": "h", "origin": "human", "t0": 1.0}  # no text or label on human rows
    assert agent == {"id": "a", "origin": "agent", "t0": 2.0, "text": "why"}  # no author
    assert "serverOnly" not in text_of(marks)
    assert len(tasks.structured_content["tasks"]) == 500
    assert "agreement" not in text_of(tasks)
    layer = layers.structured_content["layers"][0]
    assert len(layer["recordings"]) == 1000 and layer["truncated"] is True
    assert stages.is_error is True
    assert text_of(stages) == mcp_server.SERVER_NOT_UNDERSTOOD


# -- write tools -----------------------------------------------------------------------------


def test_propose_and_withdraw_marks(api: FakeAPI) -> None:
    token = api.add_token(scopes=ALL)
    tid = auth.token_id(token)
    server = server_for(api, token, scopes=ALL)
    first, again, listed, withdrawn = calls(
        server,
        ("propose_marks", {"id": "e_1", "marks": [MARK]}),
        ("propose_marks", {"id": "e_1", "marks": [MARK]}),
        ("list_marks", {"id": "e_1", "origin": "agent"}),
        ("withdraw_marks", {"id": "e_1"}),
    )
    assert first.structured_content == {
        "source": f"agent:{tid}",
        "accepted": 1,
        "ids": [f"agent-{tid}-1"],
        "total": 1,
    }
    assert again.structured_content == first.structured_content  # the same automatic opId
    assert len([a for a in api.audit if a["action"] == "propose_marks"]) == 1
    assert listed.structured_content["marks"][0]["text"] == "EMG rise, 4 s"
    assert withdrawn.structured_content == {"withdrawn": 1, "total": 0}
    sent = [json.loads(r.content) for r in api.requests]
    assert sent[0]["opId"].startswith("auto-") and sent[0]["opId"] == sent[1]["opId"]


def test_propose_marks_checks_locally_first(api: FakeAPI) -> None:
    server = server_for(api, api.add_token(scopes=ALL), scopes=ALL)
    bad, held, too_many = calls(
        server,
        ("propose_marks", {"id": "e_1", "marks": [MARK, {"kind": "note", "t0": 1, "t1": 2}]}),
        ("propose_marks", {"id": "e_2", "marks": [MARK]}),
        ("propose_marks", {"id": "e_1", "marks": [MARK] * 501}),
    )
    assert bad.is_error is True
    assert text_of(bad).startswith("Mark 2: kind must be one of bad_signal")
    assert text_of(bad).endswith("Nothing was written.")
    assert held.is_error is True and "held blind" in text_of(held)
    assert too_many.is_error is True and text_of(too_many).startswith("marks must be a list")
    assert api.agent_rows == {}
    assert len(api.requests) == 1  # only the held call reached the server


def test_write_switch_off_message(api: FakeAPI) -> None:
    api.agent_write = False
    server = server_for(api, api.add_token(scopes=ALL), scopes=ALL)
    (result,) = calls(server, ("request_analysis", {"id": "e_4"}))
    assert result.is_error is True
    assert text_of(result).startswith("This workspace does not take agent writes yet.")


def test_create_review_task_and_request_analysis(api: FakeAPI) -> None:
    server = server_for(api, api.add_token(scopes=ALL), scopes=ALL)
    given, dup, later, done = calls(
        server,
        ("create_review_task", {"ids": ["e_2", "e_0"], "member_id": OTHER, "mode": "blind"}),
        ("create_review_task", {"ids": ["e_2", "e_2"], "member_id": OTHER, "mode": "blind"}),
        ("request_analysis", {"id": "e_4"}),
        ("request_analysis", {"id": "e_1"}),
    )
    assert given.structured_content["given"] == ["e_2"]
    assert given.structured_content["refused"] == [{"id": "e_0", "code": "machine_seen"}]
    assert given.structured_content["mailed"] is True
    assert dup.is_error is True and "twice" in text_of(dup)
    assert later.is_error is True
    assert text_of(later) == (
        "Starting an analysis from an agent is not available yet. "
        "Use Analyse on the recording page."
    )
    assert done.is_error is True
    assert text_of(done).startswith("This recording already has machine output")
    api.analysis_available = True
    started, again = calls(
        server, ("request_analysis", {"id": "e_4"}), ("request_analysis", {"id": "e_4"})
    )
    assert started.structured_content == {"status": "started"}
    assert again.structured_content == {"status": "already"}


# -- bridges: workspace sides for the local comparisons ----------------------------------------


@pytest.fixture
def night(tmp_path: Path) -> dict[str, Any]:
    return make_sample_night(str(tmp_path / "night"), hours=0.5, seed=1)


def test_compare_hypnograms_with_the_workspace(api: FakeAPI, tmp_path: Path) -> None:
    hyp = tmp_path / "mine.txt"
    hyp.write_text("\n".join(["W"] * 4 + ["N2"] * 12 + ["N3"] * 6 + ["R"] * 4 + ["W"] * 4))
    server = server_for(api, api.add_token(scopes=ALL), scopes=ALL)
    machine, mine, held, mixed, neither, epoch = calls(
        server,
        ("compare_hypnograms", {"path_a": str(hyp), "recording_id": "e_1"}),
        ("compare_hypnograms", {"path_b": str(hyp), "recording_id": "e_1", "source": "mine"}),
        ("compare_hypnograms", {"path_a": str(hyp), "recording_id": "e_2"}),
        ("compare_hypnograms", {"path_a": str(hyp), "path_b": str(hyp), "recording_id": "e_1"}),
        ("compare_hypnograms", {"path_a": str(hyp)}),
        ("compare_hypnograms", {"path_a": str(hyp), "recording_id": "e_1", "epoch_sec": 20}),
    )
    result = machine.structured_content
    assert machine.is_error is False, text_of(machine)
    assert result["fileIdB"] == "workspace:e_1:machine"
    assert result["epochsTotal"] == 30 and result["epochsUnscored"] == 2
    assert mine.structured_content["fileIdA"] == "workspace:e_1:mine"
    assert held.is_error is True and "blinded on this recording" in text_of(held)
    assert mixed.is_error is True and "exactly one of path_a and path_b" in text_of(mixed)
    assert neither.is_error is True and text_of(neither).startswith("path_b is required")
    assert epoch.is_error is True and "epoch_sec=30" in text_of(epoch)


def test_compare_events_with_the_workspace(api: FakeAPI, tmp_path: Path) -> None:
    ref = tmp_path / "ref.csv"
    ref.write_text("onset_sec,duration_sec,label\n300.2,1.0,spindle\n900,1,spindle\n")
    server = server_for(api, api.add_token(scopes=ALL), scopes=ALL)
    spindles, everything, desk, no_detail = calls(
        server,
        (
            "compare_events",
            {"reference_path": str(ref), "recording_id": "e_1", "label": "spindle"},
        ),
        ("compare_events", {"reference_path": str(ref), "recording_id": "e_1", "n_shuffles": 0}),
        ("compare_events", {"candidate_path": str(ref), "recording_id": "e_3"}),
        ("compare_events", {"reference_path": str(ref), "candidate_path": str(ref)}),
    )
    got = spindles.structured_content
    assert spindles.is_error is False, text_of(spindles)
    assert got["fileIdCandidate"] == "workspace:e_1:machine"
    # Two spindle rows at 300 s (two channels) count once; 420.5 s is extra.
    assert got["eventsInFiles"]["candidate"] == 2
    assert (got["matched"], got["missed"], got["extra"]) == (1, 1, 1)
    assert everything.structured_content["eventsInFiles"]["candidate"] == 3
    assert desk.is_error is True and "blinded" in text_of(desk)
    assert no_detail.is_error is False  # two files need no sign-in
    gets = [json.loads(r.content) for r in api.requests if b"get_events" in r.content]
    assert gets[0]["kind"] == "spindle" and gets[0]["limit"] == 1000


def test_compare_with_the_workspace_needs_detail(api: FakeAPI, tmp_path: Path) -> None:
    hyp = tmp_path / "a.txt"
    hyp.write_text("W\n" * 30)
    server = server_for(api, api.add_token(scopes=["read"]), scopes=["read"])
    (result,) = calls(server, ("compare_hypnograms", {"path_a": str(hyp), "recording_id": "e_1"}))
    assert result.is_error is True
    assert "nexus-workbench login --scope detail" in text_of(result)


def test_preprocess_tool_writes_a_new_file(night: dict[str, Any]) -> None:
    server = mcp_server.build_server(
        lambda: c.WorkbenchClient(api_base=API_BASE, token=None), scopes=None
    )
    before = Path(night["edf"]).read_bytes()
    ok, again, bad = calls(
        server,
        ("preprocess", {"path": night["edf"], "low_hz": 0.5, "high_hz": 30}),
        ("preprocess", {"path": night["edf"], "low_hz": 0.5, "high_hz": 30}),
        ("preprocess", {"path": night["edf"], "notch_hz": 55}),
    )
    assert ok.is_error is False, text_of(ok)
    result = ok.structured_content
    assert result["output"] == "sample_night_preprocessed.edf"
    assert str(Path(night["edf"]).parent) not in text_of(ok)
    assert result["applied"] == ["bandpass"]
    assert Path(night["edf"]).read_bytes() == before
    assert again.is_error is True and "already exists" in text_of(again)
    assert bad.is_error is True and "notch_hz" in text_of(bad)
