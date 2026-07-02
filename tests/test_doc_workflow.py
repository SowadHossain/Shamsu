from __future__ import annotations

import pytest

from shamsu.agents.doc_workflow import (
    DocumentationApplyResult,
    DocumentationWorkflow,
    build_readme_diff,
    load_existing_readme,
)
from shamsu.patch.engine import PatchEngine
from shamsu.types import ContextPack, LLMResponse, SearchResult


class FakeSearch:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        self.queries.append(query)
        return [
            SearchResult(
                file_path="shamsu/cli/repl.py",
                language="python",
                line_start=1,
                line_end=12,
                content="def main():\n    print('SHAMSU')",
                score=0.8,
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
            raw="# SHAMSU\n\n## Run\nUse `shamsu` from the project environment.\n",
            format="text",
            model_used="fake-doc-agent",
        )


@pytest.mark.asyncio
async def test_documentation_workflow_uses_indexed_context_and_doc_agent():
    search = FakeSearch()
    llm = FakeLLM()

    proposal = await DocumentationWorkflow(search=search, llm=llm).propose_readme_update(
        existing_readme="# Old\n",
        request="document the CLI",
    )

    assert search.queries[0] == "document the CLI"
    assert "install setup requirements" in search.queries[1]
    assert llm.specialist == "doc_agent"
    assert llm.pack is not None
    assert llm.pack.snippets[0].file_path == "shamsu/cli/repl.py"
    assert proposal.markdown.startswith("# SHAMSU")
    assert "--- a/README.md" in proposal.diff_text
    assert "+++ b/README.md" in proposal.diff_text
    assert "+## Run" in proposal.diff_text


def test_build_readme_diff_creates_unified_diff_for_review():
    diff = build_readme_diff("# Old\n", "# New\n\nUsage\n", "docs/README.md")

    assert diff.startswith("--- a/docs/README.md\n+++ b/docs/README.md")
    assert "-# Old" in diff
    assert "+# New" in diff
    assert "+Usage" in diff


def test_load_existing_readme_returns_empty_string_when_missing(tmp_path):
    assert load_existing_readme(tmp_path) == ""


def test_load_existing_readme_reads_file(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Project\n", encoding="utf-8")

    assert load_existing_readme(tmp_path) == "# Project\n"


@pytest.mark.asyncio
async def test_documentation_workflow_applies_readme_diff_through_patch_engine(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Old\n", encoding="utf-8")

    result = await DocumentationWorkflow(
        search=FakeSearch(),
        llm=FakeLLM(),
        workspace_root=tmp_path,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: True),
    ).apply_readme_update(request="document the CLI")

    assert isinstance(result, DocumentationApplyResult)
    assert result.applied is True
    assert result.error == ""
    assert result.changed_files == ["README.md"]
    assert readme.read_text(encoding="utf-8").startswith("# SHAMSU")


@pytest.mark.asyncio
async def test_documentation_workflow_reports_denied_apply(tmp_path):
    readme = tmp_path / "README.md"
    readme.write_text("# Old\n", encoding="utf-8")

    result = await DocumentationWorkflow(
        search=FakeSearch(),
        llm=FakeLLM(),
        workspace_root=tmp_path,
        patch_engine=PatchEngine(tmp_path, approval_func=lambda _request: False),
    ).apply_readme_update(request="document the CLI")

    assert result.applied is False
    assert result.error == "Patch was not applied."
    assert result.changed_files == ["README.md"]
    assert readme.read_text(encoding="utf-8") == "# Old\n"
