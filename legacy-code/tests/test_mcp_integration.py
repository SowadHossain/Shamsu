from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from shamsu.action_ledger.ledger import start_run
from shamsu.action_ledger.store import load_mutations, load_tool_calls
from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.mcp.config import (
    MCPConfig,
    MCPServerConfig,
    load_mcp_config,
    resolved_server_arguments,
    resolved_server_command,
    resolved_server_cwd,
    resolved_server_url,
)
from shamsu.mcp.manager import MCPManager, _Connection
from shamsu.mcp.types import MCPTool
from shamsu.session.manager import SessionManager
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import LLMResponse


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"


class _ScriptedClient:
    def __init__(self) -> None:
        self.calls = 0
        self.tool_names: set[str] = set()

    async def chat(self, model, messages, tools, stream, options):  # noqa: ANN001
        self.tool_names.update((item.get("function") or {}).get("name", "") for item in tools)
        self.calls += 1
        if self.calls == 1:
            return {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "mcp-call-1",
                            "function": {
                                "name": "mcp__fixture__echo",
                                "arguments": {"message": "from-agent"},
                            },
                        }
                    ],
                }
            }
        return {"message": {"content": "The external MCP returned from-agent.", "tool_calls": []}}


class _NoPlanLLM:
    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        return LLMResponse(raw="", model_used="fake")


