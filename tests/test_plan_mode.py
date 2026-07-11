"""Plan mode: plan generation, plan-file parsing, and the proceed-to-execute path."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from shamsu.agents.plan_mode import PlanningWorkflow
from shamsu.plans.store import list_plan_ids, new_plan_id, parse_plan_steps, write_plan


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False)


# --- plan file parsing --------------------------------------------------------

def test_parse_plan_steps_numbered_and_bulleted():
    md = "## Steps\n1. a\n2) b\n- c\n* d\n\n## Verification\nx"
    assert parse_plan_steps(md) == ["a", "b", "c", "d"]


def test_parse_plan_steps_only_within_steps_heading():
    md = "## Overview\n1. not a step\n\n## Steps\n1. real step\n\n## Notes\n2. also not"
    assert parse_plan_steps(md) == ["real step"]


def test_parse_plan_steps_empty_when_no_steps_section():
    assert parse_plan_steps("# Plan\n\nsome prose, no steps heading") == []


# --- plan generation ----------------------------------------------------------

class _StubLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    async def generate_structured(self, role, system, prompt, schema, **kwargs):
        return json.dumps(self.payload)


class _StubMemory:
    def render_relevant(self, *args, **kwargs):
        return ""


@pytest.mark.asyncio
async def test_planning_workflow_writes_file_and_returns_steps(tmp_path: Path):
    payload = {
        "title": "Add dark mode",
        "context": "toggle lives in settings",
        "files": ["settings.html", "settings.js"],
        "steps": [
            {"description": "Add a toggle button", "target_file": "settings.html"},
            {"description": "Persist the preference", "target_file": "settings.js"},
        ],
        "verification": "npm run build",
    }
    workflow = PlanningWorkflow(
        tmp_path, llm=_StubLLM(payload), search=None, memory_service=_StubMemory()
    )
    plan = await workflow.run("add a dark-mode toggle", route="code_edit")

    assert plan.steps == ["Add a toggle button", "Persist the preference"]
    assert plan.path.exists()
    assert plan.path.parent == (tmp_path / ".shamsu" / "plans")
    assert "## Steps" in plan.markdown
    assert "Add dark mode" in plan.markdown
    # The rendered file round-trips through the same parser the proceed path uses.
    assert parse_plan_steps(plan.path.read_text(encoding="utf-8")) == [
        "Add a toggle button (`settings.html`)",
        "Persist the preference (`settings.js`)",
    ]
    assert list_plan_ids(tmp_path) == [plan.plan_id]


@pytest.mark.asyncio
async def test_planning_workflow_survives_garbled_model_output(tmp_path: Path):
    class _GarbageLLM:
        async def generate_structured(self, *a, **k):
            return "not json at all"

    workflow = PlanningWorkflow(
        tmp_path, llm=_GarbageLLM(), search=None, memory_service=_StubMemory()
    )
    plan = await workflow.run("do something", route="code_edit")
    # No crash: a plan file is still written so the user can edit it by hand.
    assert plan.path.exists()
    assert "## Steps" in plan.markdown


# --- proceed -> execute -------------------------------------------------------

@pytest.mark.asyncio
async def test_execute_pending_plan_runs_each_step_as_agent_pass(tmp_path: Path, monkeypatch):
    from shamsu.cli import repl
    from shamsu.tasks.state import list_task_ids

    md = "# Plan: X\n\n## Steps\n1. build the entities\n2. wire the game loop\n\n## Verification\nnpm run build\n"
    plan_id = new_plan_id()
    write_plan(tmp_path, plan_id, md)

    calls: list[str] = []

    async def fake_run_agent_chat(user_input, workspace, console, session_logger=None, **kwargs):
        calls.append(user_input)

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)

    pending = {
        "type": "plan",
        "awaiting": "plan_approval",
        "plan_id": plan_id,
        "route": "code_edit",
        "created_from_prompt": "build the game",
    }
    await repl._execute_pending_plan(pending, tmp_path, _console(), session_logger=None)

    assert len(calls) == 2
    assert "build the entities" in calls[0]
    assert "wire the game loop" in calls[1]
    # Execution is tracked as a MilestoneTask for `/tasks` visibility.
    assert list_task_ids(tmp_path)


@pytest.mark.asyncio
async def test_execute_pending_plan_single_pass_when_no_steps(tmp_path: Path, monkeypatch):
    from shamsu.cli import repl

    plan_id = new_plan_id()
    write_plan(tmp_path, plan_id, "# Plan\n\njust prose, no steps section\n")

    calls: list[str] = []

    async def fake_run_agent_chat(user_input, workspace, console, session_logger=None, **kwargs):
        calls.append(user_input)

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)

    pending = {"awaiting": "plan_approval", "plan_id": plan_id, "route": "code_edit",
               "created_from_prompt": "task"}
    await repl._execute_pending_plan(pending, tmp_path, _console(), session_logger=None)

    assert len(calls) == 1  # single agent pass over the whole plan


# --- glue: plan stores a pending approval; proceed consumes it ----------------

@pytest.mark.asyncio
async def test_handle_plan_stores_pending_approval(tmp_path: Path, monkeypatch):
    from shamsu.agents.plan_mode import PlanDoc
    from shamsu.cli import repl
    from shamsu.plans.store import new_plan_id, parse_plan_steps, read_plan, write_plan
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session("Plan")
    md = "# Plan: T\n\n## Steps\n1. do thing one\n2. do thing two\n\n## Verification\nok\n"

    class _StubWorkflow:
        def __init__(self, workspace, **kwargs):
            self.workspace = workspace

        async def run(self, task, route="code_edit"):
            plan_id = new_plan_id()
            path = write_plan(self.workspace, plan_id, md)
            return PlanDoc(
                plan_id=plan_id, path=path, markdown=md, title="T", route=route,
                steps=["do thing one", "do thing two"],
            )

    async def _fixed_route(task, workspace, llm):
        return "code_edit"

    monkeypatch.setattr(repl, "PlanningWorkflow", _StubWorkflow)
    monkeypatch.setattr(repl, "_resolve_plan_route", _fixed_route)

    await repl._handle_plan("build a thing", tmp_path, _console(), session_logger=logger)

    pending = logger.get_pending_action()
    assert pending.get("awaiting") == "plan_approval"
    assert pending.get("route") == "code_edit"
    assert pending.get("created_from_prompt") == "build a thing"
    assert pending.get("plan_id")
    # The proceed path can read/parse the exact file this plan referenced.
    assert parse_plan_steps(read_plan(tmp_path, pending["plan_id"])) == [
        "do thing one", "do thing two"
    ]


def test_resolve_proceed_false_when_nothing_pending(tmp_path: Path):
    from shamsu.cli import repl
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session("Empty")
    assert repl._resolve_proceed(tmp_path, _console(), logger) is False


# --- command routing ----------------------------------------------------------

def test_command_router_accepts_plan_and_proceed():
    from shamsu.cli.command_router import CommandRouter
    from shamsu.cli.repl import SYSTEM_COMMANDS

    router = CommandRouter(SYSTEM_COMMANDS)
    assert router.route("/plan add a feature").valid
    assert router.route("/proceed").valid
