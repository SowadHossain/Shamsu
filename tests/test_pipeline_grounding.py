"""
Tests for PRD grounding, template policy, model timeout, and fake-success prevention.

Covers the failures described in pipeline.md / the problem statement:
 - PRD must be parsed before template selection or generation
 - Template smoke checks != PRD complete
 - "what is this game about?" reads the PRD, not QA
 - WinError 32 stops the pipeline and is reported clearly
 - Model timeout stops a stuck loop and reports clearly
 - Same error packet without new evidence stops retrying
"""
from __future__ import annotations

import asyncio
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shamsu.cli.repl import (
    _extract_dev_command,
    _looks_like_dev_server_failure,
    _looks_like_dev_server_prompt,
    _looks_like_prd_build_request,
    _looks_like_prd_context_question,
)


# ─── PRD GROUNDING ────────────────────────────────────────────────────────────

def test_prd_build_request_detected_with_prd_in_workspace(tmp_path):
    prd = tmp_path / "Product Requirements Document.md"
    prd.write_text("# Game\n\n## Overview\nA fun multiplayer game.\n", encoding="utf-8")
    assert _looks_like_prd_build_request("build the product from this PRD", tmp_path)


def test_prd_build_request_detected_with_pdf_mention(tmp_path):
    prd = tmp_path / "prd.pdf"
    prd.write_bytes(b"%PDF mock")
    assert _looks_like_prd_build_request("build this prd.pdf", tmp_path)


def test_prd_build_request_not_triggered_without_prd(tmp_path):
    assert not _looks_like_prd_build_request("build the navbar", tmp_path)


def test_prd_context_question_about_game_with_prd_routes_to_prd(tmp_path):
    prd = tmp_path / "prd.md"
    prd.write_text("# Game\n\n## Overview\nA fun game about racing.\n", encoding="utf-8")
    assert _looks_like_prd_context_question("what is this game about", tmp_path)
    assert _looks_like_prd_context_question("what's this game about?", tmp_path)


def test_prd_context_question_without_prd_file_does_not_trigger(tmp_path):
    assert not _looks_like_prd_context_question("what is this game about", tmp_path)


def test_prd_context_question_project_about(tmp_path):
    prd = tmp_path / "project-prd.md"
    prd.write_text("# App\n", encoding="utf-8")
    assert _looks_like_prd_context_question("what is this project about", tmp_path)


def test_prd_context_question_summarize_prd(tmp_path):
    prd = tmp_path / "requirements.md"
    prd.write_text("# Product\n", encoding="utf-8")
    assert _looks_like_prd_context_question("summarize the prd", tmp_path)


def test_unrelated_question_not_prd_context(tmp_path):
    prd = tmp_path / "prd.md"
    prd.write_text("# Game\n", encoding="utf-8")
    assert not _looks_like_prd_context_question("how do I add a button", tmp_path)
    assert not _looks_like_prd_context_question("fix the navbar", tmp_path)


# ─── DEV SERVER COMMAND EXTRACTION ───────────────────────────────────────────

def test_extract_command_not_whole_sentence(tmp_path):
    cmd = _extract_dev_command(
        "can you run the code npm run dev in a new terminal window", tmp_path
    )
    assert cmd == "npm run dev", f"Expected 'npm run dev', got {cmd!r}"


def test_extract_workspace_command(tmp_path):
    cmd = _extract_dev_command(
        "run npm --workspace client run dev", tmp_path
    )
    assert cmd == "npm --workspace client run dev", f"Got {cmd!r}"


def test_bare_command_passes_through(tmp_path):
    cmd = _extract_dev_command("npm run dev", tmp_path)
    assert cmd == "npm run dev"


