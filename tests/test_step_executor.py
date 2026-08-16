from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.agents.executor import StepExecutionController, StepExecutionDecision, StepExecutionLimits
from shamsu.runtime.failures import FailureType
from shamsu.runtime.task_state import RuntimeStateStore
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import RunStatus


class NoPlanLLM:
    pass


def _tool_response(name: str, arguments: dict) -> dict:
    return {"function": {"name": name, "arguments": arguments}}


def _final_response(content: str = "done") -> dict:
    return {"message": {"content": content, "tool_calls": []}}


class MultiToolClient:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return {
                "message": {
                    "content": "",
                    "tool_calls": [
                        _tool_response("file.read", {"filepath": "a.py"}),
                        _tool_response("file.read", {"filepath": "b.py"}),
                    ],
                }
            }
        return _final_response("observed")


class FailingReadClient:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, **_kwargs):
        self.calls += 1
        return {
            "message": {
                "content": "",
                "tool_calls": [
                    _tool_response("file.read", {"filepath": f"missing-{self.calls}.py"})
                ],
            }
        }


def _loop(tmp_path: Path, client, *, run_id: str) -> AgentChatLoop:
    return AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        llm=NoPlanLLM(),
        use_planner=False,
        use_long_term_memory=False,
        hydrate_history=False,
        run_id=run_id,
        max_runtime_seconds=30,
    )


def test_step_execution_controller_blocks_after_failure_budget():
    controller = StepExecutionController(limits=StepExecutionLimits(max_consecutive_failures=2))

    assert controller.before_model_decision().decision == StepExecutionDecision.CONTINUE
    assert controller.note_failure().decision == StepExecutionDecision.CONTINUE
    assert controller.note_failure().decision == StepExecutionDecision.BLOCK


@pytest.mark.asyncio
async def test_model_decision_executes_only_one_logical_action(tmp_path: Path):
    (tmp_path / "a.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("B = 1\n", encoding="utf-8")
    loop = _loop(tmp_path, MultiToolClient(), run_id="single-action")

    result = await loop.run("inspect a.py and b.py")

    assert result.status == RunStatus.COMPLETED
    task = RuntimeStateStore(tmp_path).require_task(result.task_id)
    assert task.action_count == 1
    assert task.last_tool_call["arguments"]["filepath"] == "a.py"


@pytest.mark.asyncio
async def test_failing_step_blocks_before_broad_loop_limit(tmp_path: Path):
    client = FailingReadClient()
    loop = _loop(tmp_path, client, run_id="bounded-failure")
    loop.max_tool_rounds = 50

    result = await loop.run("inspect missing files")

    assert result.status == RunStatus.FAILED
    assert "bounded step executor limit" in result.final
    assert client.calls == 3
    store = RuntimeStateStore(tmp_path)
    task = store.require_task(result.task_id)
    assert task.status == RunStatus.FAILED
    assert store.list_failures(result.task_id, FailureType.UNKNOWN_FAILURE)
