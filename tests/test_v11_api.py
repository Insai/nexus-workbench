"""Version 1.1 (contract section 7) through the client, against the fake API.

These pin the fake to the contract (scopes, switches, order of checks, blinding, caps, the
review layer, idempotent opId, audit) and the client's errors to it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from nexus_workbench import auth
from nexus_workbench import client as c
from nexus_workbench import scopes as sc
from tests.fake_api import ME, OTHER, OWNER, SERVICE, FakeAPI, Refused, sample_data_v11

ALL = list(sc.ALL)


@pytest.fixture
def api() -> FakeAPI:
    fake = FakeAPI()
    sample_data_v11(fake)
    return fake


def make(api: FakeAPI, scopes: list[str] | None = None, known: bool = False, **state: Any):
    token = api.add_token(scopes=scopes or ["read"], **state)
    return c.WorkbenchClient(
        api_base="https://api.test.invalid",
        token=token,
        scopes=(scopes or ["read"]) if known else None,
        transport=api.transport,
        sleep=lambda s: None,
    ), token


def bodies(api: FakeAPI) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in api.requests]


MARK = {"kind": "arousal", "t0": 10.0, "t1": 14.0, "channels": [1], "text": "EMG rise, 4 s"}


# -- scopes and switches (7.1, 7.2, 7.3) ------------------------------------------------------


def test_whoami_reports_scopes_and_member_id(api: FakeAPI) -> None:
    client, _ = make(api, ["read", "detail"])
    data = client.call("whoami")
    assert data["scopes"] == ["read", "detail"]
    assert data["memberId"] == ME


def test_scope_required_names_the_scope_and_the_login_command(api: FakeAPI) -> None:
    client, _ = make(api, ["read"])
    with pytest.raises(c.ScopeRequired) as info:
        client.get_stages("e_1")
    err = info.value
    assert err.code == "scope_required" and err.status == 403
    assert err.scope == "detail"
    assert str(err) == (
        "This token cannot read per-epoch and per-event detail. "
        "Sign in again with `nexus-workbench login --scope detail`."
    )


def test_scope_required_is_local_when_the_scopes_are_known(api: FakeAPI) -> None:
    client, _ = make(api, ["read", "detail"], known=True)
    with pytest.raises(c.ScopeRequired) as info:
        client.propose_marks("e_1", [MARK])
    assert api.requests == []  # refused before any call
    assert info.value.status is None
    assert str(info.value) == (
        "This token cannot propose marks. "
        "Sign in again with `nexus-workbench login --scope detail --scope marks:write`."
    )


@pytest.mark.parametrize(
    ("call", "sentence", "scope"),
    [
        (lambda cl: cl.get_events("e_1"), "read per-epoch and per-event detail", "detail"),
        (lambda cl: cl.propose_marks("e_1", [MARK]), "propose marks", "marks:write"),
        (lambda cl: cl.withdraw_marks("e_1"), "withdraw marks", "marks:write"),
        (
            lambda cl: cl.create_review_task(["e_1"], member_id=OTHER, mode="blind"),
            "give review tasks",
            "tasks:write",
        ),
        (lambda cl: cl.request_analysis("e_4"), "start an analysis", "analysis:run"),
    ],
)
@pytest.mark.parametrize("known", [False, True])
def test_scope_required_names_the_action(
    api: FakeAPI, call: Any, sentence: str, scope: str, known: bool
) -> None:
    client, _ = make(api, ["read"], known=known)
    with pytest.raises(c.ScopeRequired) as info:
        call(client)
    assert str(info.value) == (
        f"This token cannot {sentence}. Sign in again with `nexus-workbench login --scope {scope}`."
    )


@pytest.mark.parametrize(
    ("call", "scope"),
    [
        (lambda cl: cl.get_events("e_1"), "detail"),
        (lambda cl: cl.get_quality("e_1"), "detail"),
        (lambda cl: cl.list_marks("e_1"), "detail"),
        (lambda cl: cl.withdraw_marks("e_1"), "marks:write"),
        (lambda cl: cl.create_review_task(["e_1"], member_id=OTHER, mode="blind"), "tasks:write"),
        (lambda cl: cl.request_analysis("e_4"), "analysis:run"),
    ],
)
def test_every_scoped_action_needs_its_scope(api: FakeAPI, call: Any, scope: str) -> None:
    client, _ = make(api, ["read"])
    with pytest.raises(c.ScopeRequired) as info:
        call(client)
    assert info.value.scope == scope


def test_read_actions_need_only_read(api: FakeAPI) -> None:
    client, _ = make(api, ["read"])
    assert client.list_members()["members"]
    assert "tasks" in client.list_tasks()
    assert "nights" in client.list_nights()
    assert "layers" in client.list_layers()


def test_writes_need_agent_write(api: FakeAPI) -> None:
    api.agent_write = False
    client, _ = make(api, ALL)
    with pytest.raises(c.WriteAccessRequired) as info:
        client.propose_marks("e_1", [MARK])
    assert info.value.status == 403
    assert "does not take agent writes yet" in str(info.value)
    assert "Reads still work" in str(info.value)
    assert client.get_stages("e_1")["available"] is True  # detail reads need only agentAccess
    assert api.audit == [] and api.agent_rows == {}


def test_order_of_checks(api: FakeAPI) -> None:
    api.agent_write = False
    client, _ = make(api, ["read"], early_access=False)
    with pytest.raises(c.EarlyAccessRequired):  # agentAccess before scope
        client.propose_marks("e_1", [MARK])
    client, _ = make(api, ["read"])
    with pytest.raises(c.ScopeRequired):  # scope before agentWrite
        client.propose_marks("e_1", [MARK])
    client, _ = make(api, ALL)
    with pytest.raises(c.WriteAccessRequired):  # agentWrite before the arguments
        client.propose_marks("e_nope", [{"kind": "nonsense"}])


def test_version_1_server_reads_as_not_available_yet(api: FakeAPI) -> None:
    api.v11 = False
    client, _ = make(api, ALL)
    with pytest.raises(c.NotOnServer) as info:
        client.list_nights()
    assert info.value.code == "invalid_argument"
    assert str(info.value) == (
        "This Workbench server does not offer list_nights yet. "
        "It is part of agent API version 1.1, which reaches staging first."
    )
    assert client.call("whoami")["workspace"]["id"] == "w_1"  # version 1 keeps working


# -- reads (7.6) ----------------------------------------------------------------------------


def test_list_members_has_no_names_and_marks_you(api: FakeAPI) -> None:
    client, _ = make(api)
    members = client.list_members()["members"]
    assert [m["id"] for m in members] == sorted(m["id"] for m in members)
    assert {m["id"] for m in members} == {ME, OWNER, OTHER}
    assert SERVICE not in {m["id"] for m in members}
    assert [m for m in members if m.get("you")] == [{"id": ME, "role": "member", "you": True}]
    assert all(set(m) <= {"id", "role", "you"} for m in members)


def test_list_tasks_mine_and_given(api: FakeAPI) -> None:
    client, _ = make(api)
    mine = client.list_tasks()["tasks"]
    assert [t["id"] for t in mine] == ["task_00000000000000a1"]
    both = client.list_tasks(mine=False)["tasks"]
    assert [t["id"] for t in both] == ["task_00000000000000a1", "task_00000000000000b2"]
    assert "task_00000000000000c3" not in {t["id"] for t in both}  # nobody else's tasks


def test_list_nights(api: FakeAPI) -> None:
    client, _ = make(api)
    nights = client.list_nights()["nights"]
    by_id = {n["id"]: n for n in nights}
    assert by_id["n_1"] == {
        "id": "n_1",
        "recordingIds": ["e_0", "e_1"],
        "projectId": "p_1",
        "personKey": "pk_0123456789abcdef",
    }
    assert "projectId" not in by_id["n_2"]  # e_3 is in no project
    assert "personKey" not in by_id["n_2"]
    assert [n["id"] for n in client.list_nights(project_id="p_1")["nights"]] == ["n_1"]
    with pytest.raises(c.NotFound):
        client.list_nights(project_id="p_nope")


def test_list_layers_hides_held_recordings_and_blind_desks(api: FakeAPI) -> None:
    client, _ = make(api)
    layer = client.list_layers()["layers"][0]
    assert layer["origin"] == "analysis" and layer["status"] == "stable"
    recs = {r["id"]: r for r in layer["recordings"]}
    assert recs["e_2"] == {"id": "e_2", "hidden": "blind"}  # held
    assert recs["e_3"] == {"id": "e_3", "hidden": "blind"}  # blind desk
    assert recs["e_1"]["state"] == "have" and recs["e_1"]["counts"] == {"spindle": 1}
    assert recs["e_0"] == {"id": "e_0", "state": "notRun"}
    assert recs["e_4"] == {"id": "e_4", "state": "missing"}


def test_get_events_pages_and_blinding(api: FakeAPI) -> None:
    client, _ = make(api, ["read", "detail"])
    first = client.get_events("e_1", limit=2)
    assert first["available"] is True
    assert first["kinds"] == ["arousal", "spindle"]
    assert len(first["events"]) == 2 and first["nextCursor"]
    rest = client.get_events("e_1", limit=2, cursor=first["nextCursor"])
    assert rest["nextCursor"] is None
    assert [e["onsetSec"] for e in first["events"] + rest["events"]] == [300, 300, 420.5, 600]
    with pytest.raises(c.InvalidArgument):  # a cursor is bound to the same arguments
        client.get_events("e_1", kind="spindle", limit=2, cursor=first["nextCursor"])
    tampered = first["nextCursor"].replace(".", "0.", 1)
    with pytest.raises(c.InvalidArgument) as info:  # a cursor is signed
        client.get_events("e_1", limit=2, cursor=tampered)
    assert "not a cursor of this list" in str(info.value)
    assert client.get_events("e_2") == {"hidden": "blind"}  # held
    assert client.get_events("e_3") == {"hidden": "blind"}  # blind desk
    assert client.get_events("e_4")["available"] is False
    assert "events" not in client.get_events("e_4")
    with pytest.raises(c.NotFound):
        client.get_events("e_nope")


def test_get_stages_machine_and_mine(api: FakeAPI) -> None:
    client, _ = make(api, ["read", "detail"])
    machine = client.get_stages("e_1", source="machine")
    assert machine["model"] == "usleep" and machine["nEpochs"] == 30
    mine = client.get_stages("e_1", source="mine")
    assert mine["status"] == "in_progress" and mine["questionable"] == [5]
    assert client.get_stages("e_2", source="machine") == {"source": "machine", "hidden": "blind"}
    assert client.get_stages("e_2", source="mine") == {"source": "mine", "available": False}
    with pytest.raises(c.InvalidArgument):
        client.get_stages("e_1", source="theirs")


def test_get_quality_is_not_blinded(api: FakeAPI) -> None:
    api.quality["e_2"] = dict(api.quality["e_1"])
    client, _ = make(api, ["read", "detail"])
    assert client.get_quality("e_2")["grade"] == "marginal"
    assert client.get_quality("e_4") == {"available": False}


def test_list_marks_blinding(api: FakeAPI) -> None:
    client, _ = make(api, ["read", "detail"])
    open_ = client.list_marks("e_1")
    assert [m["origin"] for m in open_["marks"]] == ["human", "human", "machine"]
    assert "hidden" not in open_
    assert all("text" not in m for m in open_["marks"] if m["origin"] == "human")
    assert all(m["kind"] != "note" for m in open_["marks"])
    held = client.list_marks("e_2")
    assert [m["id"] for m in held["marks"]] == ["h4"]  # own human rows only
    assert held["hidden"] == "blind"
    desk = client.list_marks("e_3")
    assert [m["id"] for m in desk["marks"]] == ["h6"]  # everyone's human rows, no machine
    assert desk["hidden"] == "blind"
    assert client.list_marks("e_3", origin="machine") == {
        "marks": [],
        "nextCursor": None,
        "hidden": "blind",
    }


# -- writes (7.7) ---------------------------------------------------------------------------


def test_propose_marks_is_a_review_layer(api: FakeAPI) -> None:
    client, token = make(api, ALL)
    tid = auth.token_id(token)
    result = client.propose_marks("e_1", [MARK, {**MARK, "t0": 20.0, "t1": 22.0}])
    assert result == {
        "source": f"agent:{tid}",
        "accepted": 2,
        "ids": [f"agent-{tid}-1", f"agent-{tid}-2"],
        "total": 2,
    }
    marks = client.list_marks("e_1", origin="agent")["marks"]
    assert [m["id"] for m in marks] == [f"agent-{tid}-1", f"agent-{tid}-2"]
    assert marks[0]["text"] == "EMG rise, 4 s" and marks[0]["target"] == "channel"
    assert "author" not in marks[0]
    assert marks[0]["verdicts"] == {"agree": 0, "disagree": 0}
    assert len(api.human_marks["e_1"]) == 3  # people's marks do not move
    layer = [x for x in client.list_layers()["layers"] if x["origin"] == "agent"][0]
    assert layer["status"] == "proposed"
    assert {r["id"]: r for r in layer["recordings"]}["e_1"]["counts"] == {"arousal": 2}
    assert api.audit == [
        {
            "action": "propose_marks",
            "by": ME,
            "via": "agent",
            "tokenId": tid,
            "recordingId": "e_1",
            "counts": {"accepted": 2},
        }
    ]
    assert "EMG" not in json.dumps(api.audit)  # never a mark's text


def test_propose_marks_is_all_or_nothing(api: FakeAPI) -> None:
    client, _ = make(api, ALL)
    with pytest.raises(c.InvalidArgument) as info:
        client.propose_marks("e_1", [MARK, {**MARK, "t1": 99_999.0}])
    assert "Mark 2" in str(info.value)
    with pytest.raises(c.InvalidArgument):
        client.propose_marks("e_1", [{**MARK, "kind": "note"}])
    with pytest.raises(c.InvalidArgument):
        client.propose_marks("e_1", [{**MARK, "text": "mail me at a@b.example"}])
    with pytest.raises(c.InvalidArgument):
        client.propose_marks("e_1", [{**MARK, "channels": [3]}])  # e_1 has 2 channels
    assert api.agent_rows == {} and api.audit == []


def test_propose_marks_refused_when_held_not_on_a_blind_desk(api: FakeAPI) -> None:
    client, _ = make(api, ALL)
    with pytest.raises(c.Forbidden) as info:
        client.propose_marks("e_2", [MARK])
    assert "held blind" in str(info.value)
    assert client.propose_marks("e_3", [MARK])["accepted"] == 1


def test_op_id_makes_a_retry_safe(api: FakeAPI) -> None:
    client, _ = make(api, ALL)
    first = client.propose_marks("e_1", [MARK], op_id="retry-1")
    again = client.propose_marks("e_1", [MARK], op_id="retry-1")
    assert again == first
    assert len(api.audit) == 1  # a repeated opId writes nothing and leaves no audit row
    assert sum(len(v) for v in api.agent_rows.values()) == 1
    with pytest.raises(c.InvalidArgument):
        client.propose_marks("e_1", [{**MARK, "t0": 1.0}], op_id="retry-1")


def test_mark_cap_counts_withdrawn_rows_and_numbers_are_not_reused(api: FakeAPI) -> None:
    client, token = make(api, ALL)
    tid = auth.token_id(token)
    batch = [{"kind": "spindle", "t0": float(i), "t1": float(i) + 1} for i in range(500)]
    for k in range(10):
        client.propose_marks("e_1", batch, op_id=f"b{k}")
    assert client.withdraw_marks("e_1") == {"withdrawn": 5000, "total": 0}
    with pytest.raises(c.MarkLimit) as info:
        client.propose_marks("e_1", [MARK])
    assert info.value.status == 409
    assert str(info.value).startswith("These marks would take this token past 5,000 marks")
    rows = api.agent_rows[(tid, "e_1")]
    assert len(rows) == 5000 and rows[-1].n == 5000


def test_propose_marks_body_may_be_larger(api: FakeAPI) -> None:
    client, _ = make(api, ALL)
    batch = [{**MARK, "t0": float(i), "t1": float(i) + 1} for i in range(500)]
    assert len(json.dumps(batch)) > 2048
    assert client.propose_marks("e_1", batch)["accepted"] == 500
    with pytest.raises(c.InvalidArgument) as info:
        client.call("list_marks", id="x" * 3000)
    assert "too large" in str(info.value)


def test_withdraw_marks(api: FakeAPI) -> None:
    client, token = make(api, ALL)
    tid = auth.token_id(token)
    client.propose_marks("e_1", [MARK, {**MARK, "t0": 30.0, "t1": 31.0}])
    one = f"agent-{tid}-1"
    assert client.withdraw_marks("e_1", mark_ids=[one]) == {"withdrawn": 1, "total": 1}
    audit_rows = len(api.audit)
    assert client.withdraw_marks("e_1", mark_ids=[one]) == {"withdrawn": 1, "total": 1}
    assert len(api.audit) == audit_rows  # already withdrawn: no audit row
    with pytest.raises(c.NotFound):
        client.withdraw_marks("e_1", mark_ids=["analysis-ladder-3-00017"])
    other, _ = make(api, ALL)
    with pytest.raises(c.NotFound):  # another token's rows
        other.withdraw_marks("e_1", mark_ids=[f"agent-{tid}-2"])
    assert [m["id"] for m in client.list_marks("e_1", origin="agent")["marks"]] == [
        f"agent-{tid}-2"
    ]


def test_create_review_task_refusals(api: FakeAPI) -> None:
    client, _ = make(api, ALL)
    result = client.create_review_task(
        ["e_0", "e_1", "e_3", "e_4", "e_nope", "e_2"], member_id=OTHER, mode="blind"
    )
    assert result["given"] == ["e_2"]
    assert result["batchId"]
    assert {r["id"]: r["code"] for r in result["refused"]} == {
        "e_0": "machine_seen",
        "e_1": "open_task",
        "e_3": "no_source",
        "e_4": "staged",
        "e_nope": "not_found",
    }
    confirmed = client.create_review_task(
        ["e_0"], member_id=OTHER, mode="blind", confirm_machine_seen=True
    )
    assert confirmed["given"] == ["e_0"]
    nothing = client.create_review_task(["e_1"], member_id=OTHER, mode="assisted")
    assert nothing == {
        "batchId": None,
        "given": [],
        "refused": [{"id": "e_1", "code": "open_task"}],
        "mailed": False,
    }
    assert client.create_review_task(["e_4"], member_id=OTHER, mode="assisted")["given"] == ["e_4"]
    assert api.tasks[-1]["givenBy"] == ME  # given by the token's owner, never the agent
    for bad in ({"member_id": SERVICE, "mode": "blind"}, {"member_id": "bob", "mode": "blind"}):
        with pytest.raises(c.InvalidArgument):
            client.create_review_task(["e_1"], **bad)
    with pytest.raises(c.NotFound):
        client.create_review_task(["e_1"], member_id="m_0000000000000099", mode="blind")


def test_request_analysis_is_checked_then_not_available(api: FakeAPI) -> None:
    client, _ = make(api, ALL)
    with pytest.raises(c.NotAvailable) as info:
        client.request_analysis("e_4")
    assert info.value.status == 501 and info.value.code == "not_available"
    assert str(info.value) == (
        "Starting an analysis from an agent is not available yet. "
        "Use Analyse on the recording page."
    )
    assert "e_4" not in api.pending_analysis and api.audit == []  # nothing started
    with pytest.raises(c.AlreadyAnalysed) as info:  # machine output is checked first
        client.request_analysis("e_1")
    assert info.value.status == 409
    with pytest.raises(c.AlreadyAnalysed):  # events alone count as machine output
        client.request_analysis("e_0")
    api.recordings[0]["hasEvents"] = False
    with pytest.raises(c.Forbidden) as info:
        client.request_analysis("e_0")  # uploaded by someone else
    assert "only the person who uploaded" in str(info.value)
    api.not_a_file.add("e_0")
    with pytest.raises(c.InvalidArgument):  # not a file is checked before the uploader
        client.request_analysis("e_0")
    api.pending_analysis.add("e_0")
    assert client.request_analysis("e_0") == {"status": "already"}  # before the uploader too
    with pytest.raises(c.NotFound):
        client.request_analysis("e_nope")


def test_request_analysis_on_a_server_that_starts_runs(api: FakeAPI) -> None:
    api.analysis_available = True
    client, _ = make(api, ALL)
    assert client.request_analysis("e_4") == {"status": "started"}
    assert client.request_analysis("e_4") == {"status": "already"}
    staff, _ = make(api, ALL, staff=True)
    api.recordings[0]["hasEvents"] = False
    api.analysis_service_down = True
    with pytest.raises(c.InternalError):
        staff.request_analysis("e_0")
    assert [a["recordingId"] for a in api.audit] == ["e_4"]


# -- what the 1.1 server does beyond the draft -------------------------------------------------


def test_list_layers_reads_the_newest_1000_recordings(api: FakeAPI) -> None:
    api.recordings = [
        {"id": f"r_{i:04d}", "projectId": None, "addedAt": f"2026-01-01T00:00:{i % 60:02d}Z"}
        | {"url": "https://workbench.test.invalid/r", "hasEvents": False}
        for i in range(1001)
    ]
    api.held, api.blind_desk = set(), set()
    layer = make(api)[0].list_layers()["layers"][0]
    assert len(layer["recordings"]) == 1000 and layer["truncated"] is True


def test_get_events_too_large(api: FakeAPI) -> None:
    api.too_large.add("e_1")
    client, _ = make(api, ["read", "detail"])
    with pytest.raises(c.TooLarge) as info:
        client.get_events("e_1")
    assert info.value.status == 409 and info.value.code == "too_large"
    assert str(info.value).endswith("Open the recording in Workbench instead.")


def test_list_marks_page_may_be_short_with_a_cursor(api: FakeAPI) -> None:
    api.marks_read = 2  # the server reads 5,000 stored rows a call; notes count toward it
    client, _ = make(api, ["read", "detail"])
    seen: list[str] = []
    page = client.list_marks("e_1", limit=500)
    assert len(page["marks"]) < 500 and page["nextCursor"]
    calls = 1
    while True:
        seen += [m["id"] for m in page["marks"]]
        if not page["nextCursor"]:
            break
        page = client.list_marks("e_1", limit=500, cursor=page["nextCursor"])
        calls += 1
    assert seen == ["h2", "h1", "analysis-ladder-3-00017"] and calls == 2


def test_create_review_task_mails_with_a_brake_and_audits_once(api: FakeAPI) -> None:
    client, _ = make(api, ALL)
    first = client.create_review_task(["e_2", "e_4"], member_id=OTHER, mode="assisted")
    assert first["given"] == ["e_2", "e_4"] and first["mailed"] is True
    (row,) = api.audit
    assert row["recordingIds"] == ["e_2", "e_4"]  # one row naming every recording given
    api.tasks.clear()
    again = client.create_review_task(["e_2"], member_id=OTHER, mode="assisted")
    assert again["given"] == ["e_2"] and again["mailed"] is False  # 1 a minute
    api.clock = 61
    api.tasks.clear()
    assert client.create_review_task(["e_2"], member_id=OTHER, mode="assisted")["mailed"] is True
    for minute in range(2, 6):
        api.clock = 61 * minute
        api.tasks.clear()
        client.create_review_task(["e_2"], member_id=OTHER, mode="assisted")
    assert api.emails == [OTHER] * 5  # 5 an hour
    mine = client.create_review_task(["e_2"], member_id=ME, mode="assisted")
    assert mine["given"] == ["e_2"] and mine["mailed"] is False  # no mail to yourself


def test_access_off_turns_writes_off_and_on_leaves_them_off(api: FakeAPI) -> None:
    client, _ = make(api, ALL)
    assert api.token_list() == {"agentAccess": True, "agentWrite": True}
    api.set_access(False)
    assert api.workspace_audit == ["agent_access_off", "agent_write_off"]
    with pytest.raises(c.EarlyAccessRequired):
        client.propose_marks("e_1", [MARK])
    api.set_access(True)
    assert api.token_list() == {"agentAccess": True, "agentWrite": False}
    with pytest.raises(c.WriteAccessRequired):
        client.propose_marks("e_1", [MARK])
    api.set_access(False)
    with pytest.raises(Refused) as info:  # only on is refused while access is off
        api.set_write_access(True)
    assert info.value.code == "early_access_required"
    api.set_write_access(False)  # off always succeeds


def test_approve_refuses_write_scopes_while_writes_are_off(api: FakeAPI) -> None:
    api.agent_write = False
    login = c.WorkbenchClient(api_base="https://api.test.invalid", transport=api.transport)
    session = login.cli_login_start(device_name="lab laptop", scopes=["read", "marks:write"])
    url = session["approvalUrl"]
    got = api.review_get(url)
    assert got["workspaces"][0]["agentWrite"] is False and got["scopes"] == ["read", "marks:write"]
    with pytest.raises(Refused) as info:
        api.approve(url)
    assert info.value.code == "write_access_required"
    code = api.approve(url, scopes=["read"])  # narrowed to reads: approved
    data = login.cli_login_exchange(
        session_id=session["sessionId"], poll_secret=session["pollSecret"], code=code
    )
    assert data["scopes"] == ["read"]
    # Writes off still lets the page put a write scope on a token; it cannot write yet.
    token = api.create_token("lab agent", ["marks:write"])
    with pytest.raises(c.WriteAccessRequired):
        login.with_token(token).propose_marks("e_1", [MARK])


def test_token_names_never_hold_an_email_or_an_id(api: FakeAPI) -> None:
    for name in ("ann@lab.example", "laptop 20261120091500"):
        with pytest.raises(Refused) as info:
            api.create_token(name)
        assert info.value.code == "invalid_argument"
    login = c.WorkbenchClient(api_base="https://api.test.invalid", transport=api.transport)
    session = login.cli_login_start(device_name="ann@lab.example")
    code = api.approve(session["approvalUrl"])
    data = login.cli_login_exchange(
        session_id=session["sessionId"], poll_secret=session["pollSecret"], code=code
    )
    assert login.with_token(data["token"]).call("whoami")["token"]["name"] == "nexus-workbench"


def test_a_large_body_is_checked_against_the_token_first(api: FakeAPI) -> None:
    client = c.WorkbenchClient(
        api_base="https://api.test.invalid", token=auth_token(), transport=api.transport
    )
    marks = [dict(MARK, text="x" * 150) for _ in range(40)]  # well past 2,048 bytes
    with pytest.raises(c.Unauthenticated):
        client.propose_marks("e_1", marks)
    real, _ = make(api, ALL)
    assert real.propose_marks("e_1", marks)["accepted"] == 40


def test_unknown_action_is_refused_before_the_token(api: FakeAPI) -> None:
    client = c.WorkbenchClient(
        api_base="https://api.test.invalid", token=auth_token(), transport=api.transport
    )
    with pytest.raises(c.InvalidArgument) as info:
        client.call("no_such_action")
    assert "Unknown action." in str(info.value)


def auth_token() -> str:
    """A well-formed token the server does not know."""
    from tests.fake_api import make_token  # noqa: PLC0415

    return make_token()