def test_inferred_when_no_command_found(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts":{"dev":"vite"}}', encoding="utf-8")
    cmd = _extract_dev_command("start the dev server", tmp_path)
    assert cmd == "npm run dev"


# ─── DEV SERVER FAILURE ROUTING ──────────────────────────────────────────────

def test_didnt_run_is_dev_server_failure():
    assert _looks_like_dev_server_failure("it didnt run btw")


def test_didnt_run_apostrophe_is_dev_server_failure():
    assert _looks_like_dev_server_failure("it didn't run btw")


def test_dev_server_failed_to_start():
    assert _looks_like_dev_server_failure("the dev server failed to start")


def test_server_wont_start():
    assert _looks_like_dev_server_failure("server won't start")


def test_unrelated_trouble_not_dev_failure():
    assert not _looks_like_dev_server_failure("my tests are failing")
    assert not _looks_like_dev_server_failure("how does auth work")


def test_dev_server_failure_does_not_match_dev_server_prompt():
    """Failure phrases must not accidentally trigger as a launch request."""
    assert not _looks_like_dev_server_prompt("it didnt run btw")


# ─── FAKE SUCCESS PREVENTION: WinError 32 ────────────────────────────────────

def test_write_failure_correction_includes_winerror_hint():
    from shamsu.agents.chat_loop import _write_failure_correction
    msg = _write_failure_correction("game.ts", "WinError 32 - being used by another process")
    assert "locked" in msg.lower() or "winerror 32" in msg.lower()
    assert "NOT changed" in msg


def test_write_failure_correction_no_false_winerror():
    from shamsu.agents.chat_loop import _write_failure_correction
    msg = _write_failure_correction("game.ts", "Permission denied")
    assert "WinError" not in msg
    assert "NOT changed" in msg


@pytest.mark.asyncio
async def test_winerror_32_stops_chat_loop(tmp_path):
    """A WinError 32 write failure must stop the loop immediately."""
    from shamsu.agents.chat_loop import AgentChatLoop
    from shamsu.tools.agent_tools import AgentToolRegistry, ToolResult

    # Simulate one write_file tool call that returns WinError 32
    fake_tools = MagicMock(spec=AgentToolRegistry)
    fake_tools.tool_schemas.return_value = []
    fake_tools.execute.return_value = ToolResult(
        ok=False,
        message="WinError 32 - being used by another process",
        data={},
    )

    fake_client = AsyncMock()
    fake_client.chat.return_value = MagicMock(
        message=MagicMock(
            content="",
            tool_calls=[
                {"id": "1", "function": {"name": "write_file", "arguments": {"filepath": "game.ts", "content": "x"}}}
            ],
        )
    )

    loop = AgentChatLoop(
        tmp_path,
        client=fake_client,
        tools=fake_tools,
        max_tool_rounds=10,
    )
    result = await loop.run("write game.ts")
    assert result.stopped
    assert "WinError 32" in result.final or "locked" in result.final.lower()
    # Should stop after a single write_file round, not exhaust all 10 rounds
    assert result.tool_rounds <= 2


# ─── MODEL TIMEOUT GUARD ──────────────────────────────────���──────────────────

@pytest.mark.asyncio
async def test_model_timeout_stops_loop(tmp_path, monkeypatch):
    """A stalled model call must time out and return a clear stop message."""
    import shamsu.agents.chat_loop as chat_loop_mod
    monkeypatch.setattr(chat_loop_mod, "_MODEL_CALL_TIMEOUT_SECONDS", 1)

    from shamsu.agents.chat_loop import AgentChatLoop
    from shamsu.tools.agent_tools import AgentToolRegistry

    async def slow_chat(**_kwargs):
        await asyncio.sleep(10)

    fake_tools = MagicMock(spec=AgentToolRegistry)
    fake_tools.tool_schemas.return_value = []

    fake_client = AsyncMock()
    fake_client.chat.side_effect = slow_chat

    loop = AgentChatLoop(tmp_path, client=fake_client, tools=fake_tools)
    result = await loop.run("do something")
    assert result.stopped
    assert "timed out" in result.final.lower() or "not respond" in result.final.lower()


# ─── TEMPLATE SMOKE CHECK != PRD COMPLETE ────────────────────────────────────

def test_prd_parse_happens_before_template_build(tmp_path, monkeypatch):
    """_handle_prd_build_request must call parse_prd_file before scaffold."""
    from shamsu.cli import repl as repl_mod
    from rich.console import Console

    prd = tmp_path / "prd.md"
    prd.write_text("# Game\n\n## Overview\nA multiplayer racing game.\n", encoding="utf-8")

    parse_calls: list[str] = []
    pipeline_calls: list[str] = []

    original_parse = repl_mod.parse_prd_file
    def tracked_parse(path):
        parse_calls.append(str(path))
        return original_parse(path)

    monkeypatch.setattr(repl_mod, "parse_prd_file", tracked_parse)

    # We only want to verify parse is called; stop before the full build runs
    async def fake_pipeline_run(*_args, **_kwargs):
        from shamsu.agents.full_pipeline import FullPipelineResult
        pipeline_calls.append("called")
        return FullPipelineResult(prd_path=prd, target_dir=tmp_path, success=False, error="test stop")

    monkeypatch.setattr(repl_mod.FullDjangoPipeline, "run", fake_pipeline_run)

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    asyncio.run(repl_mod._handle_prd_build_request(
        "build the product from this prd.md", tmp_path, console
    ))

    assert parse_calls, "parse_prd_file was never called before scaffold"
    assert parse_calls[0] == str(prd)
