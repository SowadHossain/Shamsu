from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.context.compiler import ContextCompiler
from shamsu.runtime.task_state import (
    EvidenceType,
    ExecutionPlan,
    PlanStep,
    RuntimeStateStore,
)
from shamsu.types import RunStatus


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _store_with_plan(tmp_path: Path) -> RuntimeStateStore:
    store = RuntimeStateStore(tmp_path, db_path=tmp_path / "state.db")
    task = store.create_task(
        run_id="run-context",
        task_id="task-context",
        user_request="update app.py to return 2",
        project_id="demo",
    )
    task.status = RunStatus.RUNNING
    task.last_tool_call = {"name": "file.read", "arguments": {"filepath": "app.py"}}
    task.last_tool_result = {
        "tool": "file.read",
        "ok": True,
        "message": "Read file.",
        "data": {"filepath": "app.py", "content": "def value(): return 1"},
    }
    store.save_task(task, checkpoint_kind="started")
    store.save_execution_plan(
        ExecutionPlan(
            plan_id="plan-context",
            task_id="task-context",
            run_id="run-context",
            title="Update app",
            summary="Read app.py, patch it, verify.",
            steps=[
                PlanStep(
                    step_id="edit",
                    title="Patch app.py",
                    goal="Change value() to return 2 in app.py",
                    inputs=["app.py"],
                    expected_outputs=["app.py returns 2"],
                    allowed_tools=["file.read", "file.patch", "test.run"],
                    acceptance_criteria=["app.py contains return 2"],
                    required_evidence=[EvidenceType.FILE_CHANGED.value],
                    approval_required=True,
                )
            ],
        ),
        valid_tool_names={"file.read", "file.patch", "test.run"},
    )
    store.current_active_step("task-context")
    store.record_successful_step(
        "task-context",
        step_id="observation",
        tool_call={"name": "file.read", "arguments": {"filepath": "app.py"}},
        tool_result={"tool": "file.read", "ok": True, "message": "Read file.", "data": {}},
        changed_files=[],
    )
    return store


@pytest.mark.asyncio
async def test_context_compiler_builds_state_frame_without_chat_history(tmp_path: Path):
    (tmp_path / "app.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    store = _store_with_plan(tmp_path)
    traces: list[tuple[str, dict]] = []
    compiler = ContextCompiler(
        store=store,
        task_id_getter=lambda: "task-context",
        workspace_root=tmp_path,
        system_prompt_getter=lambda: "SYSTEM PROMPT",
        allowed_tools_getter=lambda: [_schema("file.read"), _schema("file.patch"), _schema("test.run")],
        trace=lambda event, _message, data: traces.append((event, data or {})),
    )

    messages = await compiler.compile(6000, [])

    assert [message["role"] for message in messages] == ["system", "user"]
    frame = messages[1]["content"]
    assert "[PHASE]" in frame
    assert "[CURRENT TASK]" in frame
    assert "[CURRENT STEP]" in frame
    assert "[ACCEPTANCE CRITERIA]" in frame
    assert "[RELEVANT SOURCE CODE]" in frame
    assert "def value()" in frame
    assert "app.py contains return 2" in frame
    assert "file.patch" in frame
    assert "Read file." in frame
    assert "old assistant chatter" not in frame
    assert traces[0][0] == "context.compiled"


@pytest.mark.asyncio
async def test_context_compiler_uses_latest_written_files_as_hot_source(tmp_path: Path):
    (tmp_path / "generated.py").write_text("VALUE = 42\n", encoding="utf-8")
    store = RuntimeStateStore(tmp_path, db_path=tmp_path / "state.db")
    task = store.create_task(
        run_id="run-context",
        task_id="task-context",
        user_request="continue the generated.py task",
        project_id="demo",
    )
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    compiler = ContextCompiler(
        store=store,
        task_id_getter=lambda: "task-context",
        workspace_root=tmp_path,
        system_prompt_getter=lambda: "SYSTEM",
        allowed_tools_getter=lambda: [_schema("file.read")],
    )

    messages = await compiler.compile(5000, ["generated.py"])

    assert "VALUE = 42" in messages[1]["content"]
    assert "[COMPLETED STEP SUMMARY]" in messages[1]["content"]
