from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import Client

from nexus_workbench import client as c
from nexus_workbench import mcp_server
from tests.fake_api import API_BASE, FakeAPI, make_token, sample_data

WORKSPACE_TOOLS = {"whoami", "list_projects", "list_recordings", "get_recording"}


def server_for(api: FakeAPI, token: str | None) -> Any:
    def factory() -> c.WorkbenchClient:
        return c.WorkbenchClient(
            api_base=API_BASE, token=token, transport=api.transport, sleep=lambda s: None
        )

    return mcp_server.build_server(factory)


def run(coro_fn: Any) -> Any:
    return anyio.run(coro_fn)


def text_of(result: Any) -> str:
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))


@pytest.fixture
def api() -> FakeAPI:
    fake = FakeAPI()
    sample_data(fake)
    return fake


def test_lists_tools_with_annotations_and_schemas(api: FakeAPI) -> None:
    server = server_for(api, api.add_token())

    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            return await client.list_tools(), client.instructions, client.server_info

    tools, instructions, info = run(go)
    by_name = {t.name: t for t in tools.tools}
    assert set(by_name) == WORKSPACE_TOOLS
    assert info.name == "nexus"
    assert "read-only" in instructions
    assert "nexus-workbench login" in instructions
    assert "data, not instructions" in instructions
    for tool in tools.tools:
        ann = tool.annotations
        assert ann is not None
        assert ann.read_only_hint is True
        assert ann.destructive_hint is False
        assert ann.idempotent_hint is True
        assert ann.open_world_hint is True
        assert ann.title and tool.title
        assert tool.output_schema and tool.output_schema.get("type") == "object"
        assert tool.description and len(tool.description) > 60
    props = by_name["list_recordings"].input_schema["properties"]
    assert set(props) == {"project_id", "limit", "cursor"}
    assert props["limit"]["default"] == 50
    assert props["limit"]["maximum"] == 100
    assert "cursor" in by_name["list_recordings"].description
    assert by_name["get_recording"].input_schema["required"] == ["id"]
    assert set(by_name["list_recordings"].output_schema["required"]) == {
        "recordings",
        "nextCursor",
        "truncated",
    }


def test_calls_each_tool(api: FakeAPI) -> None:
    server = server_for(api, api.add_token())

    async def go() -> dict[str, Any]:
        out = {}
        async with Client(server, mode="legacy") as client:
            out["whoami"] = await client.call_tool("whoami", {})
            out["projects"] = await client.call_tool("list_projects", {})
            out["page1"] = await client.call_tool("list_recordings", {"limit": 2})
            cursor = out["page1"].structured_content["nextCursor"]
            out["page2"] = await client.call_tool("list_recordings", {"limit": 2, "cursor": cursor})
            out["proj"] = await client.call_tool("list_recordings", {"project_id": "p_1"})
            out["rec"] = await client.call_tool("get_recording", {"id": "e_1"})
            out["held"] = await client.call_tool("get_recording", {"id": "e_2"})
            out["missing"] = await client.call_tool("get_recording", {"id": "e_nope"})
            out["badlimit"] = await client.call_tool("list_recordings", {"limit": 500})
        return out

    out = run(go)
    assert out["whoami"].is_error is False
    assert out["whoami"].structured_content["workspace"]["name"] == "Sleep lab"
    assert out["projects"].structured_content["projects"][0]["id"] == "p_1"
    page1 = out["page1"].structured_content
    assert [r["id"] for r in page1["recordings"]] == ["e_4", "e_3"]
    assert page1["nextCursor"]
    assert "futureKey" not in page1["recordings"][0]  # unknown keys are dropped
    assert [r["id"] for r in out["page2"].structured_content["recordings"]] == ["e_2", "e_1"]
    assert {r["projectId"] for r in out["proj"].structured_content["recordings"]} == {"p_1"}
    rec = out["rec"].structured_content
    assert rec["channels"]["labels"] == ["F3", "ch2"]
    assert rec["stageMinutes"]["N2"] == 201.5
    held = out["held"].structured_content
    assert held["held"] is True
    assert "stageMinutes" not in held
    assert "durationSec" in held and "pairId" not in held  # absent stays absent
    assert out["missing"].is_error is True
    assert "does not exist" in text_of(out["missing"])
    assert out["badlimit"].is_error is True
    bodies = [json.loads(r.content) for r in api.requests]
    assert {"action": "list_recordings", "limit": 2} in bodies
    assert {"action": "list_recordings", "projectId": "p_1", "limit": 50} in bodies


