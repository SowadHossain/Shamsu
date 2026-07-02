from __future__ import annotations

import asyncio
from io import StringIO

from rich.console import Console

from shamsu.cli.repl import _build_workspace_qa_workflow, _handle_request
from shamsu.indexer.walker import FileWalker


def _console_output() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


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

    asyncio.run(_handle_request("how does auth work?", tmp_path, console))

    rendered = output.getvalue()
    assert "No index found. Run `index` first" in rendered
    assert "stub/example.py" not in rendered


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

    asyncio.run(_handle_request("charge card", tmp_path, console))

    rendered = output.getvalue()
    assert "payments.py" in rendered
    assert "charge_card" in rendered
    assert "No index found" not in rendered
    assert "stub/example.py" not in rendered
