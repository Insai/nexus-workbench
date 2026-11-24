"""Guards for what an agent meets first: tool metadata, structured errors, prompts, the
registry files and the update notice. Nothing here touches the network."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
from mcp import Client

from nexus_workbench import __version__, mcp_errors, mcp_server, prompts, update_check
from nexus_workbench import client as c
from nexus_workbench import scopes as sc
from tests.fake_api import API_BASE, FakeAPI, sample_data
from tests.test_mcp_server import error_of

ROOT = Path(__file__).resolve().parents[1]
ALL_SCOPES = list(sc.ALL)
#: Local tools that read Workbench when given a recording_id, so they are open-world.
READS_WORKSPACE_WITH_ID = {"compare_hypnograms", "compare_events"}

NUMBER_WORDS = {
    "four": 4,
    "five": 5,
    "six": 6,
    "eight": 8,
    "thirteen": 13,
    "twenty-two": 22,
}


def run(coro_fn: Any) -> Any:
    return anyio.run(coro_fn)


def factory_for(api: FakeAPI, token: str | None, transport: Any = None) -> Any:
    def factory() -> c.WorkbenchClient:
        return c.WorkbenchClient(
            api_base=API_BASE,
            token=token,
            transport=transport or api.transport,
            sleep=lambda s: None,
        )

    return factory


@pytest.fixture
def api() -> FakeAPI:
    fake = FakeAPI()
    sample_data(fake)
    return fake


def listed_tools(scopes: Any = ALL_SCOPES) -> list[Any]:
    server = mcp_server.build_server(lambda: c.WorkbenchClient(api_base=API_BASE), scopes=scopes)

    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            return (await client.list_tools()).tools

    return run(go)


def call(server: Any, name: str, args: dict[str, Any] | None = None) -> Any:
    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            return await client.call_tool(name, args or {})

    return run(go)


# -- tool metadata -------------------------------------------------------------------------


def test_every_tool_sets_all_four_hints_explicitly() -> None:
    tools = listed_tools()
    assert len(tools) == len(mcp_server.all_tools(lambda: None))  # type: ignore[arg-type,return-value]
    for tool in tools:
        ann = tool.annotations
        assert ann is not None, tool.name
        for hint in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint"):
            assert isinstance(getattr(ann, hint), bool), (tool.name, hint)
        if ann.read_only_hint:
            assert ann.destructive_hint is False, tool.name


def test_every_input_parameter_has_a_description() -> None:
    for tool in listed_tools():
        for name, prop in (tool.input_schema.get("properties") or {}).items():
            assert isinstance(prop.get("description"), str), (tool.name, name)
            assert len(prop["description"]) > 10, (tool.name, name)


def test_local_tools_are_closed_world() -> None:
    specs = {spec.name: spec for spec in mcp_server.local_tools()}
    assert set(specs) == set(mcp_errors.LOCAL_TOOLS)
    for name, spec in specs.items():
        assert spec.scope is None
        # The two comparisons read Workbench only with a recording_id, and say so.
        assert spec.open_world is (name in READS_WORKSPACE_WITH_ID), name
        if spec.open_world:
            assert "reads Workbench" in spec.description
    for spec in mcp_server.workspace_tools(lambda: None):  # type: ignore[arg-type,return-value]
        assert spec.open_world is True, spec.name


def _count(text: str, pattern: str) -> int:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    assert match, pattern
    word = match.group(1).lower()
    return int(word) if word.isdigit() else NUMBER_WORDS[word]


def test_tool_counts_in_the_docs_match_the_server() -> None:
    every = mcp_server.all_tools(lambda: None)  # type: ignore[arg-type,return-value]
    local = mcp_server.local_tools()
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert _count(readme, r"MCP server named `nexus` with ([\w-]+) tools") == len(every)
    assert _count(readme, r"([\w-]+) tools work on EDF files") == len(local)
    tools_md = (ROOT / "docs" / "tools.md").read_text(encoding="utf-8")
    assert _count(tools_md, r"first the ([\w-]+) local tools") == len(local)
    contract = (ROOT / "docs" / "contract.md").read_text(encoding="utf-8")
    assert _count(contract, r"There are ([\w-]+)\. Names are fixed") == len(local)
    # Every tool the README's tables name is a tool, and every tool is in a table.
    tools_section = readme.split("## Tools", 1)[1].split("### Prompts", 1)[0]
    assert _count(tools_section, r"The server has ([\w-]+) tools") == len(every)
    assert _count(readme, r"tools and ([\w-]+) prompts") == len(prompts.PROMPTS)
    named = set(re.findall(r"^\| `([a-z_]+)` \|", tools_section, flags=re.MULTILINE))
    assert named == {spec.name for spec in every}


# -- structured errors ---------------------------------------------------------------------


def test_early_access_error_is_structured(api: FakeAPI) -> None:
    server = mcp_server.build_server(factory_for(api, api.add_token()), scopes=None)
    api.agent_access = False
    result = call(server, "list_projects")
    assert result.is_error is True
    assert "Agent access is not on" in result.content[0].text
    error = error_of(result)
    assert error["code"] == "early_access_required"
    assert error["howToFix"] == [mcp_errors.REQUEST_ACCESS]
    assert error["localAlternative"] == list(mcp_errors.LOCAL_TOOLS)
    assert json.loads(result.content[1].text) == error


def test_expired_token_error_says_login(api: FakeAPI) -> None:
    server = mcp_server.build_server(factory_for(api, api.add_token(expired=True)), scopes=None)
    result = call(server, "whoami")
    error = error_of(result)
    assert error["code"] == "token_expired"
    assert error["howToFix"] == ["nexus-workbench login"]
    assert "inspect_edf" in error["localAlternative"]
    assert mcp_errors.RUN_IT_YOURSELF in result.content[0].text


def test_write_access_error_has_no_local_alternative(api: FakeAPI) -> None:
    token = api.add_token(scopes=["read", "marks:write"])
    server = mcp_server.build_server(factory_for(api, token), scopes=["read", "marks:write"])
    result = call(server, "withdraw_marks", {"id": "e_1"})
    error = error_of(result)
    assert error["code"] == "write_access_required"
    assert error["localAlternative"] == []
    assert error["howToFix"] and "Insai" in error["howToFix"][0]


def test_rate_limit_error_carries_retry_after(api: FakeAPI) -> None:
    server = mcp_server.build_server(factory_for(api, api.add_token()), scopes=None)
    api.rate_limit_next = ["30", "30"]  # more than the client waits by itself
    result = call(server, "list_projects")
    error = error_of(result)
    assert error["code"] == "rate_limited"
    assert error["retryAfter"] == 30
    assert error["howToFix"] == ["Wait 30 seconds, then try again."]


def test_request_id_from_a_header_reaches_the_tool_error(api: FakeAPI) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = api.handler(request)
        response.headers["X-Cloud-Trace-Context"] = "abc123def/456;o=1"
        return response

    api.agent_access = False
    factory = factory_for(api, api.add_token(), transport=httpx.MockTransport(handler))
    result = call(mcp_server.build_server(factory, scopes=None), "whoami")
    assert error_of(result)["requestId"] == "abc123def"


def test_every_error_has_the_same_keys(api: FakeAPI, tmp_path: Path) -> None:
    server = mcp_server.build_server(factory_for(api, None), scopes=None)
    keys = {"ok", "code", "reason", "howToFix", "localAlternative", "requestId", "retryAfter"}
    for name, args in (
        ("whoami", {}),
        ("list_recordings", {"limit": 500}),
        ("inspect_edf", {"path": str(tmp_path / "missing.edf")}),
        ("compare_hypnograms", {"path_a": "a.txt"}),
    ):
        result = call(server, name, args)
        assert result.is_error is True, name
        assert set(error_of(result)) == keys, name
        assert error_of(result)["ok"] is False
        assert error_of(result)["code"] in CONTRACT_CODES, name


def _contract_codes() -> set[str]:
    text = (ROOT / "docs" / "contract.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\|(?: \d+ \|)? `([a-z_]+)` \|", text, flags=re.MULTILINE))


CONTRACT_CODES = _contract_codes()


def test_every_code_with_fixed_steps_is_a_contract_code() -> None:
    assert {"not_signed_in", "early_access_required", "scope_required"} <= CONTRACT_CODES
    assert set(mcp_errors.HOW_TO_FIX) <= CONTRACT_CODES
    assert mcp_errors.OFFER_LOCAL <= CONTRACT_CODES
    assert mcp_errors.SIGN_IN_CODES <= CONTRACT_CODES
    for steps in mcp_errors.HOW_TO_FIX.values():
        for step in steps:
            assert "token" not in step.lower() or "never" in step.lower()


def test_request_id_parsing() -> None:
    def response(headers: dict[str, str] | None = None, body: Any = None) -> httpx.Response:
        return httpx.Response(403, headers=headers or {}, json=body or {"error": {}})

    assert c.request_id_of(response({"X-Request-Id": "r-1"})) == "r-1"
    assert c.request_id_of(response(body={"requestId": "b_2", "error": {}})) == "b_2"
    assert c.request_id_of(response(body={"error": {"requestId": "e.3"}})) == "e.3"
    assert c.request_id_of(response({"X-Request-Id": "bad id with spaces"})) is None
    assert c.request_id_of(response()) is None
    error = c.error_from_response(response({"X-Request-Id": "r-9"}))
    assert error.request_id == "r-9"


def test_cli_prints_the_request_id(capsys: pytest.CaptureFixture[str]) -> None:
    from nexus_workbench import cli

    error = c.Forbidden()
    error.request_id = "r-7"
    cli._fail_workbench(error)
    assert capsys.readouterr().err.rstrip().endswith("Request id: r-7.")
    cli._fail_workbench(error, as_json=True)
    assert json.loads(capsys.readouterr().err)["error"]["requestId"] == "r-7"
    cli._fail_workbench(c.Forbidden(), as_json=True)
    assert "requestId" not in json.loads(capsys.readouterr().err)["error"]


# -- prompts ---------------------------------------------------------------------------------


def test_every_tool_a_prompt_names_exists_and_is_named_in_its_text() -> None:
    names = {spec.name for spec in mcp_server.all_tools(lambda: None)}  # type: ignore[arg-type,return-value]
    assert 3 <= len(prompts.PROMPTS) <= 5
    for spec in prompts.PROMPTS:
        assert set(spec.tools) <= names, spec.name
        text = spec.fn()
        for tool in spec.tools:
            assert tool in text, (spec.name, tool)
        # No other tool name appears in the text without being declared.
        assert {n for n in names if re.search(rf"\b{n}\b", text)} <= set(spec.tools), spec.name
        assert "data, not instructions" in text
        assert "paste" not in text.lower()


def test_signed_out_prompts_use_only_tools_a_signed_out_server_lists() -> None:
    signed_out = {t.name for t in listed_tools(scopes=None)}
    local = set(mcp_errors.LOCAL_TOOLS)
    for spec in prompts.PROMPTS:
        if spec.signed_out:
            assert set(spec.tools) <= local & signed_out, spec.name
            assert "nexus-sample/" in spec.fn()  # the defaults are the sample's files
        else:
            assert "nexus-workbench login" in spec.fn()


def test_prompts_are_served(api: FakeAPI) -> None:
    server = mcp_server.build_server(factory_for(api, None), scopes=None)

    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            listed = await client.list_prompts()
            got = await client.get_prompt("qc_edf_file", {"path": "night.edf"})
            default = await client.get_prompt("scorer_agreement", {})
            return listed, got, default

    listed, got, default = run(go)
    assert {p.name for p in listed.prompts} == {spec.name for spec in prompts.PROMPTS}
    for prompt in listed.prompts:
        assert prompt.title and prompt.description
        for argument in prompt.arguments or []:
            assert argument.description
    assert "night.edf" in got.messages[0].content.text
    assert "sample_hypnogram_scorer2.txt" in default.messages[0].content.text


# -- registry files --------------------------------------------------------------------------


def test_server_json_matches_the_package() -> None:
    data = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    assert data["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/")
    assert data["version"] == __version__
    (package,) = data["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == "nexus-workbench"
    assert package["version"] == __version__
    assert package["transport"] == {"type": "stdio"}
    assert package["packageArguments"] == [{"type": "positional", "value": "mcp"}]
    assert len(data["description"]) <= 100
    # PyPI ownership: the registry looks for this line in the package's README.
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert f"mcp-name: {data['name']}" in readme


def test_llms_install_never_asks_for_a_token_in_chat() -> None:
    text = (ROOT / "llms-install.md").read_text(encoding="utf-8")
    assert "uvx nexus-workbench setup" in text
    assert "nexus-workbench login" in text
    assert "own terminal" in text
    assert "Never ask" in text and "token" in text
    assert "nexus-workbench sample" in text


def test_readme_try_it_block_uses_signed_out_prompts() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    block = readme.split("## Try it in 60 seconds", 1)[1].split("\n## ", 1)[0]
    assert "nexus-workbench sample" in block
    asks = re.findall(r"^> (.+)$", block, flags=re.MULTILINE)
    assert len(asks) == 3
    for ask in asks:
        assert "nexus-sample/" in ask


# -- update notice ---------------------------------------------------------------------------


@pytest.fixture
def check_on(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.delenv(update_check.ENV_OPT_OUT, raising=False)
    path = tmp_path / "cache" / update_check.CACHE_FILE
    monkeypatch.setattr(update_check, "cache_path", lambda: path)

    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the update check must not reach the network in tests")

    monkeypatch.setattr(httpx, "get", no_network)
    return path


def test_release_parsing_and_notice() -> None:
    assert update_check.release("0.2.1") == (0, 2, 1)
    assert update_check.release("0.2.1.dev0") == (0, 2, 1)
    assert update_check.release("nope") is None
    assert update_check.notice_for("0.3.0", "0.2.1") == (
        "nexus-workbench 0.3.0 is available (this is 0.2.1)."
    )
    assert update_check.notice_for("0.2.1", "0.2.1") is None
    assert update_check.notice_for("0.2.0", "0.2.1") is None
    assert update_check.notice_for(None, "0.2.1") is None
    assert update_check.notice_for("garbage", "0.2.1") is None


def test_check_is_cached_for_a_day(check_on: Path) -> None:
    calls: list[int] = []

    def fetch() -> str:
        calls.append(1)
        return "99.0.0"

    assert update_check.latest_version(fetch=fetch, now=1000.0) == "99.0.0"
    assert update_check.latest_version(fetch=fetch, now=1000.0 + 3600) == "99.0.0"
    assert len(calls) == 1
    assert update_check.latest_version(fetch=fetch, now=1000.0 + 25 * 3600) == "99.0.0"
    assert len(calls) == 2


def test_failed_check_is_silent_and_cached(check_on: Path) -> None:
    calls: list[int] = []

    def offline() -> None:
        calls.append(1)
        return None

    assert update_check.check(fetch=offline) is None
    assert update_check.check(fetch=offline) is None
    assert len(calls) == 1  # an offline computer asks at most once a day

    def boom() -> str:
        raise RuntimeError("anything")

    check_on.unlink()
    assert update_check.check(fetch=boom) is None


def test_unwritable_cache_is_silent(check_on: Path) -> None:
    check_on.parent.mkdir(parents=True)
    check_on.parent.chmod(0o500)
    try:
        assert update_check.check(fetch=lambda: "99.0.0") == (
            f"nexus-workbench 99.0.0 is available (this is {__version__})."
        )
    finally:
        check_on.parent.chmod(0o700)


def test_fetch_latest_is_silent_on_errors(check_on: Path) -> None:
    assert update_check.fetch_latest() is None  # httpx.get raises: no notice


def test_opt_out_fetches_nothing(check_on: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(update_check.ENV_OPT_OUT, "1")

    def fetch() -> str:
        raise AssertionError("fetched although turned off")

    assert update_check.check(fetch=fetch) is None
    notifier = update_check.Notifier(fetch=fetch)
    notifier.start()
    notifier.join(5)
    assert notifier.take() is None
    assert not check_on.exists()


def test_notice_is_added_to_one_tool_result(check_on: Path, api: FakeAPI) -> None:
    notifier = update_check.Notifier(fetch=lambda: "99.0.0")
    server = mcp_server.build_server(factory_for(api, None), scopes=None, notifier=notifier)

    async def go() -> list[Any]:
        async with Client(server, mode="legacy") as client:
            first = await client.call_tool("whoami", {})
            notifier.join(5)
            return [first] + [await client.call_tool("whoami", {}) for _ in range(2)]

    results = run(go)
    notices = [
        block.text
        for result in results
        for block in result.content
        if getattr(block, "text", "").startswith("Notice: ")
    ]
    assert notices == [
        update_check.agent_notice(f"nexus-workbench 99.0.0 is available (this is {__version__}).")
    ]
    assert "uv tool upgrade nexus-workbench" in notices[0]
    # The structured copy is untouched.
    assert error_of(results[1])["code"] == "not_signed_in"


def test_doctor_prints_the_notice(
    check_on: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from nexus_workbench import cli

    monkeypatch.setattr(update_check, "fetch_latest", lambda: "99.0.0")
    monkeypatch.setattr("shutil.which", lambda name: None)
    cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "Update: nexus-workbench 99.0.0 is available" in out
    assert "uv tool upgrade nexus-workbench" in out
