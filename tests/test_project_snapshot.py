from __future__ import annotations

import json
from pathlib import Path

import pytest

from shamsu.context.compiler import ContextCompiler
from shamsu.context.project_snapshot import (
    build_project_snapshot,
    load_project_snapshot,
    render_project_invariants,
    render_tech_stack,
)
from shamsu.runtime.phase_contracts import ExecutionPhase
from shamsu.runtime.task_state import EvidenceType, ExecutionPlan, PlanStep, RuntimeStateStore
from shamsu.types import RunStatus


def _schema(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def test_project_snapshot_detects_python_pygame_without_web_stack(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("pygame==2.6.1\npytest\n", encoding="utf-8")
    (tmp_path / "game.py").write_text("import pygame\n\npygame.init()\n", encoding="utf-8")
    (tmp_path / "bullet.py").write_text("class Bullet:\n    pass\n", encoding="utf-8")

    snapshot = load_project_snapshot(tmp_path)

    assert (tmp_path / ".shamsu" / "project" / "context.json").is_file()
    assert snapshot.identity["project_type"] == "standalone Python/Pygame application"
    stack = render_tech_stack(snapshot)
    invariants = render_project_invariants(snapshot)
    assert "Python" in stack
    assert "Pygame" in stack
    assert "web backend" in invariants
    assert "Django" not in stack


def test_project_snapshot_prefers_manifest_database_over_incidental_sqlite(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "demo-web",
                "dependencies": {
                    "react": "^19.0.0",
                    "vite": "^7.0.0",
                    "typescript": "^5.8.0",
                    "vitest": "^3.0.0",
                },
                "scripts": {"dev": "vite", "build": "vite build", "test": "vitest run"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "demo-api"
dependencies = [
    "fastapi",
    "sqlalchemy",
    "psycopg",
    "pytest",
]
""".strip(),
        encoding="utf-8",
    )
    (tmp_path / "compose.yml").write_text(
        "services:\n  db:\n    image: postgres:16\n", encoding="utf-8"
    )
    (tmp_path / "tests.py").write_text("import sqlite3\n", encoding="utf-8")

    snapshot = build_project_snapshot(tmp_path)
    stack = render_tech_stack(snapshot)

    assert snapshot.identity["project_type"] == "full-stack web application"
    assert "React" in stack
    assert "Vite" in stack
    assert "TypeScript" in stack
    assert "FastAPI" in stack
    assert "PostgreSQL" in stack
    assert "SQLAlchemy" in stack
    assert "SQLite" not in stack


@pytest.mark.asyncio
async def test_context_compiler_includes_stack_and_invariants_before_source(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("pygame\npytest\n", encoding="utf-8")
    (tmp_path / "game.py").write_text("VALUE = 1\n" + "x = 1\n" * 2000, encoding="utf-8")
    store = RuntimeStateStore(tmp_path, db_path=tmp_path / "state.db")
    task = store.create_task(
        run_id="run",
        task_id="task",
        user_request="implement bullet collisions in game.py",
        project_id="asteroids",
    )
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    store.save_execution_plan(
        ExecutionPlan(
            plan_id="plan",
            task_id="task",
            run_id="run",
            title="Collisions",
            summary="Patch game.py and verify.",
            steps=[
                PlanStep(
                    step_id="code",
                    title="Implement collisions",
                    goal="Implement bullet collisions in game.py",
                    inputs=["game.py"],
                    expected_outputs=["game.py"],
                    allowed_tools=["file.read", "file.patch"],
                    acceptance_criteria=["bullets remove asteroids"],
                    required_evidence=[EvidenceType.FILE_CHANGED.value],
                )
            ],
        ),
        valid_tool_names={"file.read", "file.patch"},
    )

    compiler = ContextCompiler(
        store=store,
        task_id_getter=lambda: "task",
        workspace_root=tmp_path,
        system_prompt_getter=lambda: "SYSTEM",
        allowed_tools_getter=lambda: [_schema("file.read"), _schema("file.patch")],
    )

    messages = await compiler.compile(5200, ["game.py"])
    frame = messages[1]["content"]

    assert "[PROJECT IDENTITY]" in frame
    assert "[TECH STACK]" in frame
    assert "[PROJECT INVARIANTS]" in frame
    assert "standalone Python/Pygame application" in frame
    assert "Pygame" in frame
    assert "Do not introduce a web backend" in frame
    assert "[RELEVANT SOURCE CODE]" in frame


@pytest.mark.asyncio
async def test_author_context_omits_artifact_brief_but_keeps_code_frame(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("pygame\npytest\n", encoding="utf-8")
    (tmp_path / "game.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = _phase_store(tmp_path, ExecutionPhase.AUTHOR)

    compiler = ContextCompiler(
        store=store,
        task_id_getter=lambda: "task",
        workspace_root=tmp_path,
        system_prompt_getter=lambda: "SYSTEM",
        allowed_tools_getter=lambda: [_schema("file.read"), _schema("file.patch")],
    )

    frame = (await compiler.compile(6000, ["game.py"]))[1]["content"]

    assert "[TECH STACK]" in frame
    assert "[PROJECT INVARIANTS]" in frame
    assert "[RELEVANT SOURCE CODE]" in frame
    assert "[PROJECT FACTS]" not in frame
    assert "[RELEVANT ARTIFACTS]" not in frame
    assert frame.index("[ACCEPTANCE CRITERIA]") < frame.index("[RELEVANT SOURCE CODE]")


@pytest.mark.asyncio
async def test_repair_context_prioritizes_failure_and_changed_source(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("pygame\npytest\n", encoding="utf-8")
    (tmp_path / "game.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = _phase_store(tmp_path, ExecutionPhase.REPAIR)
    task = store.load_task("task")
    assert task is not None
    task.last_tool_result = {
        "tool": "run_command",
        "ok": False,
        "command": "pytest",
        "exit_code": 1,
        "stderr": "AssertionError: asteroid was not removed",
    }
    task.changed_files = ["game.py"]
    store.save_task(task, checkpoint_kind="failed_verify")

    compiler = ContextCompiler(
        store=store,
        task_id_getter=lambda: "task",
        workspace_root=tmp_path,
        system_prompt_getter=lambda: "SYSTEM",
        allowed_tools_getter=lambda: [_schema("file.read"), _schema("file.patch"), _schema("run_command")],
    )

    frame = (await compiler.compile(6000, ["game.py"]))[1]["content"]

    assert "[PROJECT INVARIANTS]" in frame
    assert "[LATEST OBSERVATION]" in frame
    assert "AssertionError: asteroid was not removed" in frame
    assert "[RELEVANT SOURCE CODE]" in frame
    assert "[PROJECT FACTS]" not in frame
    assert "[COMPLETED STEP SUMMARY]" not in frame
    assert frame.index("[LATEST OBSERVATION]") < frame.index("[RELEVANT SOURCE CODE]")


@pytest.mark.asyncio
async def test_verify_context_keeps_evidence_not_full_project_summary(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("pygame\npytest\n", encoding="utf-8")
    (tmp_path / "game.py").write_text("VALUE = 1\n", encoding="utf-8")
    store = _phase_store(tmp_path, ExecutionPhase.VERIFY)
    task = store.load_task("task")
    assert task is not None
    task.last_tool_result = {
        "tool": "run_command",
        "ok": True,
        "command": "pytest",
        "exit_code": 0,
        "stdout": "3 passed",
    }
    store.save_task(task, checkpoint_kind="verify")

    compiler = ContextCompiler(
        store=store,
        task_id_getter=lambda: "task",
        workspace_root=tmp_path,
        system_prompt_getter=lambda: "SYSTEM",
        allowed_tools_getter=lambda: [_schema("file.read"), _schema("run_command")],
    )

    frame = (await compiler.compile(6000, ["game.py"]))[1]["content"]

    assert "[PROJECT INVARIANTS]" in frame
    assert "[ACCEPTANCE CRITERIA]" in frame
    assert "[LATEST OBSERVATION]" in frame
    assert "3 passed" in frame
    assert "[TECH STACK]" not in frame
    assert "[PROJECT FACTS]" not in frame
    assert "[COMPLETED STEP SUMMARY]" not in frame


def _phase_store(tmp_path: Path, phase: ExecutionPhase) -> RuntimeStateStore:
    store = RuntimeStateStore(tmp_path, db_path=tmp_path / "state.db")
    task = store.create_task(
        run_id="run",
        task_id="task",
        user_request="implement bullet collisions in game.py",
        project_id="asteroids",
    )
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    store.save_execution_plan(
        ExecutionPlan(
            plan_id="plan",
            task_id="task",
            run_id="run",
            title="Collisions",
            summary="Patch game.py and verify.",
            steps=[
                PlanStep(
                    step_id="code",
                    title="Implement collisions",
                    goal="Implement bullet collisions in game.py",
                    inputs=["game.py"],
                    expected_outputs=["game.py"],
                    allowed_tools=["file.read", "file.patch", "run_command"],
                    acceptance_criteria=["bullets remove asteroids"],
                    required_evidence=[EvidenceType.FILE_CHANGED.value],
                )
            ],
        ),
        valid_tool_names={"file.read", "file.patch", "run_command"},
    )
    store.current_active_step("task")
    task = store.load_task("task")
    assert task is not None
    task.current_phase = phase.value
    store.save_task(task, checkpoint_kind=f"{phase.value.lower()}_phase")
    return store
