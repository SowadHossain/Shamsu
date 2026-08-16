from __future__ import annotations

from pathlib import Path

from shamsu.agents.planner import AgentPlanner
from shamsu.runtime.phase_contracts import ExecutionPhase
from shamsu.runtime.task_state import RuntimeStateStore
from shamsu.types import RunStatus


class RecordingToolPolicy:
    def __init__(self) -> None:
        self.allowed_tools: list[str] = []
        self.phase: ExecutionPhase | None = None
        self.risk = ""

    def set_allowed_tools(self, names: list[str]) -> None:
        self.allowed_tools = list(names)

    def set_phase(self, phase: str | ExecutionPhase | None, *, task_risk: str | None = None) -> None:
        self.phase = phase if isinstance(phase, ExecutionPhase) else ExecutionPhase(str(phase))
        self.risk = str(task_risk or "")


def _store_with_task(tmp_path: Path) -> RuntimeStateStore:
    store = RuntimeStateStore(tmp_path, db_path=tmp_path / "state.db")
    task = store.create_task(run_id="run-plan", task_id="task-plan", user_request="do it")
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    return store


def test_agent_planner_persists_mutating_contract_and_applies_author_phase(tmp_path: Path):
    store = _store_with_task(tmp_path)
    policy = RecordingToolPolicy()
    planner = AgentPlanner(
        store=store,
        tool_policy=policy,
        registered_tool_names={
            "project.inspect",
            "code.search",
            "file.read",
            "file.patch",
            "test.run",
            "git.inspect",
            "git.checkpoint",
        },
        run_id="run-plan",
        task_id="task-plan",
    )

    result = planner.persist_contract("create app.py", "Write the file and verify it.")

    task = store.require_task("task-plan")
    plan = store.load_execution_plan(task.plan_id)
    assert plan is not None
    assert plan.steps[0].required_evidence == ["file_changed"]
    assert policy.phase == ExecutionPhase.AUTHOR
    assert policy.risk == "medium"
    assert "file.patch" in policy.allowed_tools
    assert "write_file" not in policy.allowed_tools
    assert "Runtime execution contract" in result.text


def test_agent_planner_keeps_information_request_read_only(tmp_path: Path):
    store = _store_with_task(tmp_path)
    policy = RecordingToolPolicy()
    planner = AgentPlanner(
        store=store,
        tool_policy=policy,
        registered_tool_names={"read_file", "grep_files", "write_file"},
        run_id="run-plan",
        task_id="task-plan",
    )

    result = planner.persist_contract("what does app.py do?", "Inspect the file and summarize.")

    plan = store.load_task_plan("task-plan")
    assert plan is not None
    assert plan.steps[0].required_evidence == []
    assert policy.phase == ExecutionPhase.EXPLORE
    assert "write_file" not in policy.allowed_tools
    assert result.active_step_id == "step-1"
