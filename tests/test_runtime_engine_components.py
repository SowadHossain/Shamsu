from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from shamsu.agents.executor import AgentExecutor, StepExecutionLimits
from shamsu.agents.repair import RepairRecorder
from shamsu.context.compiler import ContextCompiler
from shamsu.runtime.engine import RuntimeEngine
from shamsu.runtime.run_control import ControlledRun, get_run_status
from shamsu.runtime.task_state import RuntimeStateStore
from shamsu.runtime.transitions import apply_completion_gate_failure, status_from_result
from shamsu.tools.dispatcher import ToolDispatcher
from shamsu.types import RunStatus
from shamsu.verification.completion import CompletionCoordinator
from shamsu.verification.verifier import ChangeVerifier


@dataclass
class FakeResult:
    final: str
    stopped: bool = False
    awaiting_user: bool = False
    timeout_category: str | None = None
    run_id: str = ""
    task_id: str = ""
    status: RunStatus = RunStatus.COMPLETED


class FakeAgent:
    def __init__(self, tmp_path: Path, *, result: FakeResult | None = None) -> None:
        self.run_id = "engine-run"
        self.runtime_task_id = "task-engine-run"
        self.session_logger = None
        self.action_ledger = None
        self.max_runtime_seconds = 30
        self.runtime_state_store = RuntimeStateStore(tmp_path, db_path=tmp_path / "state.db")
        self.result = result or FakeResult("done")
        self.executor = AgentExecutor(self._execute)
        self.checkpoints: list[tuple[RunStatus, str, str]] = []

    def _initialize_runtime_task(self, user_input: str, control: ControlledRun) -> Any:
        self.runtime_state_store.create_run(self.run_id, status=RunStatus.RUNNING)
        state = self.runtime_state_store.create_task(
            run_id=self.run_id,
            task_id=self.runtime_task_id,
            user_request=user_input,
            project_id="test",
        )
        state.status = RunStatus.RUNNING
        return self.runtime_state_store.save_task(state, checkpoint_kind="task_started")

    async def _execute(self, _user_input: str, _control: ControlledRun) -> FakeResult:
        await asyncio.sleep(0)
        return self.result

    def _checkpoint_task_status(
        self,
        status: RunStatus,
        phase: str,
        checkpoint_kind: str,
    ) -> None:
        self.checkpoints.append((status, phase, checkpoint_kind))
        self.runtime_state_store.update_task_status(
            self.runtime_task_id,
            status,
            phase=phase,
            checkpoint_kind=checkpoint_kind,
        )

    def _make_terminal_result(
        self,
        final: str,
        status: RunStatus,
        *,
        stopped: bool = True,
    ) -> FakeResult:
        return FakeResult(
            final=final,
            stopped=stopped,
            run_id=self.run_id,
            task_id=self.runtime_task_id,
            status=status,
        )


class RecordingRegistry:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        return {"ok": True, "tool": name, "arguments": arguments}


@pytest.mark.asyncio
async def test_runtime_engine_coordinates_lifecycle_and_completion(tmp_path: Path):
    agent = FakeAgent(tmp_path)

    result = await RuntimeEngine(agent).run("finish")

    assert result.status == RunStatus.COMPLETED
    assert result.run_id == "engine-run"
    assert result.task_id == "task-engine-run"
    assert get_run_status("engine-run")["active"] is False
    assert agent.checkpoints == []


@pytest.mark.asyncio
async def test_runtime_engine_maps_stopped_result_to_failed(tmp_path: Path):
    agent = FakeAgent(tmp_path, result=FakeResult("stopped", stopped=True))

    result = await RuntimeEngine(agent).run("stop")

    assert result.status == RunStatus.FAILED
    assert agent.checkpoints[-1] == (RunStatus.FAILED, "failed", "failed")


def test_transitions_apply_completion_gate_unavailable():
    result = apply_completion_gate_failure(FakeResult("done"), None)

    assert result.stopped is True
    assert "Completion not registered" in result.final
    assert status_from_result(FakeResult("waiting", status=RunStatus.CANCELLED)) == RunStatus.CANCELLED


def test_completion_coordinator_completes_task_without_plan(tmp_path: Path):
    store = RuntimeStateStore(tmp_path, db_path=tmp_path / "state.db")
    store.create_run("run", status=RunStatus.RUNNING)
    task = store.create_task(
        run_id="run",
        task_id="task",
        user_request="finish",
        project_id="test",
    )
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")

    gate = CompletionCoordinator(store, "task").request_completion()

    assert gate is not None
    assert gate.ok is True
    assert store.load_task("task").status == RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_context_compiler_and_executor_are_independently_testable():
    async def compile_messages(token_budget: int, written_files: list[str]):
        return [{"role": "user", "content": f"{token_budget}:{','.join(written_files)}"}]

    async def execute_loop(user_input: str, control: ControlledRun):
        return f"{control.run_id}:{user_input}"

    compiler = ContextCompiler(compile_messages)
    executor = AgentExecutor(execute_loop)
    control = ControlledRun("run-1")

    assert await compiler.compile(100, ["a.py"]) == [{"role": "user", "content": "100:a.py"}]
    assert await executor.execute("hello", control) == "run-1:hello"


@pytest.mark.asyncio
async def test_agent_executor_run_step_delegates_to_bounded_step_runner():
    async def execute_loop(user_input: str, _control: ControlledRun):
        return f"loop:{user_input}"

    async def run_step(step, user_input: str, control: ControlledRun, limits: StepExecutionLimits):
        return f"{step['step_id']}:{control.run_id}:{user_input}:{limits.max_actions_per_step}"

    executor = AgentExecutor(
        execute_loop,
        step_runner=run_step,
        limits=StepExecutionLimits(max_actions_per_step=4),
    )

    result = await executor.run_step({"step_id": "step-1"}, "do it", ControlledRun("run-1"))

    assert result == "step-1:run-1:do it:4"


def test_tool_dispatcher_delegates_to_registry():
    registry = RecordingRegistry()

    result = ToolDispatcher(registry).dispatch("read_file", {"filepath": "README.md"})

    assert result["tool"] == "read_file"
    assert registry.calls == [("read_file", {"filepath": "README.md"})]


def test_repair_recorder_persists_attempt(tmp_path: Path):
    store = RuntimeStateStore(tmp_path, db_path=tmp_path / "state.db")
    store.create_run("run", status=RunStatus.RUNNING)
    store.create_task(run_id="run", task_id="task", user_request="fix", project_id="test")

    RepairRecorder(store, "task").record_attempt(["bug.py"])

    assert store.load_task("task").repair_count == 1


@pytest.mark.asyncio
async def test_change_verifier_wraps_verify_only(monkeypatch, tmp_path: Path):
    calls: list[tuple[Path, list[str], Any, bool, Any]] = []

    class Outcome:
        summary = "ok"

    def fake_verify_only(workspace, files, *, command_runner, lightweight, session_logger):
        calls.append((workspace, files, command_runner, lightweight, session_logger))
        return Outcome()

    monkeypatch.setattr("shamsu.verification.verifier.verify_only", fake_verify_only)

    runner = object()
    outcome = await ChangeVerifier(tmp_path, command_runner=runner).verify(["app.py"])

    assert outcome.summary == "ok"
    assert calls == [(tmp_path, ["app.py"], runner, True, None)]
