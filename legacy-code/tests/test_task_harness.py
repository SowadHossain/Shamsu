from __future__ import annotations

from shamsu.agents.task_harness import append_task_handoff, build_task_plan
from shamsu.types import RoutingDecision


def test_build_task_plan_maps_bugfix_to_bugfix_mode_and_coder_tools():
    decision = RoutingDecision(
        intent="bug_fix",
        complexity="multi_step",
        steps=[
            {"id": 1, "specialist": "bugfix", "task": "Inspect the failing traceback."},
            {"id": 2, "specialist": "bugfix", "task": "Patch the failing file."},
        ],
        needs_tools=["search_index", "read_file", "write_file", "run_command"],
        target_files=["app.py"],
        confidence=0.82,
    )

    plan = build_task_plan(decision, "fix the crash")

    assert plan.mode == "bugfix"
    assert plan.executor_role == "bugfix"
    assert plan.required_tools == ["search_index", "read_file", "write_file", "run_command"]
    assert plan.target_files == ["app.py"]
    assert "rerun the failing command or test when safe" in plan.verification


def test_append_task_handoff_renders_master_plan_for_specialists():
    decision = RoutingDecision(intent="code_edit", complexity="single", confidence=0.5)
    plan = build_task_plan(decision, "add validation")

    rendered = append_task_handoff("add validation", plan, "Workspace root: demo")

    assert "## SHAMSU Task Harness" in rendered
    assert "Mode: code_edit" in rendered
    assert "Executor role: coder" in rendered
    assert "discover with search_index/read_file before editing" in rendered
    assert "Workspace root: demo" in rendered
