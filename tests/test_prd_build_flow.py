from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path

from rich.console import Console

from shamsu.cli import repl
from shamsu.tasks.state import list_task_ids, load_task


def _console() -> tuple[Console, StringIO]:
    out = StringIO()
    return Console(file=out, force_terminal=False, width=100), out


def _write_prd(root: Path) -> Path:
    prd = root / "Product Requirements Document.md"
    prd.write_text(
        "# Team Notes App\n\n"
        "## Overview\nA collaborative notes web app.\n\n"
        "## Milestones\n"
        "Milestone 1: Project setup\n"
        "Milestone 2: Note editing\n",
        encoding="utf-8",
    )
    return prd


def test_prd_build_previews_plan_and_builds_long_running(monkeypatch, tmp_path):
    _write_prd(tmp_path)
    captured = []

    async def fake_run_agent_chat(user_input, workspace, console, session_logger=None, force_long_running=False, auto_approve=False):
        captured.append((user_input, force_long_running, auto_approve))

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)

    console, out = _console()
    asyncio.run(
        repl._handle_prd_build_request("build me the product from this prd", tmp_path, console)
    )

    rendered = out.getvalue()
    assert "PRD Build Plan" in rendered
    assert "Team Notes App" in rendered
    assert "Milestone 1: Project setup" in rendered
    assert len(captured) == 2
    # Each milestone build is long-running AND auto-approved (the build request
    # is the consent, so file writes proceed without further prompts).
    assert all(force and auto for _prompt, force, auto in captured)
    assert "Current milestone 1/2: Milestone 1: Project setup" in captured[0][0]
    assert "Current milestone 2/2: Milestone 2: Note editing" in captured[1][0]
    task = load_task(tmp_path, list_task_ids(tmp_path)[0])
    assert task.phase == "milestone-2"
    assert [step.status.value for step in task.steps] == ["done", "done"]


def test_prd_build_starts_directly_without_a_broken_inline_approval(monkeypatch, tmp_path):
    """The build request is the consent — it must start the build directly and
    never sit on a fragile inline input() approval that could auto-deny."""
    _write_prd(tmp_path)
    called = {"build": False}

    async def fake_run_agent_chat(*args, **kwargs):
        called["build"] = True

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)
    # If the handler ever calls input()/ask_approval, fail loudly.
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no input() in build")))

    console, out = _console()
    asyncio.run(
        repl._handle_prd_build_request("build me the product from this prd", tmp_path, console)
    )

    rendered = out.getvalue()
    assert "Building now" in rendered
    assert "not approved" not in rendered.lower()
    assert called["build"] is True


def test_prd_build_asks_which_when_multiple_prds(monkeypatch, tmp_path):
    (tmp_path / "prd-one.md").write_text("# One\n", encoding="utf-8")
    (tmp_path / "Product Requirements Document.md").write_text("# Two\n", encoding="utf-8")
    called = {"build": False}

    async def fake_run_agent_chat(*args, **kwargs):
        called["build"] = True

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)

    console, out = _console()
    asyncio.run(
        repl._handle_prd_build_request("build me the product from this prd", tmp_path, console)
    )

    rendered = out.getvalue()
    assert "multiple PRD" in rendered
    assert called["build"] is False


def test_prd_build_reports_when_no_prd_found(monkeypatch, tmp_path):
    called = {"build": False}

    async def fake_run_agent_chat(*args, **kwargs):
        called["build"] = True

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)

    console, out = _console()
    asyncio.run(
        repl._handle_prd_build_request("build me the product from this prd", tmp_path, console)
    )

    rendered = out.getvalue()
    assert "couldn't find a PRD" in rendered
    assert called["build"] is False


def test_multiplayer_prd_build_scaffolds_template_then_runs_agent(tmp_path, monkeypatch):
    (tmp_path / "Product Requirements Document.md").write_text(
        "# Multiplayer Snake Arena\n\n"
        "## Overview\n"
        "Build a multiplayer 3D game with player snakes, a lobby, remote players, HUD, and end condition.\n",
        encoding="utf-8",
    )
    captured = []

    async def fake_run_agent_chat(user_input, workspace, console, session_logger=None, force_long_running=False, auto_approve=False):
        captured.append((user_input, workspace, force_long_running, auto_approve))

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)

    async def fake_verify(*args, **kwargs):
        return True

    monkeypatch.setattr(repl, "_verify_and_repair_frontend", fake_verify)

    console, out = _console()
    asyncio.run(
        repl._handle_prd_build_request("build me the game from prd", tmp_path, console)
    )

    rendered = out.getvalue()
    assert "Template Build" in rendered
    assert "multiplayer-game" in rendered
    assert (tmp_path / "package.json").exists()
    assert (tmp_path / "client" / "src" / "App.tsx").exists()
    assert (tmp_path / "server" / "src" / "index.ts").exists()
    assert (tmp_path / "server" / "src" / "db.ts").exists()
    assert (tmp_path / "SHAMSU_SUMMARY.md").exists()
    assert len(captured) == 1
    assert captured[0][1] == tmp_path
    assert captured[0][2] is True
    assert captured[0][3] is True
    assert "Do real coding work now" in captured[0][0]
    assert "// HOLE:rule.update" in captured[0][0]


def test_multiplayer_prd_build_skips_scaffold_overwrite_and_runs_agent(tmp_path, monkeypatch):
    (tmp_path / "Product Requirements Document.md").write_text(
        "# Multiplayer Snake Arena\n\n"
        "Build a multiplayer 3D snake arena with collisions and a win condition.",
        encoding="utf-8",
    )
    for relative in (
        "client/src/game",
        "client/src/ui",
        "server/src",
    ):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    existing_app = tmp_path / "client" / "src" / "App.tsx"
    existing_app.write_text("// custom work\n", encoding="utf-8")
    for relative in (
        "package.json",
        "client/package.json",
        "client/src/game/entities.ts",
        "client/src/game/rules.ts",
        "client/src/ui/Hud.tsx",
        "server/src/index.ts",
        "server/src/db.ts",
    ):
        (tmp_path / relative).write_text("// existing\n", encoding="utf-8")
    captured = []

    async def fake_run_agent_chat(*args, **kwargs):
        captured.append((args, kwargs))

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)

    async def fake_verify(*args, **kwargs):
        return True

    monkeypatch.setattr(repl, "_verify_and_repair_frontend", fake_verify)

    console, out = _console()
    asyncio.run(
        repl._handle_prd_build_request("finish all the requirements", tmp_path, console)
    )

    # Setup already done: it must NOT re-announce the scaffold step, just continue.
    rendered = out.getvalue()
    assert "Continuing the game build" in rendered
    assert "Template Ready" not in rendered
    assert "Template Build" not in rendered
    assert existing_app.read_text(encoding="utf-8") == "// custom work\n"
    assert captured


def test_prd_build_without_milestones_falls_back_to_single_pass(monkeypatch, tmp_path):
    (tmp_path / "Product Requirements Document.md").write_text(
        "# Notes App\n\nBuild a tiny notes app without explicit milestone lines.",
        encoding="utf-8",
    )
    captured = []

    async def fake_run_agent_chat(user_input, workspace, console, session_logger=None, force_long_running=False, auto_approve=False):
        captured.append((user_input, force_long_running, auto_approve))

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)

    console, out = _console()
    asyncio.run(
        repl._handle_prd_build_request("build me the product from this prd", tmp_path, console)
    )

    assert len(captured) == 1
    assert captured[0][1] is True  # force_long_running
    assert captured[0][2] is True  # auto_approve
    assert "Build the complete product" in captured[0][0]
    assert list_task_ids(tmp_path) == []
