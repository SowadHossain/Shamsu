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
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from shamsu.cli.repl import (
    _append_demo_login_docs,
    _extract_dev_command,
    _looks_like_dev_server_failure,
    _looks_like_dev_server_prompt,
    _bugfix_request_has_actionable_target,
    _looks_like_prd_build_request,
    _looks_like_prd_context_question,
    _requests_demo_login,
    _seed_django_demo_login,
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


def test_demo_login_is_seeded_only_when_requested_for_an_auth_project():
    auth_project = SimpleNamespace(
        pages=[SimpleNamespace(page_type="auth", name="Login")]
    )
    plain_project = SimpleNamespace(
        pages=[SimpleNamespace(page_type="dashboard", name="Dashboard")]
    )

    assert _requests_demo_login("seed demo data so I can log in", auth_project)
    assert not _requests_demo_login("build the project", auth_project)
    assert not _requests_demo_login("seed demo data", plain_project)


def test_demo_login_seed_command_and_docs_are_deterministic(tmp_path: Path):
    class FakeRunner:
        def __init__(self):
            self.calls = []

        def run(self, command, cwd):
            self.calls.append((command, cwd))
            return 0, "", ""

    runner = FakeRunner()
    (tmp_path / "README.md").write_text("# App\n", encoding="utf-8")
    (tmp_path / "SHAMSU_SUMMARY.md").write_text("# Summary\n", encoding="utf-8")

    ok, error = _seed_django_demo_login(tmp_path, runner)
    _append_demo_login_docs(tmp_path, "demo@example.com", "ShamsuDemo123!")
    _append_demo_login_docs(tmp_path, "demo@example.com", "ShamsuDemo123!")

    assert ok is True
    assert error == ""
    assert runner.calls[0][1] == tmp_path
    assert "manage.py shell -c" in runner.calls[0][0]
    assert "set_password('ShamsuDemo123!')" in runner.calls[0][0]
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert readme.count("## Demo Login") == 1
    assert "demo@example.com" in readme


def test_prd_build_request_does_not_hijack_unrelated_game_request_with_single_prd(tmp_path):
    prd = tmp_path / "REQUIREMENTS.md"
    prd.write_text("# SHAMSU Requirements\n", encoding="utf-8")
    assert not _looks_like_prd_build_request("build a pong game in java", tmp_path)


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
        # Hermetic: without these the planner makes a REAL model call, and a
        # thinking model that (reasonably) asks "what kind of game?" for this
        # vague prompt ends the turn on a clarification before any write is
        # attempted - so the WinError path under test never runs.
        use_planner=False,
        use_long_term_memory=False,
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

    # Inject the planner too. Without an `llm=`, AgentChatLoop builds a real
    # LLMManager and the per-request planner call reaches live Ollama - so this
    # test of the CLIENT timeout was quietly depending on a model being up. It
    # surfaced when the planner gained an upfront "this needs a decision" verdict
    # (J6) and correctly judged "do something" too vague, ending the turn with a
    # question before the tool loop could ever time out.
    class _QuietPlanner:
        async def run_specialist(self, specialist, pack):
            from shamsu.types import LLMResponse

            return LLMResponse(raw="", model_used="fake")

    loop = AgentChatLoop(tmp_path, client=fake_client, tools=fake_tools, llm=_QuietPlanner())
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

    # Templates are off by default, so this request falls through to the
    # freeform milestone build, whose agent turns hit a REAL Ollama. Stub them:
    # the assertion below is only about parse ordering, and leaving them live
    # made this test take minutes and stall the suite.
    async def fake_agent_chat(*_args, **_kwargs):
        from shamsu.agents.chat_loop import AgentLoopResult

        return AgentLoopResult(final="stopped for test", stopped=True)

    monkeypatch.setattr(repl_mod, "_run_agent_chat", fake_agent_chat)

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    asyncio.run(repl_mod._handle_prd_build_request(
        "build the product from this prd.md", tmp_path, console
    ))

    assert parse_calls, "parse_prd_file was never called before scaffold"
    assert parse_calls[0] == str(prd)


def test_freeform_prd_build_uses_scoped_react_milestones(tmp_path, monkeypatch):
    """A bespoke full-stack PRD must use scoped, checkpointed ReAct turns."""
    from rich.console import Console
    from shamsu.cli import repl as repl_mod

    prd = tmp_path / "prd.md"
    prd.write_text(
        "# AtlasOps\n\n"
        "## Overview\n"
        "A full-stack web application with a browser UI and terminal CLI.\n\n"
        "## Recommended Technical Stack\n"
        "- TypeScript\n- React\n- Vite\n- Node.js\n- SQLite\n\n"
        "### Entity: Incident\n\n"
        "Fields:\n\n"
        "- id: string, required, unique\n"
        "- title: string, required\n"
        "- status: enum, values: new, triaged, resolved\n\n"
        "## Required CLI Commands\n"
        "atlas init\natlas status\n",
        encoding="utf-8",
    )

    calls: list[tuple[str, dict[str, object]]] = []

    async def fail_pipeline_run(self, prd_path, target_dir=None):
        raise AssertionError("interactive PRD builds must not use bulk generation")

    async def fake_react(prompt, *_args, **kwargs):
        calls.append((prompt, kwargs))
        return SimpleNamespace(
            changed_files=["atlasops-freeform/src/current.ts"],
            stopped=False,
            awaiting_user=False,
            final="done",
        )

    async def fake_verify(*_args, **_kwargs):
        return "verified", {
            "status": "verified",
            "verified": True,
            "unverifiable": False,
            "exit_code": 0,
            "command": "focused-check",
            "files": ["atlasops-freeform/src/current.ts"],
            "summary": "Verification passed.",
        }

    async def fake_final_verify(*_args, **_kwargs):
        return True

    monkeypatch.setattr(repl_mod.FullDjangoPipeline, "run", fail_pipeline_run)
    monkeypatch.setattr(repl_mod, "_run_agent_chat", fake_react)
    monkeypatch.setattr(repl_mod, "_verify_prd_milestone", fake_verify)
    monkeypatch.setattr(repl_mod, "_verify_completed_plan", fake_final_verify)

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    asyncio.run(
        repl_mod._handle_prd_build_request(
            "build the product from prd.md in a new folder named atlasops-freeform",
            tmp_path,
            console,
        )
    )

    assert calls
    assert all(call[1]["allowed_write_paths"] == ("atlasops-freeform",) for call in calls)
    assert all(
        call[1]["user_request"]
        == "Implement the current coding milestone inside project root atlasops-freeform."
        for call in calls
    )
    assert all("Project root: atlasops-freeform" in call[0] for call in calls)
    assert any("## Active SHAMSU Skills" in call[0] for call in calls)
    assert any("Use this skill for coding" in call[0] for call in calls)


def test_freeform_prd_build_validates_acceptance_and_downgrades(tmp_path, monkeypatch):
    from rich.console import Console
    from shamsu.agents.full_pipeline import FullPipelineResult
    from shamsu.cli import repl as repl_mod

    prd = tmp_path / "prd.md"
    prd.write_text(
        "# LedgerLite\n\n"
        "## Acceptance\n"
        "- `python ledgerlite.py list` prints `No expenses yet`.\n",
        encoding="utf-8",
    )
    project = SimpleNamespace(
        project_name="ledgerlite-app",
        category="utility",
        archetype=SimpleNamespace(value="utility"),
    )
    validation_calls: list[dict[str, object]] = []
    repair_calls: list[dict[str, object]] = []

    async def fake_pipeline_run(self, prd_path, target_dir=None):
        target = tmp_path / str(target_dir)
        target.mkdir(parents=True, exist_ok=True)
        (target / "ledgerlite.py").write_text("print('wrong')\n", encoding="utf-8")
        return FullPipelineResult(
            prd_path=Path(prd_path),
            target_dir=target,
            project=project,
            written_files=["ledgerlite.py"],
            success=True,
        )

    def fake_validation(prd_text, output_scope, acceptance, workspace, console, session_logger=None):
        validation_calls.append(
            {
                "prd_text": prd_text,
                "output_scope": output_scope,
                "acceptance": acceptance,
                "workspace": workspace,
            }
        )
        return False, ["Failed command: python ledgerlite.py list"]

    async def fake_repair(user_input, workspace, console, **kwargs):
        repair_calls.append(
            {
                "user_input": user_input,
                "workspace": workspace,
                "allowed_write_paths": kwargs.get("allowed_write_paths"),
            }
        )
        return SimpleNamespace(changed_files=["ledgerlite.py"])

    async def no_structured_rewrite(*_args, **_kwargs):
        return []

    monkeypatch.setattr(repl_mod, "_build_search_agent", lambda *args, **kwargs: (None, False))
    monkeypatch.setattr(repl_mod.FullDjangoPipeline, "run", fake_pipeline_run)
    monkeypatch.setattr(repl_mod, "_run_prd_validation", fake_validation)
    monkeypatch.setattr(repl_mod, "_structured_validation_rewrite", no_structured_rewrite)
    monkeypatch.setattr(repl_mod, "_run_agent_chat", fake_repair)

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    result = asyncio.run(
        repl_mod._run_freeform_prd_build(
            "build the product from prd.md in a new folder named ledgerlite-app",
            prd,
            project,
            tmp_path,
            console,
            prd_text=prd.read_text(encoding="utf-8"),
            acceptance=[("python ledgerlite.py list", "No expenses yet")],
        )
    )

    assert result.success is False
    assert "PRD validation failed" in result.error
    assert validation_calls[0]["workspace"] == tmp_path / "ledgerlite-app"
    assert validation_calls[0]["output_scope"] == ("ledgerlite.py",)
    assert repair_calls
    assert repair_calls[0]["workspace"] == tmp_path / "ledgerlite-app"
    assert repair_calls[0]["allowed_write_paths"] == ("ledgerlite.py",)


def test_source_repair_targets_exclude_runtime_json_data():
    from shamsu.cli import repl as repl_mod

    assert repl_mod._source_repair_targets(
        ("ledgerlite.py", "ledgerlite.json", "package.json", "README.md", "report.csv")
    ) == ("ledgerlite.py", "package.json")


def test_acceptance_failure_hint_explains_subcommand_options():
    from shamsu.cli import repl as repl_mod

    hint = repl_mod._acceptance_failure_hint(
        "python ledgerlite.py seed --db data.json",
        "ledgerlite.py: error: unrecognized arguments: --db data.json",
    )

    assert "after the subcommand" in hint
    assert "subparser" in hint


def test_structured_validation_rewrite_applies_complete_source_file(tmp_path, monkeypatch):
    from rich.console import Console
    from shamsu.cli import repl as repl_mod

    target = tmp_path / "ledgerlite.py"
    target.write_text("print('old')\n", encoding="utf-8")

    class FakeLLM:
        def __init__(self, *args, **kwargs):
            pass

        async def generate_structured(self, role, system, prompt, schema, **kwargs):
            assert "Validation failures to fix" in prompt
            return '{"content": "print(\\"new\\")\\n"}'

    monkeypatch.setattr(repl_mod, "LLMManager", FakeLLM)

    changed = asyncio.run(
        repl_mod._structured_validation_rewrite(
            "PRD",
            ["acceptance failed"],
            ("ledgerlite.py",),
            tmp_path,
            Console(record=True),
        )
    )

    assert changed == ["ledgerlite.py"]
    assert target.read_text(encoding="utf-8") == 'print("new")\n'


def test_freeform_generation_budgets_keep_repair_short(monkeypatch):
    from shamsu.cli import repl as repl_mod
    from shamsu.repair.prompt import STRICT_DEBUG_SYSTEM

    monkeypatch.setenv("SHAMSU_FREEFORM_NUM_PREDICT", "9000")
    monkeypatch.setenv("SHAMSU_REPAIR_NUM_PREDICT", "700")

    assert repl_mod._structured_num_predict_for(
        STRICT_DEBUG_SYSTEM,
        {"type": "object", "properties": {"target_file": {"type": "string"}}},
    ) == 700
    assert repl_mod._structured_num_predict_for(
        "You are SHAMSU writing ONE file",
        {"type": "object", "properties": {"content": {"type": "string"}}},
    ) == 9000


def test_bugfix_request_requires_concrete_target_before_workflow():
    assert not _bugfix_request_has_actionable_target("fix a code for me")
    assert _bugfix_request_has_actionable_target("fix app.py")
    assert _bugfix_request_has_actionable_target("fix this traceback: TypeError: bad value")
