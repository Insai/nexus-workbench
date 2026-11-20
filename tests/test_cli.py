from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from nexus_workbench import auth, cli
from nexus_workbench import client as c
from tests.fake_api import API_BASE, FakeAPI, make_token, sample_data


class FakeStdin(io.StringIO):
    def __init__(self, text: str = "", tty: bool = False) -> None:
        super().__init__(text)
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeAPI:
    fake = FakeAPI()
    sample_data(fake)
    monkeypatch.setattr(
        cli,
        "_client",
        lambda: c.WorkbenchClient(
            api_base=API_BASE, transport=fake.transport, sleep=lambda s: None
        ),
    )
    monkeypatch.setattr(cli, "resolve_api_base", lambda: API_BASE)
    return fake


def no_browser(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    return opened


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["--version"])
    assert info.value.code == 0
    assert "nexus-workbench 0." in capsys.readouterr().out


def test_usage_error_exit_2() -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["nonsense"])
    assert info.value.code == 2


def test_no_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "setup" in capsys.readouterr().out


def test_token_is_not_an_argument_value() -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(["login", "--token", make_token()])
    assert info.value.code == 2


def test_login_with_piped_token(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = api.add_token()
    monkeypatch.setattr("sys.stdin", FakeStdin(token + "\n"))
    assert cli.main(["login", "--token"]) == 0
    out, err = capsys.readouterr()
    assert "Sleep lab" in out
    assert "2027-02-18T09:15:00Z" in out
    assert token not in out + err
    stored = auth.load_token(API_BASE)
    assert stored.token == token
    assert stored.workspace_id == "w_1"


def test_login_hidden_prompt(api: FakeAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    token = api.add_token()
    monkeypatch.setattr("sys.stdin", FakeStdin("", tty=True))
    prompts: list[str] = []
    monkeypatch.setattr("getpass.getpass", lambda text: prompts.append(text) or token)
    assert cli.main(["login", "--token"]) == 0
    assert prompts and "will not be shown" in prompts[0]
    assert auth.load_token(API_BASE).token == token


def test_login_rejects_malformed_token(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin("nxs_pat_typo"))
    assert cli.main(["login", "--token"]) == 1
    err = capsys.readouterr().err
    assert "57" in err
    assert api.requests == []
    assert not auth.credentials_path().exists()


def test_login_does_not_save_refused_token(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(make_token()))  # valid shape, unknown to server
    assert cli.main(["login", "--token"]) == 1
    assert "did not accept your token" in capsys.readouterr().err
    assert auth.load_token(API_BASE).token is None


def test_login_saves_token_when_early_access_off(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    token = api.add_token(early_access=False)
    monkeypatch.setattr("sys.stdin", FakeStdin(token))
    assert cli.main(["login", "--token"]) == 0
    _, err = capsys.readouterr()
    assert c.EARLY_ACCESS_URL in err
    assert auth.load_token(API_BASE).token == token


def test_login_empty_stdin(api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", FakeStdin(""))
    assert cli.main(["login", "--token"]) == 1
    assert "No token was given" in capsys.readouterr().err


# -- browser flow (contract section 5) ---------------------------------------------


def test_browser_login(api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    opened = no_browser(monkeypatch)
    codes: list[str] = []

    def prompt(text: str) -> str:
        code = api.approve(opened[-1])
        codes.append(code)
        return " " + code.lower() + " "  # normalised before sending

    monkeypatch.setattr("builtins.input", prompt)
    assert cli.main(["login"]) == 0
    out = capsys.readouterr().out
    assert opened and opened[0].startswith("https://workbench.test.invalid/cli/login/")
    assert opened[0] in out  # also printed
    assert "Sleep lab" in out
    stored = auth.load_token(API_BASE)
    assert stored.token in api.tokens
    exchange = json.loads(api.requests[1].content)
    assert exchange["action"] == "exchange"
    assert exchange["code"] == codes[0].replace("-", "")


def test_browser_login_wrong_code_then_right(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    opened = no_browser(monkeypatch)
    state: dict[str, Any] = {"code": None, "n": 0}

    def prompt(text: str) -> str:
        if state["code"] is None:
            state["code"] = api.approve(opened[-1])
        state["n"] += 1
        if state["n"] == 1:
            return "ABC"  # malformed: refused locally, no attempt used
        if state["n"] == 2:
            return "AAAA-AAAA"  # well formed but wrong
        return state["code"]

    monkeypatch.setattr("builtins.input", prompt)
    assert cli.main(["login"]) == 0
    err = capsys.readouterr().err
    assert "8 characters" in err
    assert "4 attempts left" in err
    exchanges = [json.loads(r.content) for r in api.requests if b"exchange" in r.content]
    assert len(exchanges) == 2


def test_browser_login_out_of_attempts(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    opened = no_browser(monkeypatch)
    approved: list[str] = []

    def prompt(text: str) -> str:
        if not approved:
            approved.append(api.approve(opened[-1]))
        return "2222-2222"

    monkeypatch.setattr("builtins.input", prompt)
    assert cli.main(["login"]) == 1
    err = capsys.readouterr().err
    assert "No attempts are left" in err
    assert auth.load_token(API_BASE).token is None


def test_browser_login_expired(api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    no_browser(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda text: "ABCD-EFGH")  # never approved
    assert cli.main(["login"]) == 1
    assert "expired" in capsys.readouterr().err


def test_browser_login_unavailable_falls_back_to_token(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    api.login_available = False
    opened = no_browser(monkeypatch)
    token = api.add_token()
    monkeypatch.setattr("sys.stdin", FakeStdin(token))
    assert cli.main(["login"]) == 0
    out = capsys.readouterr().out
    assert "Connect an agent" in out
    assert opened == []
    assert auth.load_token(API_BASE).token == token


def test_normalize_code() -> None:
    from nexus_workbench.login import code_problem, normalize_code

    assert normalize_code(" abcd-efgh ") == "ABCDEFGH"
    assert normalize_code("ab cd ef gh") == "ABCDEFGH"
    assert code_problem("ABCDEFGH") is None
    assert "8 characters" in (code_problem("ABC") or "")
    assert "I" in (code_problem("ABCDEFGI") or "")
    assert "0" in (code_problem("ABCDEFG0") or "")


# -- whoami / logout / recordings ------------------------------------------------------


def test_whoami(api: FakeAPI, capsys) -> None:
    token = api.add_token()
    auth.save_token(API_BASE, token)
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert "Sleep lab" in out and "member" in out
    assert cli.main(["whoami", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["role"] == "member"


def test_whoami_signed_out(api: FakeAPI, capsys) -> None:
    assert cli.main(["whoami"]) == 1
    assert "nexus-workbench login" in capsys.readouterr().err


def test_logout(api: FakeAPI, capsys) -> None:
    auth.save_token(API_BASE, api.add_token())
    assert cli.main(["logout"]) == 0
    out = capsys.readouterr().out
    assert "Signed out" in out
    assert "revoke" in out
    assert auth.load_token(API_BASE).token is None
    assert cli.main(["logout"]) == 0
    assert "No saved sign-in" in capsys.readouterr().out


def test_recordings_table(api: FakeAPI, capsys) -> None:
    auth.save_token(API_BASE, api.add_token())
    assert cli.main(["recordings", "--limit", "2"]) == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split() == ["ID", "PROJECT", "ADDED", "LENGTH", "QUALITY", "DEVICE", "FLAGS"]
    assert lines[1].startswith("e_4")
    assert "8h00" in lines[1]
    assert "--cursor" in out


def test_recordings_project_and_json(api: FakeAPI, capsys) -> None:
    auth.save_token(API_BASE, api.add_token())
    assert cli.main(["recordings", "--project", "p_1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert {r["projectId"] for r in data["recordings"]} == {"p_1"}


def test_recordings_bad_limit(api: FakeAPI, capsys) -> None:
    assert cli.main(["recordings", "--limit", "0"]) == 2


def test_table_escapes_control_characters() -> None:
    table = cli.format_recordings_table(
        [{"id": "e\x1b[31m", "projectId": None, "addedAt": "2026-11-20T09:15:00Z", "url": "u"}]
    )
    assert "\x1b" not in table


# -- doctor ---------------------------------------------------------------------------


def _fake_exe(directory: Path, name: str, script: str) -> None:
    import sys

    path = directory / name
    path.write_text(f"#!{sys.executable}\n{script}")
    path.chmod(0o755)


def test_doctor_ok(api: FakeAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_exe(
        bin_dir,
        "codex",
        "import json,sys\n"
        "print(json.dumps({'transport': {'type': 'stdio', 'command': '/x/nexus-workbench',"
        " 'args': ['mcp']}}))\n",
    )
    monkeypatch.setenv("PATH", str(bin_dir))
    token = api.add_token()
    auth.save_token(API_BASE, token)
    assert cli.main(["doctor"]) == 0
    out = capsys.readouterr().out
    assert "API base: https://api.test.invalid" in out
    assert f"from file (nxs_pat_…{token[-4:]})" in out
    assert token not in out
    assert "signed in to Sleep lab" in out
    assert "Claude Code: not found" in out
    assert "Codex:" in out and "registered (/x/nexus-workbench mcp)" in out


def test_doctor_reports_failure(api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "Token: none" in out


def test_doctor_env_token_precise_failure(
    api: FakeAPI, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("PATH", "/nonexistent")
    token = api.add_token(expired=True)
    monkeypatch.setenv("NEXUS_TOKEN", token)
    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out
    assert "from env" in out
    assert "expired" in out
    assert token not in out
