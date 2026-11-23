"""``nexus-workbench channels show | set | clear`` against a fake agent API (contract 7.12)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from nexus_workbench import channels as chan
from nexus_workbench import cli
from nexus_workbench import client as c
from tests.fake_api import API_BASE, make_token
from tests.fake_channels import LABEL, RECORDING, TWIN_LABEL, TWINS, ChannelState, respond

FRONTAL_ON_ET = ["--port", "Ec1=M1", "--port", "Ec2=M2", "--port", "Et1=AF7", "--port", "Et2=AF8"]


@pytest.fixture
def token() -> str:
    return make_token()


@pytest.fixture
def st(monkeypatch: pytest.MonkeyPatch, token: str) -> ChannelState:
    state = ChannelState()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {token}":
            return httpx.Response(
                401, json={"error": {"code": "unauthenticated", "message": "Sign-in failed."}}
            )
        status, body = respond(state, json.loads(request.content))
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        cli,
        "_client",
        lambda: c.WorkbenchClient(
            api_base=API_BASE, token=token, transport=httpx.MockTransport(handler)
        ),
    )
    return state


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(["channels", *argv])
    out, err = capsys.readouterr()
    return code, out, err


def sets(st: ChannelState) -> list[dict[str, Any]]:
    return [b for b in st.calls if b["action"] == "set_channels"]


# -- show -------------------------------------------------------------------------------------


def test_show_not_set(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "show", RECORDING)
    assert code == 0
    assert "Sites:     not set, NOT checked" in out
    assert "Device:    not fixed: sites are set per recording" in out
    assert "Version:   0" in out
    assert "Ec1-Ec2" in out


def test_show_set_and_checked(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    st.version, st.checked = 3, True
    st.ports = {"Ec1": "M1", "Ec2": "M2", "Et1": "AF7", "Et2": "AF8"}
    code, out, _ = run(capsys, "show", RECORDING)
    assert code == 0
    assert "Sites:     set, checked" in out
    assert "Version:   3" in out
    assert "Ports:     --port Ec1=M1 --port Et1=AF7 --port Ec2=M2 --port Et2=AF8 --checked" in out
    assert "M1-M2" in out and "M1-AF7  (inverted)" in out


def test_show_fixed_device(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    st.fixed = True
    code, out, _ = run(capsys, "show", RECORDING)
    assert code == 0
    assert "Device:    fixed by the device" in out
    assert "checked" in out and "NOT checked" not in out
    assert "You cannot change these sites (fixed_device)." in out


def test_show_external_member_can_set_but_not_tick(
    st: ChannelState, capsys: pytest.CaptureFixture[str]
) -> None:
    st.external = True
    _, out, _ = run(capsys, "show", RECORDING)
    assert "save with --unchecked" in out


def test_show_json_is_the_server_answer(
    st: ChannelState, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(capsys, "show", RECORDING, "--json")
    data = json.loads(out)
    assert code == 0
    assert set(data) >= {"effective", "ports", "checked", "version", "permissions", "presets"}


@pytest.mark.parametrize(
    ("setup", "code_text", "rc"),
    [
        ({"enabled": False}, "not_available", 1),
        ({"header_unreadable": True}, "unavailable", 1),
    ],
)
def test_show_server_unable(
    st: ChannelState,
    capsys: pytest.CaptureFixture[str],
    setup: dict[str, Any],
    code_text: str,
    rc: int,
) -> None:
    for k, v in setup.items():
        setattr(st, k, v)
    code, _, err = run(capsys, "show", RECORDING)
    assert code == rc and f"({code_text})" in err


def test_show_unknown_recording(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "show", "e_nope")
    assert code == 1 and "(not_found)" in err


# -- label or id -----------------------------------------------------------------------------


def test_hex_is_an_id_anything_else_a_label() -> None:
    assert chan.recording_ref(RECORDING) == {"id": RECORDING}
    assert chan.recording_ref(" night-08 ") == {"label": "night-08"}
    assert chan.recording_ref(RECORDING[:-1]) == {"label": RECORDING[:-1]}
    assert chan.recording_ref("e_old", by_id=True) == {"id": "e_old"}
    with pytest.raises(chan.UsageError):
        chan.recording_ref(" ")


def test_show_by_label(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "show", LABEL)
    assert code == 0 and f"Recording: {LABEL}" in out
    assert st.calls[-1] == {"action": "get_channels", "label": LABEL}


def test_set_by_label(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "set", LABEL, "--preset", "frontal_on_et", "--checked")
    assert code == 0
    assert [b.get("label") for b in st.calls] == [LABEL, LABEL]
    assert all("id" not in b for b in st.calls)
    assert f"undo: nexus-workbench channels clear {LABEL} --expected-version 1" in out


def test_unknown_label(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "show", "night-99")
    assert code == 1 and "(not_found)" in err and "case-sensitive" in err


def test_ambiguous_label_lists_the_ids(
    st: ChannelState, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = run(capsys, "set", TWIN_LABEL, *FRONTAL_ON_ET, "--checked")
    assert code == 2 and "(ambiguous_label)" in err
    assert all(i in err for i in TWINS) and "one of these ids" in err
    assert sets(st) == []


def test_ambiguous_label_json(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "show", TWIN_LABEL, "--json")
    data = json.loads(err)
    assert code == 2 and data["error"]["code"] == "ambiguous_label"
    assert data["candidates"] == TWINS


def test_id_flag_forces_an_id(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, _ = run(capsys, "show", "e_old", "--id")
    assert code == 1 and st.calls[-1] == {"action": "get_channels", "id": "e_old"}


# -- set --------------------------------------------------------------------------------------


def test_set_reads_the_version_then_writes(
    st: ChannelState, capsys: pytest.CaptureFixture[str]
) -> None:
    st.version = 2
    code, out, _ = run(capsys, "set", RECORDING, *FRONTAL_ON_ET, "--checked")
    assert code == 0
    [body] = sets(st)
    assert body["expectedVersion"] == 2 and body["checked"] is True
    assert body["ports"] == {"Ec1": "M1", "Et1": "AF7", "Ec2": "M2", "Et2": "AF8"}
    assert "Saved (version 3)." in out
    assert "was: --unchecked" in out
    assert "now: --port Ec1=M1 --port Et1=AF7 --port Ec2=M2 --port Et2=AF8 --checked" in out
    assert f"undo: nexus-workbench channels clear {RECORDING} --expected-version 3" in out


def test_set_preset_with_one_port_changed(
    st: ChannelState, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = run(
        capsys, "set", RECORDING, "--preset", "frontal_on_ec", "--port", "et2=", "--unchecked"
    )
    assert code == 0
    assert sets(st)[0]["ports"] == {"Ec1": "AF7", "Et1": "M1", "Ec2": "AF8"}


def test_set_keeps_unnamed_ports(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    st.version, st.ports = 1, {"Ec1": "M1", "Ec2": "M2", "Et1": "AF7", "Et2": "AF8"}
    code, _, _ = run(capsys, "set", RECORDING, "--port", "Ec1=Fp1", "--unchecked")
    assert code == 0
    assert sets(st)[0]["ports"] == {"Ec1": "Fp1", "Et1": "AF7", "Ec2": "M2", "Et2": "AF8"}


def test_undo_names_the_ports_to_take_off(
    st: ChannelState, capsys: pytest.CaptureFixture[str]
) -> None:
    st.version, st.ports = 1, {"Ec1": "M1", "Ec2": "M2"}
    code, out, _ = run(capsys, "set", RECORDING, *FRONTAL_ON_ET, "--checked")
    assert code == 0
    undo = (
        f"undo: nexus-workbench channels set {RECORDING} --port Ec1=M1 --port Et1= "
        "--port Ec2=M2 --port Et2= --unchecked --expected-version 2"
    )
    assert undo in out
    # the undo line, run, puts the old sites back
    argv = undo.removeprefix("undo: nexus-workbench channels ").split()
    assert cli.main(["channels", *argv]) == 0
    assert (st.ports, st.checked, st.version) == ({"Ec1": "M1", "Ec2": "M2"}, False, 3)


def test_set_unchanged(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    st.version, st.ports, st.checked = 4, {"Ec1": "M1"}, False
    code, out, _ = run(capsys, "set", RECORDING, "--unchecked")
    assert code == 0 and "Unchanged" in out and "undo:" not in out and st.version == 4


def test_set_expected_version_flag_is_sent(
    st: ChannelState, capsys: pytest.CaptureFixture[str]
) -> None:
    st.version = 5
    code, _, _ = run(
        capsys, "set", RECORDING, *FRONTAL_ON_ET, "--checked", "--expected-version", "5"
    )
    assert code == 0 and sets(st)[0]["expectedVersion"] == 5


def test_set_json(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "set", RECORDING, *FRONTAL_ON_ET, "--unchecked", "--json")
    data = json.loads(out)
    assert code == 0 and data["version"] == 1 and data["prev"] == {"ports": {}, "checked": False}


@pytest.mark.parametrize(
    "argv",
    [
        ["--port", "Ec1"],
        ["--port", "Ex1=M1"],
        ["--port", "Ec1=M1", "--port", "ec1=M2"],
        ["--preset", "nope"],
        ["--expected-version", "-1"],
    ],
)
def test_set_usage_errors_send_nothing(
    st: ChannelState, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code, _, err = run(capsys, "set", RECORDING, *argv, "--unchecked")
    assert code == 2 and "(usage)" in err
    assert sets(st) == []


def test_set_needs_checked_or_unchecked(st: ChannelState) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["channels", "set", RECORDING, *FRONTAL_ON_ET])
    assert info.value.code == 2


REFUSALS = [
    ({"no_source_key": True}, "no_source_key", 3, "Ask Insai"),
    ({"combined": True}, "combined", 3, "each of its recordings"),
    ({"fixed": True}, "fixed_device", 3, "nothing to set"),
    ({"held": True}, "held", 3, "held blind"),
    ({"device_crossed": True}, "device_crossed", 3, "fix the device"),
    ({"external": True, "checked": True}, "insai_checked", 3, "Ask Insai"),
    ({"header_unparsed": True}, "header_unparsed", 3, "Ec/Et ports"),
    ({"scopes": {"read"}}, "scope_required", 4, "channels:write scope"),
    ({"agent_write": False}, "write_access_required", 4, "Ask Insai"),
    ({"enabled": False}, "not_available", 1, "not switched on"),
]


@pytest.mark.parametrize(("setup", "code_text", "rc", "fix"), REFUSALS)
def test_set_refusals_print_the_reason_and_a_fix(
    st: ChannelState,
    capsys: pytest.CaptureFixture[str],
    setup: dict[str, Any],
    code_text: str,
    rc: int,
    fix: str,
) -> None:
    # read with a clean state, then refuse the write
    real = dict(setup)
    code, _, err = _set_with(st, capsys, real, [*FRONTAL_ON_ET, "--unchecked"])
    assert code == rc, err
    assert f"({code_text})" in err
    assert fix in err


def _set_with(
    st: ChannelState,
    capsys: pytest.CaptureFixture[str],
    setup: dict[str, Any],
    argv: list[str],
) -> tuple[int, str, str]:
    for k, v in setup.items():
        setattr(st, k, v)
    return run(capsys, "set", RECORDING, *argv)


def test_external_member_cannot_tick(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _set_with(st, capsys, {"external": True}, [*FRONTAL_ON_ET, "--checked"])
    assert code == 3 and "(checked_needs_insai)" in err and "--unchecked" in err


@pytest.mark.parametrize(
    ("port", "code_text"),
    [("Ec2=Q9", "bad_site"), ("Ec2=M1", "duplicate_site")],
)
def test_bad_sites_are_the_servers_call(
    st: ChannelState, capsys: pytest.CaptureFixture[str], port: str, code_text: str
) -> None:
    code, _, err = run(capsys, "set", RECORDING, "--port", "Ec1=M1", "--port", port, "--unchecked")
    assert code == 3 and f"({code_text})" in err and chan.REFUSAL_FIXES[code_text] in err


def test_incomplete_checked(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = run(capsys, "set", RECORDING, "--port", "Ec1=M1", "--checked")
    assert code == 3 and "(incomplete)" in err and "all four ports" in err


def test_race_is_changed_with_the_current_sites(
    st: ChannelState, capsys: pytest.CaptureFixture[str]
) -> None:
    st.race = True
    code, _, err = run(capsys, "set", RECORDING, *FRONTAL_ON_ET, "--checked")
    assert code == 3 and "(changed)" in err
    assert "Now at version 1: --port Ec1=Fp1 --port Et1=AF7 --port Ec2=Fp2 --port Et2=AF8" in err


def test_changed_json_carries_current(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    st.version = 2
    code, _, err = run(
        capsys, "set", RECORDING, *FRONTAL_ON_ET, "--checked", "--expected-version", "1", "--json"
    )
    data = json.loads(err)
    assert code == 3
    assert data["error"]["code"] == "changed" and data["error"]["fix"]
    assert data["current"]["version"] == 2


def test_unauthenticated_is_exit_4(
    st: ChannelState, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli,
        "_client",
        lambda: c.WorkbenchClient(
            api_base=API_BASE,
            token=make_token(),
            transport=httpx.MockTransport(
                lambda r: httpx.Response(
                    401, json={"error": {"code": "unauthenticated", "message": "no"}}
                )
            ),
        ),
    )
    code, _, err = run(capsys, "show", RECORDING)
    assert code == 4 and "(unauthenticated)" in err


# -- clear ------------------------------------------------------------------------------------


def test_clear(st: ChannelState, capsys: pytest.CaptureFixture[str]) -> None:
    st.version, st.ports, st.checked = (
        2,
        {"Ec1": "M1", "Ec2": "M2", "Et1": "AF7", "Et2": "AF8"},
        False,
    )
    code, out, _ = run(capsys, "clear", RECORDING)
    assert code == 0
    assert sets(st)[0] == {
        "action": "set_channels",
        "id": RECORDING,
        "expectedVersion": 2,
        "ports": {},
        "checked": False,
    }
    assert "Cleared (version 3)." in out
    assert "was: --port Ec1=M1" in out


def test_clear_over_an_insai_check_is_refused(
    st: ChannelState, capsys: pytest.CaptureFixture[str]
) -> None:
    st.external, st.checked = True, True
    st.ports = {"Ec1": "M1", "Ec2": "M2", "Et1": "AF7", "Et2": "AF8"}
    code, _, err = run(capsys, "clear", RECORDING)
    assert code == 3 and "(insai_checked)" in err


# -- client passthrough -----------------------------------------------------------------------


def test_scope_required_keeps_its_code_and_scope() -> None:
    response = httpx.Response(
        403,
        json={"error": {"code": "scope_required", "message": "No."}, "scope": "channels:write"},
    )
    error = c.error_from_response(response)
    assert isinstance(error, c.ServerRefusal)
    assert error.code == "scope_required" and error.extra == {"scope": "channels:write"}


def test_unknown_403_is_still_forbidden() -> None:
    response = httpx.Response(403, json={"error": {"code": "something_new", "message": "No."}})
    assert isinstance(c.error_from_response(response), c.Forbidden)
