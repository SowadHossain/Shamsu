from __future__ import annotations

import pytest

from shamsu.agents.audit_workflow import AuditWorkflow, parse_audit_findings
from shamsu.types import ContextPack, LLMResponse, SearchResult


class FakeSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return [
            SearchResult(
                file_path="app/auth.py",
                language="python",
                line_start=10,
                line_end=16,
                content="def login(password):\n    return password == 'admin'",
                score=0.9,
            )
        ]

    def symbol_lookup(self, name: str) -> list[SearchResult]:
        return []

    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return self.search(query, top_k=top_k)


class FakeLLM:
    def __init__(self) -> None:
        self.specialist = ""
        self.pack: ContextPack | None = None

    async def route(self, prompt: str, project_summary: str):  # pragma: no cover
        raise NotImplementedError

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        self.specialist = specialist
        self.pack = pack
        return LLMResponse(
            raw=(
                '{"findings":[{"severity":"high","file_path":"app/auth.py",'
                '"line_start":11,"category":"security",'
                '"reason":"Hard-coded password bypasses real authentication.",'
                '"recommendation":"Use hashed credentials and Django auth."}]}'
            ),
            format="json",
            model_used="fake-reviewer",
        )


@pytest.mark.asyncio
async def test_audit_workflow_uses_indexed_search_and_reviewer_specialist():
    search = FakeSearch()
    llm = FakeLLM()

    report = await AuditWorkflow(search=search, llm=llm).run("audit auth")

    assert search.queries[0] == "audit auth"
    assert "security password secret" in search.queries[1]
    assert llm.specialist == "reviewer"
    assert llm.pack is not None
    assert llm.pack.snippets[0].file_path == "app/auth.py"
    assert report.findings[0].severity == "high"
    assert report.findings[0].file_path == "app/auth.py"
    assert report.findings[0].line_start == 11
    assert "Hard-coded password" in report.findings[0].reason


def test_parse_audit_findings_accepts_json_list_and_normalizes_severity():
    raw = (
        '[{"severity":"critical","file_path":"app.py","line_start":"7",'
        '"category":"bug","reason":"Bad state handling."}]'
    )

    findings = parse_audit_findings(raw)

    assert len(findings) == 1
    assert findings[0].severity == "medium"
    assert findings[0].line_start == 7


def test_parse_audit_findings_ignores_incomplete_items():
    findings = parse_audit_findings('{"findings":[{"severity":"low"},{"file_path":"x.py"}]}')

    assert findings == []
