"""The version 1.1 commands, ``login --scope`` and ``preprocess`` on the command line."""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Any

import pytest

from nexus_workbench import auth, cli, tables
from nexus_workbench import client as c
from nexus_workbench import scopes as sc
from nexus_workbench.local.sample import make_sample_night
from tests.fake_api import API_BASE, ME, OTHER, FakeAPI, sample_data_v11

ALL = list(sc.ALL)
NEW_COMMANDS = [
    "members",
    "tasks",
    "nights",
    "layers",
    "events",
    "stages",
    "quality",
    "marks",
    "propose",
    "withdraw",
    "task",
    "analyse",
    "preprocess",
]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeAPI:
    fake = FakeAPI()
    sample_data_v11(fake)
    monkeypatch.setattr(
        cli,
        "_client",
        lambda: c.WorkbenchClient(
            api_base=API_BASE, transport=fake.transport, sleep=lambda s: None
        ),
    )
    monkeypatch.setattr(cli, "resolve_api_base", lambda: API_BASE)
    return fake


def sign_in(api: FakeAPI, scopes: list[str] | None = None, *, save: bool = True) -> str:
    token = api.add_token(scopes=scopes or ALL)
    auth.save_token(API_BASE, token, scopes=(scopes or ALL) if save else None)
    return token


def requests(api: FakeAPI) -> list[dict[str, Any]]:
    return [json.loads(r.content) for r in api.requests]


# -- login --scope, whoami, doctor -----------------------------------------------------------


