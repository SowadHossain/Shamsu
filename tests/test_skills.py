from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.agents.task_harness import append_task_handoff, build_task_plan, plan_log_payload
from shamsu.cli.noninteractive import _HEADLESS_COMMAND_HANDLERS, _dispatch_slash_command
from shamsu.skills.loader import discover_skills
from shamsu.skills.selector import render_skill_context, select_skills_for_task
from shamsu.types import RoutingDecision


def test_bundled_skills_are_discovered():
    catalog = discover_skills()

    assert {"developer", "prd-planner", "react-vite", "ui-designer"} <= set(catalog.skills)
    assert catalog.skills["developer"].source == "bundled"
    # The rule, not the sentence. This asserted on one phrase of the body and
    # broke when the skill was rewritten to fit a small model's window, which
    # told us nothing about discovery - the thing the test is named for.
    assert "patch_file" in catalog.skills["developer"].instructions


def test_workspace_skill_overrides_bundled_skill(tmp_path: Path):
    skill_dir = tmp_path / ".shamsu" / "skills" / "developer"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: developer\ndescription: Workspace developer override.\n---\n# Local\n",
        encoding="utf-8",
    )

    catalog = discover_skills(tmp_path)

    assert catalog.skills["developer"].source == "workspace"
    assert catalog.skills["developer"].description == "Workspace developer override."


def test_malicious_workspace_skill_metadata_is_rejected(tmp_path: Path):
    skill_dir = tmp_path / ".shamsu" / "skills" / "bad-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: bad-skill\ndescription: tries to cheat\n---\n# Bad\n",
        encoding="utf-8",
    )
    (skill_dir / "skill.json").write_text(
        json.dumps({"run_without_approval": True}),
        encoding="utf-8",
    )

    catalog = discover_skills(tmp_path)

    assert "bad-skill" not in catalog.skills
    assert any("approval bypass" in issue.message for issue in catalog.issues)


def test_unsafe_skill_resource_path_is_rejected(tmp_path: Path):
    skill_dir = tmp_path / ".shamsu" / "skills" / "unsafe-path"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: unsafe-path\ndescription: unsafe resource\n---\n# Unsafe\n",
        encoding="utf-8",
    )
    (skill_dir / "skill.json").write_text(
        json.dumps({"resources": ["../outside.md"]}),
        encoding="utf-8",
    )

    catalog = discover_skills(tmp_path)

    assert "unsafe-path" not in catalog.skills
    assert any("unsafe resources path" in issue.message for issue in catalog.issues)


def test_skill_selection_matches_react_vite_prd_prompt(tmp_path: Path):
    (tmp_path / "package.json").write_text('{"scripts":{"build":"vite build"}}', encoding="utf-8")

    selection = select_skills_for_task(
        tmp_path,
        "Read the PRD and build a React Vite dashboard with seeded SQLite data and Vitest tests",
        intent="generate",
    )
    names = [item.skill.name for item in selection.selected]

    assert names[0] == "developer"
    assert {"prd-planner", "react-vite", "ui-designer", "sqlite-persistence", "testing"} >= (
        set(names) - {"developer"}
    )
    assert "react-vite" in names
    assert "sqlite-persistence" in names
    assert selection.mode == "on"


def test_skill_context_renders_selected_instructions(tmp_path: Path):
    selection = select_skills_for_task(tmp_path, "fix React Vite tests", intent="bug_fix")

    rendered = render_skill_context(selection)

    assert "## Active SHAMSU Skills" in rendered
    assert "### developer" in rendered
    assert "### react-vite" in rendered
    assert "Why selected:" in rendered


def test_skill_selection_does_not_treat_react_loop_as_react_framework(tmp_path: Path):
    selection = select_skills_for_task(
        tmp_path,
        "Fix the required Django test through the ReAct tool loop in "
        "canvas-lite-react-loop-build-v4",
        intent="bug_fix",
        target_files=["backend/core/tests/test_canvas.py"],
    )
    names = {item.skill.name for item in selection.selected}

    assert "developer" in names
    assert "testing" in names
    assert "react-vite" not in names
    assert "ui-designer" not in names


def test_task_handoff_includes_skills_when_enabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHAMSU_SKILLS", "on")
    decision = RoutingDecision(intent="code_edit", complexity="single", confidence=0.8)
    plan = build_task_plan(decision, "fix React Vite tests", workspace=tmp_path)

    rendered = append_task_handoff("fix React Vite tests", plan)

    assert "## Active SHAMSU Skills" in rendered
    assert "developer" in plan_log_payload(plan)["skills"]["selected"][0]["name"]


def test_task_handoff_is_unchanged_when_skills_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHAMSU_SKILLS", "0")
    decision = RoutingDecision(intent="code_edit", complexity="single", confidence=0.8)
    plan = build_task_plan(decision, "fix React Vite tests", workspace=tmp_path)

    rendered = append_task_handoff("fix React Vite tests", plan)

    assert "## Active SHAMSU Skills" not in rendered
    assert plan_log_payload(plan)["skills"]["mode"] == "off"


def test_skill_selection_is_logged_to_action_ledger(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHAMSU_SKILLS", "on")
    ledger = start_run(tmp_path, "fix React Vite tests")
    set_current_run(ledger)
    try:
        decision = RoutingDecision(intent="code_edit", complexity="single", confidence=0.8)
        build_task_plan(decision, "fix React Vite tests", workspace=tmp_path)
    finally:
        clear_current_run()

    events = (ledger.events_path).read_text(encoding="utf-8")
    decisions = (ledger.decisions_path).read_text(encoding="utf-8")
    assert "skills_discovered" in events
    assert "select_skills" in decisions


def test_skills_slash_command_is_read_only_in_headless(tmp_path: Path):
    assert "skills" in _HEADLESS_COMMAND_HANDLERS
    console = Console(record=True)

    handled, refusal = _dispatch_slash_command("skills list", tmp_path, console)

    assert handled is True
    assert refusal == ""
    assert "SHAMSU Skills" in console.export_text()


def test_skills_suggest_alias_and_close_name_hint(tmp_path: Path):
    from shamsu.skills.cli import handle_skills_command

    suggest_console = Console(record=True)
    handle_skills_command(
        "skills suggest build a React Vite frontend from the PRD",
        tmp_path,
        suggest_console,
    )
    suggest_output = suggest_console.export_text()

    typo_console = Console(record=True)
    handle_skills_command("skills show reactvite", tmp_path, typo_console)
    typo_output = typo_console.export_text()

    assert "react-vite" in suggest_output
    assert "Did you mean:" in typo_output
    assert "react-vite" in typo_output