@contextmanager
def _remote_server(transport: str):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    env = os.environ.copy()
    env.update({"MCP_FIXTURE_TRANSPORT": transport, "MCP_FIXTURE_PORT": str(port)})
    process = subprocess.Popen(
        [sys.executable, str(FIXTURE_SERVER)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(f"fixture MCP server exited with {process.returncode}")
            with socket.socket() as client:
                client.settimeout(0.1)
                if client.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        else:
            raise RuntimeError("fixture MCP server did not start")
        yield port
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _config(*, approval: str = "always") -> MCPConfig:
    return MCPConfig(
        mcpServers={
            "fixture": MCPServerConfig(
                command=sys.executable,
                args=[str(FIXTURE_SERVER)],
                env={"MCP_FIXTURE_VALUE": "connected"},
                approval=approval,
                trust_tool_annotations=True,
                timeout=15,
            )
        }
    )


def test_loads_claude_compatible_workspace_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TEST_MCP_TOKEN", "secret-value")
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "remote": {
                        "type": "http",
                        "url": "https://example.invalid/mcp",
                        "headers": {"Authorization": "Bearer ${TEST_MCP_TOKEN}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_mcp_config(tmp_path)

    assert config.errors == []
    assert config.servers["remote"].transport == "http"
    assert config.sources == [str(tmp_path / ".mcp.json")]


def test_loads_camel_case_mcp_permission_settings(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local": {
                        "command": "example",
                        "toolPermissions": {"read_file": "allow"},
                        "readOnlyTools": ["read_file"],
                        "trustToolAnnotations": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    server = load_mcp_config(tmp_path).servers["local"]

    assert server.approval == "writes"
    assert server.tool_permissions == {"read_file": "allow"}
    assert server.read_only_tools == ["read_file"]
    assert server.trust_tool_annotations is True


def test_expands_environment_references_in_all_transport_fields(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MCP_TEST_COMMAND", sys.executable)
    monkeypatch.setenv("MCP_TEST_ARG", str(FIXTURE_SERVER))
    monkeypatch.setenv("MCP_TEST_CWD", str(tmp_path))
    monkeypatch.setenv("MCP_TEST_URL", "https://example.invalid/mcp")
    stdio = MCPServerConfig(
        command="${MCP_TEST_COMMAND}",
        args=["${MCP_TEST_ARG}"],
        cwd="${MCP_TEST_CWD}",
    )
    remote = MCPServerConfig(type="http", url="${MCP_TEST_URL}")

    assert resolved_server_command(stdio) == sys.executable
    assert resolved_server_arguments(stdio) == [str(FIXTURE_SERVER)]
    assert resolved_server_cwd(stdio, tmp_path) == tmp_path.resolve()
    assert resolved_server_url(remote) == "https://example.invalid/mcp"


def test_real_stdio_server_accepts_environment_references_in_args(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("MCP_FIXTURE_SCRIPT", str(FIXTURE_SERVER))
    config = MCPConfig(
        mcpServers={
            "fixture": MCPServerConfig(
                command=sys.executable,
                args=["${MCP_FIXTURE_SCRIPT}"],
                approval="never",
                timeout=15,
            )
        }
    )
    manager = MCPManager(tmp_path, config=config)
    try:
        assert "mcp__fixture__echo" in {tool.model_name for tool in manager.tools()}
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_mcp_client_advertises_workspace_root(tmp_path: Path) -> None:
    connection = _Connection("fixture", _config().servers["fixture"], tmp_path.resolve())

    result = await connection._list_roots(None)

    assert len(result.roots) == 1
    assert str(result.roots[0].uri).rstrip("/") == tmp_path.resolve().as_uri()
    assert result.roots[0].name == tmp_path.name


def test_real_stdio_server_discovery_and_call(tmp_path: Path) -> None:
    logger = SessionManager(tmp_path).create_session("MCP integration")
    manager = MCPManager(tmp_path, config=_config(), session_logger=logger)
    try:
        tools = manager.tools()
        names = {tool.model_name for tool in tools}
        assert "mcp__fixture__echo" in names
        assert "mcp__fixture__external_write" in names
        echo = next(tool for tool in tools if tool.name == "echo")
        assert echo.read_only is True

        result = manager.call("mcp__fixture__echo", {"message": "hello"})
        assert result["isError"] is False
        assert result["structuredContent"] == {
            "message": "hello",
            "fixture_env": "connected",
        }
    finally:
        manager.close()


def test_real_streamable_http_server_discovery_and_call(tmp_path: Path) -> None:
    with _remote_server("streamable-http") as port:
        config = MCPConfig(
            mcpServers={
                "remote": MCPServerConfig(
                    type="http",
                    url=f"http://127.0.0.1:{port}/mcp",
                    approval="never",
                    timeout=15,
                )
            }
        )
        manager = MCPManager(tmp_path, config=config)
        try:
            assert {tool.name for tool in manager.tools()} == {"echo", "external_write", "fail"}
            result = manager.call("mcp__remote__echo", {"message": "over-http"})
            assert result["structuredContent"]["message"] == "over-http"
        finally:
            manager.close()


def test_real_legacy_sse_server_discovery_and_call(tmp_path: Path) -> None:
    with _remote_server("sse") as port:
        config = MCPConfig(
            mcpServers={
                "legacy": MCPServerConfig(
                    type="sse",
                    url=f"http://127.0.0.1:{port}/sse",
                    approval="never",
                    timeout=15,
                )
            }
        )
        manager = MCPManager(tmp_path, config=config)
        try:
            result = manager.call("mcp__legacy__echo", {"message": "over-sse"})
            assert result["structuredContent"]["message"] == "over-sse"
        finally:
            manager.close()


def test_registry_approval_and_server_error(tmp_path: Path) -> None:
    approvals = []
    manager = MCPManager(tmp_path, config=_config())
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda request: approvals.append(request) or True,
        mcp_manager=manager,
    )
    try:
        result = registry.execute("mcp__fixture__echo", {"message": "approved"})
        assert result.ok is True
        assert result.data["mcp_server"] == "fixture"
        assert approvals[0].action_type == "mcp_tool"

        failed = registry.execute("mcp__fixture__fail", {})
        assert failed.ok is False
        assert failed.data["is_error"] is True
    finally:
        manager.close()


@pytest.mark.asyncio
async def test_normal_agent_loop_can_select_and_log_mcp_tool(tmp_path: Path) -> None:
    manager = MCPManager(tmp_path, config=_config())
    ledger = start_run(tmp_path, "Ask the external MCP to echo from-agent")
    tools = AgentToolRegistry(
        tmp_path,
        approval_func=lambda _request: True,
        action_ledger=ledger,
        mcp_manager=manager,
    )
    client = _ScriptedClient()
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=tools,
        llm=_NoPlanLLM(),
        action_ledger=ledger,
        use_planner=False,
    )
    try:
        result = await loop.run("Ask the external MCP to echo from-agent")
        assert "mcp__fixture__echo" in client.tool_names
        assert "from-agent" in result.final
        records = load_tool_calls(tmp_path, ledger.run_id)
        assert any(
            record["tool"] == "mcp__fixture__echo" and record["phase"] == "finished"
            for record in records
        )
    finally:
        manager.close()


def test_registry_denial_and_read_only_request_do_not_call_server(tmp_path: Path) -> None:
    manager = MCPManager(tmp_path, config=_config())
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda _request: False,
        mcp_manager=manager,
    )
    try:
        denied = registry.execute("mcp__fixture__echo", {"message": "no"})
        assert denied.ok is False
        assert "denied" in denied.message.lower()

        registry.set_read_only(True)
        blocked = registry.execute("mcp__fixture__external_write", {"value": "no"})
        assert blocked.ok is False
        assert blocked.data["read_only_request"] is True
    finally:
        manager.close()


def test_scoped_write_restriction_applies_to_external_mcp_tools(tmp_path: Path) -> None:
    manager = MCPManager(tmp_path, config=_config(approval="never"))
    registry = AgentToolRegistry(tmp_path, mcp_manager=manager)
    registry.set_allowed_write_paths(["notes.md"])
    try:
        assert registry._outside_allowed_scope(str(tmp_path / "notes.md")) is None

        blocked = registry.execute("mcp__fixture__external_write", {"value": "no path"})

        assert blocked.ok is False
        assert blocked.data["scoped_read_only"] is True
        assert "could not prove which path" in blocked.message
    finally:
        manager.close()


def test_mcp_workspace_write_creates_a_mutation_transaction(tmp_path: Path) -> None:
    class WritingManager:
        def __init__(self) -> None:
            self.config = MCPConfig(
                mcpServers={
                    "writer": MCPServerConfig(command="unused", approval="never")
                }
            )
            self.tool = MCPTool(
                server="writer",
                name="write_file",
                description="Write a file",
                input_schema={"type": "object"},
            )

        def get_tool(self, name):
            return self.tool if name == self.tool.model_name else None

        def tool_schemas(self):
            return [self.tool.to_ollama_schema()]

        def call(self, _name, arguments):
            Path(arguments["path"]).write_text(arguments["content"], encoding="utf-8")
            return {"isError": False, "content": [{"type": "text", "text": "written"}]}

    ledger = start_run(tmp_path, "write through MCP")
    manager = WritingManager()
    registry = AgentToolRegistry(tmp_path, action_ledger=ledger, mcp_manager=manager)
    target = tmp_path / "mcp-written.txt"

    result = registry.execute(
        "mcp__writer__write_file",
        {"path": str(target), "content": "hello"},
    )

    mutations = load_mutations(tmp_path, ledger.run_id)
    assert result.ok is True
    assert result.data["touched_files"] == ["mcp-written.txt"]
    assert result.data["transaction_id"]
    assert mutations[0]["status"] == "applied"
    assert mutations[0]["touched_files"] == ["mcp-written.txt"]


def test_action_ledger_redacts_secret_argument_keys(tmp_path: Path) -> None:
    ledger = start_run(tmp_path, "Use external MCP")
    call_id = ledger.log_tool_call(
        "mcp__fixture__echo",
        {"token": "plain-secret", "nested": {"client_secret": "also-secret"}},
    )
    ledger.log_tool_result(call_id, "mcp__fixture__echo", True, "done", {})
    ledger.finish("done")

    called = next(item for item in load_tool_calls(tmp_path, ledger.run_id) if item["phase"] == "called")
    assert called["arguments"]["token"] == "[REDACTED]"
    assert called["arguments"]["nested"]["client_secret"] == "[REDACTED]"


def test_bad_server_is_reported_without_crashing_other_config(tmp_path: Path) -> None:
    config = _config()
    config.servers["broken"] = MCPServerConfig(
        command="definitely-not-a-real-mcp-command",
        timeout=1,
    )
    manager = MCPManager(tmp_path, config=config)
    try:
        statuses = {status.name: status for status in manager.statuses()}
        assert statuses["fixture"].connected is True
        assert statuses["broken"].connected is False
        assert statuses["broken"].error
    finally:
        manager.close()
