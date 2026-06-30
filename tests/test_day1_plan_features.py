from __future__ import annotations

import sqlite3

import pytest

from shamsu.agents.qa_workflow import QAWorkflow
from shamsu.core.coordinator import Coordinator
from shamsu.indexer.walker import FileWalker, sha256_file
from shamsu.prd.parser import MarkdownPRDParser
from shamsu.safety.approval import ask_approval
from shamsu.types import ApprovalRequest, RoutingDecision


class FakeLLM:
    async def route(self, prompt: str, project_summary: str) -> RoutingDecision:
        return RoutingDecision(
            intent="qa",
            complexity="single",
            steps=[{"id": 1, "specialist": "qa", "task": prompt}],
            needs_tools=["search"],
            confidence=0.9,
        )

    async def run_specialist(self, specialist, pack):  # pragma: no cover - Day 1 preview only
        raise NotImplementedError


def test_file_walker_indexes_files_and_ignores_heavy_dirs(tmp_path):
    (tmp_path / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("x", encoding="utf-8")

    db_path = tmp_path / ".shamsu" / "index.db"
    entries = FileWalker(tmp_path, db_path=db_path).index()

    assert [entry.path for entry in entries] == ["app.py"]
    assert entries[0].language == "python"
    assert entries[0].hash == sha256_file(tmp_path / "app.py")

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT path, language FROM files").fetchall()
    conn.close()
    assert rows == [("app.py", "python")]


def test_markdown_prd_parser_extracts_title_and_sections(tmp_path):
    prd = tmp_path / "TODO_PRD.md"
    prd.write_text(
        "# Todo App\n\n"
        "## Entities\n"
        "- **Task**: title (text), done (boolean)\n\n"
        "### Pages\n"
        "- Dashboard: task stats\n",
        encoding="utf-8",
    )

    parsed = MarkdownPRDParser().parse(prd)

    assert parsed.title == "Todo App"
    assert parsed.sections["Entities"] == ["**Task**: title (text), done (boolean)"]
    assert parsed.sections["Pages"] == ["Dashboard: task stats"]


def test_qa_workflow_places_task_at_prompt_end():
    preview = QAWorkflow().build_prompt("how does login work?")

    assert preview.pack.specialist == "qa"
    assert preview.prompt.rstrip().endswith("how does login work?")
    assert "stub/example.py" in preview.prompt


@pytest.mark.asyncio
async def test_coordinator_routes_and_builds_qa_preview():
    result = await Coordinator(llm=FakeLLM(), qa_workflow=QAWorkflow()).handle(
        "explain authentication"
    )

    assert result.decision.intent == "qa"
    assert result.preview.rstrip().endswith("explain authentication")


def test_approval_accepts_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "y")
    request = ApprovalRequest(
        action_type="file_write",
        description="Create a generated file",
        risk_level="medium",
        preview="new file",
    )

    assert ask_approval(request) is True