def test_login_asks_for_scopes_and_saves_what_was_approved(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setattr(
        "builtins.input", lambda text: api.approve(opened[-1], scopes=["read", "detail"])
    )
    assert cli.main(["login", "--scope", "detail", "--scope", "marks:write"]) == 0
    out = capsys.readouterr().out
    assert "Asking for scopes: read, detail, marks:write" in out
    assert "Write scopes can be approved only in a workspace where Insai" in out
    assert "Scopes: read, detail" in out
    assert "Workbench approved fewer scopes than asked; left out: marks:write." in out
    assert requests(api)[0]["scopes"] == ["read", "detail", "marks:write"]
    assert auth.load_token(API_BASE).scopes == ("read", "detail")


def test_login_says_when_write_scopes_were_refused(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # The approval page refuses write scopes while writes are off (contract 7.9); a server
    # that passes the refusal on to exchange gets one clear sentence.
    api.login_errors = {"exchange": [(403, "write_access_required")]}
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setattr("builtins.input", lambda text: api.approve(opened[-1], scopes=["read"]))
    assert cli.main(["login", "--scope", "tasks:write"]) == 1
    err = capsys.readouterr().err
    assert "Workbench refused to approve write scopes" in err
    assert "`nexus-workbench login --scope detail`" in err


def test_login_without_scope_sends_none(api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys):
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setattr("builtins.input", lambda text: api.approve(opened[-1]))
    assert cli.main(["login"]) == 0
    assert "scopes" not in requests(api)[0]  # version 1 behaviour: read only
    out = capsys.readouterr().out
    assert "Scopes: read" in out and "Write scopes" not in out
    assert auth.load_token(API_BASE).scopes == ("read",)


def test_login_token_keeps_the_page_scopes(api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys):
    token = api.add_token(scopes=["read", "detail"])

    class Stdin(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", Stdin(token))
    assert cli.main(["login", "--token", "--scope", "marks:write"]) == 0
    out = capsys.readouterr().out
    assert "--scope does not change them" in out
    assert "Scopes: read, detail" in out
    assert auth.load_token(API_BASE).scopes == ("read", "detail")


def test_login_unknown_scope_is_a_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["login", "--scope", "admin"])
    assert info.value.code == 2


def test_whoami_shows_and_saves_scopes(api: FakeAPI, capsys) -> None:
    sign_in(api, ["read", "detail"], save=False)  # saved by an older version: scopes unknown
    assert auth.load_token(API_BASE).scopes is None
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert f"Member:    {ME}" in out
    assert "Scopes:    read, detail" in out
    assert auth.load_token(API_BASE).scopes == ("read", "detail")


def test_doctor_shows_scopes(api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("PATH", "")
    sign_in(api, ["read", "tasks:write"])
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "Scopes: read, tasks:write (saved with the token)" in out
    assert "signed in to Sleep lab, scopes read, tasks:write, token expires" in out


# -- reads -----------------------------------------------------------------------------------


def test_members_table_and_json(api: FakeAPI, capsys) -> None:
    sign_in(api)
    assert cli.main(["members"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["MEMBER", "ROLE"]
    assert lines[1].split() == [ME, "member", "you"]
    assert "\x1b" not in "".join(lines)  # no colour when not a terminal
    assert cli.main(["members", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["members"][0]["id"] == ME


def test_tasks(api: FakeAPI, capsys) -> None:
    sign_in(api)
    assert cli.main(["tasks"]) == 0
    out = capsys.readouterr().out
    assert "task_00000000000000a1" in out and "task_00000000000000b2" not in out
    assert out.splitlines()[0].split()[:4] == ["TASK", "RECORDING", "MODE", "STATE"]
    assert cli.main(["tasks", "--all"]) == 0
    assert "task_00000000000000b2" in capsys.readouterr().out
    assert requests(api)[-1] == {"action": "list_tasks", "mine": False}


def test_nights_and_layers(api: FakeAPI, capsys) -> None:
    sign_in(api)
    assert cli.main(["nights", "--project", "p_1"]) == 0
    out = capsys.readouterr().out
    assert "n_1" in out and "pk_0123456789abcdef" in out and "e_0, e_1" in out
    assert cli.main(["layers"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split()[:3] == ["LAYER", "ORIGIN", "STATUS"]
    # e_2 is held and e_3 has a blind desk: both are hidden.
    assert out[1].split() == ["analysis:ladder-3", "analysis", "stable", "1", "1", "1", "2", "1"]


def test_events_with_zero_counts_say_none_on_the_recording(api: FakeAPI, capsys) -> None:
    sign_in(api)
    api.events["e_1"] = []
    api.empty_kinds["e_1"] = ["blink", "drift"]
    assert cli.main(["events", "e_1"]) == 0
    out = capsys.readouterr().out
    assert out == "No events on this recording.\n"
    assert cli.main(["events", "e_1", "--kind", "blink"]) == 0
    assert capsys.readouterr().out == "No blink events on this recording.\n"
    assert cli.main(["events", "e_1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {
        "available": True,
        "kinds": ["blink", "drift"],
        "events": [],
        "nextCursor": None,
    }


def test_events_table_and_save(api: FakeAPI, tmp_path: Path, capsys) -> None:
    sign_in(api)
    assert cli.main(["events", "e_1", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    assert "Kinds: arousal, spindle" in out
    assert "300.000" in out and "More events: run again with --cursor" in out
    target = tmp_path / "machine.csv"
    assert cli.main(["events", "e_1", "--save", str(target)]) == 0
    assert "Saved 3 machine events" in capsys.readouterr().out
    assert target.read_text().splitlines()[0] == "onset_sec,duration_sec,label"
    assert cli.main(["events", "e_1", "--save", str(target)]) == 1  # never replaced
    assert "(local_file)" in capsys.readouterr().err
    assert cli.main(["events", "e_2"]) == 0
    assert "Withheld" in capsys.readouterr().out


def test_stages_and_save(api: FakeAPI, tmp_path: Path, capsys) -> None:
    sign_in(api)
    assert cli.main(["stages", "e_1"]) == 0
    out = capsys.readouterr().out
    assert "Stages: machine staging, model usleep; 30 epochs of 30 s" in out
    rows = {line.split()[0]: line.split() for line in out.splitlines()[2:8]}
    assert rows["N2"] == ["N2", "10", "5.0", "33.3%"]
    assert rows["unscored"][1] == "2"
    assert cli.main(["stages", "e_1", "--source", "mine"]) == 0
    assert "Flagged questionable: 1 epochs; no signal: 1 epochs" in capsys.readouterr().out
    target = tmp_path / "machine.txt"
    assert cli.main(["stages", "e_1", "--save", str(target)]) == 0
    from nexus_workbench.local import api as local_api  # noqa: PLC0415

    compared = local_api.compare_hypnograms(str(target), str(target))
    assert compared["epochsTotal"] == 30 and compared["kappa"] == 1.0
    capsys.readouterr()
    assert cli.main(["stages", "e_2", "--save", str(tmp_path / "held.txt")]) == 0
    assert "Nothing was saved. You are blinded" in capsys.readouterr().out
    assert not (tmp_path / "held.txt").exists()


def test_quality_and_marks(api: FakeAPI, capsys) -> None:
    sign_in(api)
    assert cli.main(["quality", "e_1"]) == 0
    out = capsys.readouterr().out
    assert "Quality: marginal (score 0.71); 30 epochs of 30 s: good 20, marginal 6, bad 2" in out
    assert out.splitlines()[1].split()[:3] == ["CH", "GRADE", "SCORE"]
    assert cli.main(["marks", "e_1"]) == 0
    out = capsys.readouterr().out
    assert "analysis-ladder-3-00017" in out and "sigma burst" in out
    assert cli.main(["marks", "e_3"]) == 0
    assert "withheld" in capsys.readouterr().out


def test_detail_without_the_scope_fails_locally_with_its_code(api: FakeAPI, capsys) -> None:
    sign_in(api, ["read"])
    assert cli.main(["stages", "e_1"]) == 1
    err = capsys.readouterr().err
    assert err.strip() == (
        "This token cannot read per-epoch and per-event detail (scope_required). "
        "Sign in again with `nexus-workbench login --scope detail`."
    )
    assert api.requests == []
    assert cli.main(["marks", "e_1", "--json"]) == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "scope_required"


def test_limits_are_usage_errors(api: FakeAPI, capsys) -> None:
    sign_in(api)
    assert cli.main(["events", "e_1", "--limit", "5000"]) == 2
    assert "(usage)" in capsys.readouterr().err
    assert cli.main(["marks", "e_1", "--limit", "0", "--json"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "usage"


# -- writes ----------------------------------------------------------------------------------


def marks_csv(tmp_path: Path, rows: int = 3) -> Path:
    path = tmp_path / "marks.csv"
    lines = ["kind,t0,t1,channels,label,confidence,text"]
    for i in range(rows):
        lines.append(f"arousal,{10 * i},{10 * i + 4},1;2,EMG rise,0.6,EMG rise with alpha")
    path.write_text("\n".join(lines) + "\n")
    return path


def test_propose_dry_run_sends_nothing(api: FakeAPI, tmp_path: Path, capsys) -> None:
    assert cli.main(["propose", "e_1", str(marks_csv(tmp_path)), "--dry-run"]) == 0
    assert (
        "3 marks are valid. They would go in 1 call. Nothing was sent." in capsys.readouterr().out
    )
    assert api.requests == []


def test_propose_sends_in_batches_and_a_rerun_writes_nothing_twice(
    api: FakeAPI, tmp_path: Path, capsys
) -> None:
    sign_in(api)
    path = marks_csv(tmp_path, rows=1200)
    assert cli.main(["propose", "e_1", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Proposed 1200 marks on e_1 as layer agent:tok_" in out
    assert "1200 live marks in the layer" in out
    assert "They are proposals" in out
    sent = [r for r in requests(api) if r["action"] == "propose_marks"]
    assert [len(r["marks"]) for r in sent] == [500, 500, 200]
    assert sent[0]["marks"][0] == {
        "kind": "arousal",
        "t0": 0.0,
        "t1": 4.0,
        "channels": [1, 2],
        "label": "EMG rise",
        "text": "EMG rise with alpha",
        "confidence": 0.6,
    }
    assert cli.main(["propose", "e_1", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1200  # answered from the first calls
    assert sum(len(v) for v in api.agent_rows.values()) == 1200


def test_propose_bad_file_has_a_code(api: FakeAPI, tmp_path: Path, capsys) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("kind,t0,t1\nspindle,10,5\n")
    assert cli.main(["propose", "e_1", str(bad)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("Mark 1: t0 must be 0 or more and t1 at least t0 (local_file).")
    assert "Nothing was sent." in err
    wrong = tmp_path / "marks.txt"
    wrong.write_text("x")
    assert cli.main(["propose", "e_1", str(wrong), "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "local_file"
    assert api.requests == []


def test_propose_json_file(api: FakeAPI, tmp_path: Path, capsys) -> None:
    sign_in(api)
    path = tmp_path / "marks.json"
    path.write_text(json.dumps({"marks": [{"kind": "spindle", "t0": 1, "t1": 2}]}))
    assert cli.main(["propose", "e_1", str(path), "--op-id", "first"]) == 0
    assert requests(api)[-1]["opId"] == "first-1"


def test_propose_refused_when_writes_are_off(api: FakeAPI, tmp_path: Path, capsys) -> None:
    api.agent_write = False
    sign_in(api)
    assert cli.main(["propose", "e_1", str(marks_csv(tmp_path))]) == 1
    assert capsys.readouterr().err.startswith(
        "This workspace does not take agent writes yet (write_access_required)."
    )


def test_withdraw(api: FakeAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    sign_in(api)
    assert cli.main(["propose", "e_1", str(marks_csv(tmp_path)), "--json"]) == 0
    ids = json.loads(capsys.readouterr().out)["ids"]
    assert cli.main(["withdraw", "e_1", "--mark", ids[0]]) == 0
    assert "Withdrew 1 marks; 2 live marks left in the layer." in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["withdraw", "e_1"]) == 2  # all of them needs --yes when nobody can answer
    assert "--yes" in capsys.readouterr().err
    assert cli.main(["withdraw", "e_1", "--yes", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"withdrawn": 2, "total": 0}


def test_task_give(api: FakeAPI, capsys) -> None:
    sign_in(api)
    argv = ["task", "give", "e_2,e_0", "e_1", "--member", OTHER, "--mode", "blind"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"Gave {OTHER} 1 blind task (batch batch_0001): e_2")
    assert "Workbench emailed the member about the new tasks." in out
    assert "machine_seen" in out and "--confirm-machine-seen" in out
    assert "open_task" in out
    assert cli.main(["task", "give", "e_1", "--member", OTHER, "--mode", "assisted"]) == 1
    out = capsys.readouterr().out
    assert "No task was given." in out and "email" not in out
    assert cli.main(["task", "give", "e_4", "--member", OTHER, "--mode", "assisted"]) == 0
    assert "Workbench sent no email this time" in capsys.readouterr().out  # the mail brake
    assert cli.main(["task", "give", "e_1", "--member", "bob", "--mode", "blind"]) == 2


def test_analyse(api: FakeAPI, capsys) -> None:
    sign_in(api)
    assert cli.main(["analyse", "e_4"]) == 1
    assert capsys.readouterr().err.startswith(
        "Starting an analysis from an agent is not available yet (not_available). "
        "Use Analyse on the recording page."
    )
    assert cli.main(["analyse", "e_4", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "not_available"
    assert cli.main(["analyse", "e_1"]) == 1
    assert "(already_analysed)" in capsys.readouterr().err
    api.recordings[0]["hasEvents"] = False
    assert cli.main(["analyse", "e_0"]) == 1
    assert "only the person who uploaded" in capsys.readouterr().err


def test_analyse_on_a_server_that_starts_runs(api: FakeAPI, capsys) -> None:
    api.analysis_available = True
    sign_in(api)
    assert cli.main(["analyse", "e_4"]) == 0
    assert "Analysis started for e_4." in capsys.readouterr().out
    assert cli.main(["analyze", "e_4", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "already"}


def test_old_server_says_not_available_yet(api: FakeAPI, capsys) -> None:
    api.v11 = False
    sign_in(api)
    assert cli.main(["nights"]) == 1
    assert capsys.readouterr().err.startswith(
        "This Workbench server does not offer list_nights yet (invalid_argument)."
    )


# -- preprocess ------------------------------------------------------------------------------


def test_preprocess_command(tmp_path: Path, capsys) -> None:
    night = make_sample_night(str(tmp_path), hours=0.5, seed=0)
    before = Path(night["edf"]).read_bytes()
    assert cli.main(["preprocess", night["edf"], "--low", "0.3", "--high", "35"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Wrote sample_night_preprocessed.edf (id ")
    assert "Applied: band-pass 0.3-35 Hz. The input is unchanged." in out
    assert str(tmp_path) not in out
    assert Path(night["edf"]).read_bytes() == before
    assert cli.main(["preprocess", night["edf"], "--low", "0.3", "--json"]) == 1
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "local_file"
    assert (
        cli.main(
            [
                "preprocess",
                night["edf"],
                "--channels",
                "1,C4-M1",
                "--out",
                str(tmp_path / "two.edf"),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert [ch["label"] for ch in result["channels"]] == ["C3-M2", "C4-M1"]


def test_documented_sample_then_preprocess_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """The README path: ``sample`` then ``preprocess ... --low 0.3 --high 35 --notch 50``."""
    monkeypatch.chdir(tmp_path)
    assert cli.main(["sample", "--hours", "0.5"]) == 0
    capsys.readouterr()
    edf = "nexus-sample/sample_night.edf"
    argv = ["preprocess", edf, "--low", "0.3", "--high", "35", "--notch", "50"]
    assert cli.main(argv) == 0
    out = capsys.readouterr().out
    assert "Applied: band-pass 0.3-35 Hz. The input is unchanged." in out
    assert "Note: notch 50 Hz skipped on C3-M2: at or above half its 100 Hz sample rate." in out
    rows = [line.split() for line in out.splitlines() if line[:1].isdigit()]
    assert [row[1] for row in rows] == ["C3-M2", "C4-M1", "E1-M2", "Chin1-Chin2"]
    assert [row[-1] for row in rows] == ["0"] * 4  # filtering never clips


# -- tables and help ---------------------------------------------------------------------------


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_tables_colour_only_on_a_terminal_without_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [["a", 1], ["long value", 22]]
    plain = tables.render(["NAME", "N"], rows, right=[1], stream=io.StringIO())
    assert plain.splitlines() == ["NAME         N", "a            1", "long value  22"]
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert tables.render(["NAME", "N"], rows, stream=Tty()).startswith(tables.BOLD)
    monkeypatch.setenv("NO_COLOR", "1")
    assert "\x1b" not in tables.render(["NAME", "N"], rows, stream=Tty())


def test_table_cells_are_plain_and_capped() -> None:
    text = tables.render(["X"], [["a\x1b[2J\nb"], ["y" * 100], [None], [True]])
    lines = text.splitlines()
    assert "\x1b" not in text and lines[1] == "a [2J b"
    assert len(lines[2]) == tables.MAX_CELL and lines[2].endswith("…")
    assert lines[3:] == ["-", "yes"]


def test_new_commands_have_help_with_an_example() -> None:
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for name in NEW_COMMANDS:
        command = sub.choices[name]
        text = command.format_help()
        if name == "task":
            give = next(a for a in command._actions if isinstance(a, argparse._SubParsersAction))
            text = give.choices["give"].format_help()
        assert "example" in text, name
        assert "nexus-workbench " in text, name
        if name not in ("task",):
            assert "--json" in text, name
    assert "Exit codes: 0 ok, 1 failure, 2 usage." in parser.format_help()