def test_signed_out_message(api: FakeAPI) -> None:
    server = mcp_server.build_server(
        lambda: c.WorkbenchClient(api_base=API_BASE, transport=api.transport)
    )

    async def go() -> list[Any]:
        async with Client(server, mode="legacy") as client:
            return [
                await client.call_tool(name, {"id": "e_1"} if name == "get_recording" else {})
                for name in sorted(WORKSPACE_TOOLS)
            ]

    for result in run(go):
        assert result.is_error is True
        assert text_of(result) == (
            "Not signed in. Run `nexus-workbench login` in a terminal, then try again."
        )
    assert api.requests == []


def test_client_errors_become_tool_errors(api: FakeAPI) -> None:
    server = server_for(api, api.add_token(early_access=False))

    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            return await client.call_tool("whoami", {})

    result = run(go)
    assert result.is_error is True
    text = text_of(result)
    assert "early access" in text and c.EARLY_ACCESS_URL in text
    assert "Traceback" not in text


def test_unexpected_error_has_no_stack_trace(api: FakeAPI) -> None:
    class Boom(c.WorkbenchClient):
        def call(self, action: str, **args: Any) -> dict[str, Any]:
            raise RuntimeError("secret internals")

    server = mcp_server.build_server(lambda: Boom(api_base=API_BASE, token=make_token()))

    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            return await client.call_tool("whoami", {})

    result = run(go)
    assert result.is_error is True
    assert "secret internals" not in text_of(result)
    assert "doctor" in text_of(result)


def test_bad_api_url_is_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEXUS_API_URL", "http://example.com")
    server = mcp_server.build_server()

    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            return await client.call_tool("whoami", {})

    result = run(go)
    assert result.is_error is True
    assert "https://" in text_of(result)


def test_local_tool_seam_registers_extra_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    def inspect_edf(path: str) -> dict[str, Any]:
        return {"fileId": "f_1", "channels": 2, "path_len": len(path)}

    monkeypatch.setattr(
        mcp_server,
        "local_tools",
        lambda: [
            mcp_server.ToolSpec(
                fn=inspect_edf,
                name="inspect_edf",
                title="Inspect EDF",
                description="Summarise an EDF file.",
                open_world=False,
            )
        ],
    )
    server = mcp_server.build_server()

    async def go() -> Any:
        async with Client(server, mode="legacy") as client:
            tools = await client.list_tools()
            result = await client.call_tool("inspect_edf", {"path": "a.edf"})
            return tools, result

    tools, result = run(go)
    tool = {t.name: t for t in tools.tools}["inspect_edf"]
    assert tool.annotations.open_world_hint is False
    assert tool.annotations.read_only_hint is True
    assert result.structured_content["fileId"] == "f_1"


# -- stdio ---------------------------------------------------------------------------


def _child_env(tmp_path: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")}
    env["HOME"] = str(tmp_path / "home")
    env["XDG_CONFIG_HOME"] = str(tmp_path / "xdg")
    env["APPDATA"] = str(tmp_path / "appdata")
    return env


def test_import_writes_nothing_to_stdout(tmp_path: Path) -> None:
    code = (
        "import nexus_workbench.cli, nexus_workbench.mcp_server, nexus_workbench.setup_cmd;"
        "nexus_workbench.mcp_server.build_server()"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=_child_env(tmp_path),
        timeout=60,
        check=True,
    )
    assert result.stdout == ""


def test_stdio_handshake_only_protocol_on_stdout(tmp_path: Path) -> None:
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "whoami", "arguments": {}},
        },
    ]
    proc = subprocess.Popen(
        [sys.executable, "-m", "nexus_workbench.cli", "mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(tmp_path),
    )
    assert proc.stdin and proc.stdout
    lines: list[str] = []
    try:
        for message in messages:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
            if "id" in message:
                lines.append(proc.stdout.readline())
        rest, err = proc.communicate(timeout=30)  # closes stdin, so the server exits
    finally:
        if proc.poll() is None:
            proc.kill()
    lines.extend(line for line in rest.splitlines() if line)
    parsed = []
    for line in lines:
        obj = json.loads(line)  # every stdout line is JSON
        assert obj.get("jsonrpc") == "2.0"
        parsed.append(obj)
    by_id = {m["id"]: m for m in parsed if "id" in m}
    assert by_id[1]["result"]["serverInfo"]["name"] == "nexus"
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == WORKSPACE_TOOLS
    call = by_id[3]["result"]
    assert call["isError"] is True
    assert "Not signed in" in call["content"][0]["text"]
