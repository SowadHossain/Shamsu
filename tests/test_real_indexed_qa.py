from __future__ import annotations

import asyncio
from io import StringIO

from rich.console import Console

from shamsu.cli import repl
from shamsu.cli.repl import _build_workspace_qa_workflow, _handle_request
from shamsu.indexer.walker import FileWalker
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import WebTool
from shamsu.types import LLMResponse


def _console_output() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def _tools(root):
    return (
        WebTool(approval_func=lambda _request: False),
        BrowserTool(root, approval_func=lambda _request: False),
    )


def test_workspace_qa_workflow_uses_empty_search_without_index(tmp_path):
    workflow, uses_real_index = _build_workspace_qa_workflow(tmp_path)

    preview = workflow.build_prompt("how does auth work?")

    assert uses_real_index is False
    assert preview.pack.snippets == []
    assert "stub/example.py" not in preview.prompt


def test_workspace_qa_workflow_uses_real_index_when_available(tmp_path):
    source = tmp_path / "auth.py"
    source.write_text(
        "def authenticate_user(username, password):\n"
        "    return username == 'admin' and bool(password)\n",
        encoding="utf-8",
    )
    FileWalker(tmp_path).index()

    workflow, uses_real_index = _build_workspace_qa_workflow(tmp_path)
    preview = workflow.build_prompt("authenticate user")

    assert uses_real_index is True
    assert "auth.py" in preview.prompt
    assert "authenticate_user" in preview.prompt
    assert "stub/example.py" not in preview.prompt


def test_repl_request_reports_missing_index_without_stub_preview(tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    asyncio.run(_handle_request("how does auth work?", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "No index found. Run `index` first" in rendered
    assert "Context Preview" not in rendered
    assert "stub/example.py" not in rendered


def test_repl_greeting_prints_ready_message_without_model_qa(tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    asyncio.run(_handle_request("hi", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "SHAMSU is ready" in rendered
    assert "Workspace:" in rendered
    assert "intent=qa" not in rendered
    assert "No index found" not in rendered
    assert "Context Preview" not in rendered


def test_repl_general_chat_without_index_uses_local_chat(monkeypatch, tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    class FakeLLM:
        def __init__(self, session_logger=None):
            self.session_logger = session_logger

        async def run_specialist(self, specialist, pack):
            assert specialist == "qa"
            assert pack.task_id == "general-chat"
            assert "No indexed project context" in pack.prd_context
            return LLMResponse(raw="General answer", model_used="fake-phi3")

    monkeypatch.setattr(repl, "LLMManager", FakeLLM)

    asyncio.run(_handle_request("what is recursion?", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "General answer" in rendered
    assert "Chat (fake-phi3)" in rendered
    assert "No index found" not in rendered
    assert "intent=qa" not in rendered


def test_repl_workspace_prd_request_finds_single_prd_without_routing(tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)
    prd = tmp_path / "TODO_PRD.md"
    prd.write_text("# Todo App\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")

    asyncio.run(_handle_request("i have add a prd to my working folder can you check that out?", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "PRD Found" in rendered
    assert "TODO_PRD.md" in rendered
    assert "/plan-prd" in rendered
    assert "Code Edit Not Applied" not in rendered


def test_repl_workspace_file_question_lists_real_files(tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)
    (tmp_path / "README.md").write_text("# Test\n", encoding="utf-8")
    (tmp_path / "src").mkdir()

    asyncio.run(_handle_request("hi what files do i have here?", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "Workspace Files" in rendered
    assert "README.md" in rendered
    assert "src" in rendered
    assert "I cannot see any files" not in rendered


def test_repl_workspace_location_question_reports_workspace(tmp_path):
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    asyncio.run(_handle_request("what folder are you in rn?", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "Current Workspace" in rendered
    assert str(tmp_path) in rendered
    assert "I don’t have a current working directory" not in rendered


def test_repl_request_uses_indexed_context_when_index_exists(tmp_path):
    source = tmp_path / "payments.py"
    source.write_text(
        "class PaymentGateway:\n"
        "    def charge_card(self, amount):\n"
        "        return amount > 0\n",
        encoding="utf-8",
    )
    FileWalker(tmp_path).index()
    console, output = _console_output()
    web_tool, browser_tool = _tools(tmp_path)

    asyncio.run(_handle_request("charge card", tmp_path, console, web_tool, browser_tool))

    rendered = output.getvalue()
    assert "payments.py" in rendered
    assert "charge_card" in rendered
    assert "No index found" not in rendered
    assert "stub/example.py" not in rendered
