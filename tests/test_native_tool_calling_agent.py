from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shamsu.agents.tool_calling_loop import ToolCallingAgentLoop
from shamsu.runtime.run_control import add_feedback, cancel_run, get_run_status
from shamsu.tools.registry import ToolRegistry
from shamsu.types import RunStatus


class SequenceLLM:
    def __init__(self, responses: list[dict]) -> None:
        self.responses = responses
        self.messages_seen: list[list[dict]] = []

    async def chat_with_tools(self, model, messages, tools, **_kwargs):
        self.messages_seen.append(list(messages))
        return self.responses.pop(0)


def _tool_response(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    return {
        "message": {
            "content": "",
            "tool_calls": [{"id": call_id, "function": {"name": name, "arguments": arguments}}],
        }
    }


def _final_response(content: str = "done") -> dict:
    return {"message": {"content": content, "tool_calls": []}}


@pytest.mark.asyncio
async def test_unknown_tool_rejected(tmp_path: Path):
    llm = SequenceLLM([_tool_response("search_code", {"query": "x"}), _final_response("stopped")])
    loop = ToolCallingAgentLoop(tmp_path, llm=llm, max_tool_iterations=3)

    result = await loop.run("try a tool")

    assert result.status == RunStatus.COMPLETED
    tool_messages = [m for m in llm.messages_seen[-1] if m.get("role") == "tool"]
    assert "Unknown tool: search_code" in tool_messages[0]["content"]


@pytest.mark.asyncio
async def test_invalid_args_rejected(tmp_path: Path):
    llm = SequenceLLM([_tool_response("run_safe_command", {"command": 123}), _final_response("stopped")])
    loop = ToolCallingAgentLoop(tmp_path, llm=llm, max_tool_iterations=3)

    result = await loop.run("run this")

    assert result.status == RunStatus.COMPLETED
    tool_messages = [m for m in llm.messages_seen[-1] if m.get("role") == "tool"]
    assert "Argument command must be a string" in tool_messages[0]["content"]


def test_safe_command_tool_works(tmp_path: Path):
    class FakeCommandRunner:
        last_error_packet = None

        def run(self, command, cwd):
            return 0, f"ran {command} in {cwd}", ""

    registry = ToolRegistry(
        tmp_path,
        approval_func=lambda _request: False,
        command_runner=FakeCommandRunner(),
    )

    result = registry.execute("run_safe_command", {"command": "python -m pytest --version"})

    assert result.ok is True
    assert result.data["exit_code"] == 0
    assert "python -m pytest --version" in result.data["stdout"]


def test_unsafe_command_asks_approval_or_is_blocked(tmp_path: Path):
    approvals = []
    registry = ToolRegistry(tmp_path, approval_func=lambda request: approvals.append(request) or False)

    result = registry.execute("run_safe_command", {"command": "python -c \"print(1)\""})

    assert result.ok is False
    assert result.data["exit_code"] == 126
    assert "arbitrary Python" in result.message
    assert approvals == []


class FeedbackLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.messages_seen: list[list[dict]] = []
        self.calls = 0

    async def chat_with_tools(self, model, messages, tools, **_kwargs):
        self.calls += 1
        self.messages_seen.append(list(messages))
        if self.calls == 1:
            self.started.set()
            await asyncio.sleep(60)
        return _final_response("feedback handled")


@pytest.mark.asyncio
async def test_feedback_queued_during_run_is_injected_before_next_step(tmp_path: Path):
    llm = FeedbackLLM()
    loop = ToolCallingAgentLoop(tmp_path, llm=llm, run_id="feedback-run")

    task = asyncio.create_task(loop.run("start"))
    await llm.started.wait()
    assert add_feedback("feedback-run", "please run tests first") is True
    result = await asyncio.wait_for(task, timeout=5)

    assert result.status == RunStatus.COMPLETED
    assert llm.calls == 2
    user_messages = [m["content"] for m in llm.messages_seen[-1] if m.get("role") == "user"]
    assert any("please run tests first" in content for content in user_messages)


class SlowLLM:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def chat_with_tools(self, model, messages, tools, **_kwargs):
        self.started.set()
        await asyncio.sleep(60)
        return _final_response("too late")


@pytest.mark.asyncio
async def test_cancel_stops_run_cleanly(tmp_path: Path):
    llm = SlowLLM()
    loop = ToolCallingAgentLoop(tmp_path, llm=llm, run_id="cancel-run")

    task = asyncio.create_task(loop.run("start"))
    await llm.started.wait()
    assert cancel_run("cancel-run") is True
    result = await asyncio.wait_for(task, timeout=5)

    assert result.status == RunStatus.CANCELLED
    assert "cancelled" in result.final.lower()
    status = get_run_status("cancel-run")
    assert status is not None
    assert status["status"] == "cancelled"


@pytest.mark.asyncio
async def test_loop_stops_at_max_iterations(tmp_path: Path):
    responses = [_tool_response("unknown_tool", {}) for _ in range(5)]
    llm = SequenceLLM(responses)
    loop = ToolCallingAgentLoop(tmp_path, llm=llm, max_tool_iterations=2)

    result = await loop.run("spin")

    assert result.status == RunStatus.FAILED
    assert result.iterations == 2
    assert "stopped after 2 tool iterations" in result.final
