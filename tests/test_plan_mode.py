"""Plan mode: plan generation, plan-file parsing, and the proceed-to-execute path."""
from __future__ import annotations

import asyncio
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


# --- plan MODE (bare `/plan`) -------------------------------------------------


def test_bare_plan_and_plan_off_are_real_commands():
    """Bare `/plan` arms plan mode. It used to normalize to `plan` with an empty
    task and print a usage error, so there was no mode to enter at all."""
    from shamsu.cli.command_router import CommandRouter
    from shamsu.cli.repl import _PLAN_MODE_OFF_COMMANDS, SYSTEM_COMMANDS

    router = CommandRouter(SYSTEM_COMMANDS)
    assert router.route("/plan").valid
    assert router.route("/plan").normalized == "plan"
    assert router.route("/plan off").valid
    # `/plan off` must read as "leave the mode", never as "plan a task called off".
    assert router.route("/plan off").normalized in _PLAN_MODE_OFF_COMMANDS


@pytest.mark.parametrize(
    "text",
    [
        "make me a plan to add auth",
        "make a plan for the login page",
        "come up with a plan for the game",
        "draft a plan for refactoring",
        "plan out how to add login",
        "give me a plan for the api",
    ],
)
def test_natural_language_plan_requests_are_detected(text):
    """These used to fall through to QA and get chatted at instead of planned."""
    from shamsu.cli.repl import _looks_like_plan_request

    assert _looks_like_plan_request(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "what's the plan",
        "what is the plan",
        "show me the plan",
        "explain the plan",
        "read the plan",
        "build the app",
        "add two numbers",
    ],
)
def test_questions_about_a_plan_are_not_plan_requests(text):
    """Asking ABOUT a plan must not regenerate one over the top of it."""
    from shamsu.cli.repl import _looks_like_plan_request

    assert _looks_like_plan_request(text) is False


def test_plan_mode_is_visible_in_the_toolbar(tmp_path: Path):
    """A mode that changes what the next prompt does has to be visible."""
    from shamsu.cli.repl import CachedBottomToolbar, _bottom_toolbar

    assert "PLAN MODE" in _bottom_toolbar(tmp_path, plan_mode=True)
    assert "PLAN MODE" not in _bottom_toolbar(tmp_path, plan_mode=False)

    toolbar = CachedBottomToolbar(tmp_path)
    assert "PLAN MODE" not in toolbar()
    toolbar.set_plan_mode(True)
    assert "PLAN MODE" in toolbar()
    toolbar.set_plan_mode(False)
    assert "PLAN MODE" not in toolbar()


# ---------------------------------------------------------------------------
# Gap C1: plan grounding. A plan naming files that don't exist is a
# hallucination the coder then inherits as trusted context. Caught by the
# plan_references_only_real_files eval: a vanilla-JS workspace (game.js,
# index.html) produced a plan whose every step targeted a React component at
# src/components/PauseButton.tsx - because search returned nothing and the
# planner was handed NO context while being told to ground "ONLY" in it.
# ---------------------------------------------------------------------------


class _RecordingLLM:
    """Returns queued payloads and records every prompt it was given."""

    def __init__(self, payloads: list[dict]) -> None:
        self._payloads = list(payloads)
        self.prompts: list[str] = []

    async def generate_structured(self, role, system, prompt, schema, **kwargs):
        self.prompts.append(prompt)
        return json.dumps(self._payloads.pop(0))


def _plan_payload(*targets: str, creating: bool = False) -> dict:
    verb = "Create" if creating else "Edit"
    return {
        "title": "T",
        "steps": [
            {"description": f"{verb} it.", "target_file": target} for target in targets
        ],
        "verification": "run tests",
    }


def _workflow(workspace, llm):
    from shamsu.agents.plan_mode import PlanningWorkflow

    return PlanningWorkflow(workspace, llm=llm, search=None, memory_service=_StubMemory())


def test_planner_grounds_on_real_files_when_search_finds_nothing(tmp_path: Path):
    """search=None (no index) used to mean an EMPTY context. The files are
    right there on disk; a plain listing beats nothing."""
    (tmp_path / "game.js").write_text("// loop", encoding="utf-8")
    (tmp_path / "index.html").write_text("<html>", encoding="utf-8")

    llm = _RecordingLLM([_plan_payload("game.js")])
    asyncio.run(_workflow(tmp_path, llm).run("add a pause button"))

    prompt = llm.prompts[0]
    assert "game.js" in prompt and "index.html" in prompt


def test_grounding_listing_skips_dependency_noise(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1", encoding="utf-8")
    junk = tmp_path / "node_modules" / "left-pad"
    junk.mkdir(parents=True)
    (junk / "index.js").write_text("// dep", encoding="utf-8")

    llm = _RecordingLLM([_plan_payload("app.py")])
    asyncio.run(_workflow(tmp_path, llm).run("do a thing"))

    prompt = llm.prompts[0]
    assert "app.py" in prompt
    assert "node_modules" not in prompt


def test_planner_retries_once_when_it_invents_files(tmp_path: Path):
    """The exact observed failure: every step targets a phantom component."""
    (tmp_path / "game.js").write_text("// loop", encoding="utf-8")

    llm = _RecordingLLM(
        [
            _plan_payload("src/components/PauseButton.tsx"),   # hallucinated
            _plan_payload("game.js"),                           # corrected
        ]
    )
    plan = asyncio.run(_workflow(tmp_path, llm).run("add a pause button"))

    assert len(llm.prompts) == 2, "a phantom target must trigger one re-grounding round"
    assert "DO NOT EXIST" in llm.prompts[1]
    assert "src/components/PauseButton.tsx" in llm.prompts[1]
    assert "PauseButton" not in plan.markdown
    assert "game.js" in plan.markdown


def test_planner_does_not_retry_a_well_grounded_plan(tmp_path: Path):
    (tmp_path / "game.js").write_text("// loop", encoding="utf-8")

    llm = _RecordingLLM([_plan_payload("game.js")])
    asyncio.run(_workflow(tmp_path, llm).run("add a pause button"))

    assert len(llm.prompts) == 1, "a grounded plan must not pay for a second model call"


def test_creating_a_new_file_is_not_a_hallucination(tmp_path: Path):
    (tmp_path / "game.js").write_text("// loop", encoding="utf-8")

    llm = _RecordingLLM([_plan_payload("pause.js", creating=True)])
    asyncio.run(_workflow(tmp_path, llm).run("add a pause module"))

    assert len(llm.prompts) == 1, "proposing a new file is legitimate planning"


def test_a_worse_retry_does_not_replace_the_first_plan(tmp_path: Path):
    """If the retry is no better grounded, keep the original - the user can
    still fix a reviewable plan, but a degraded swap helps nobody."""
    (tmp_path / "game.js").write_text("// loop", encoding="utf-8")

    llm = _RecordingLLM(
        [
            _plan_payload("ghost-a.tsx"),
            _plan_payload("ghost-b.tsx", "ghost-c.tsx"),   # worse
        ]
    )
    plan = asyncio.run(_workflow(tmp_path, llm).run("add a pause button"))

    assert "ghost-a.tsx" in plan.markdown
    assert "ghost-b.tsx" not in plan.markdown
