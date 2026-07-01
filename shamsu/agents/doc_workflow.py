"""
Documentation generation workflow.

This workflow is intentionally proposal-only until the approval-backed patch
apply layer lands. It uses indexed project context, asks the doc specialist for
README-style Markdown, and can produce a unified diff for review.
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path

from shamsu.context.builder import ContextBuilder
from shamsu.interfaces import IContextBuilder, ILLMManager, ISearchAgent
from shamsu.llm.manager import LLMManager
from shamsu.types import ContextPack, SearchResult

DOC_SYSTEM_INSTRUCTIONS = """Generate concise, accurate project documentation from
the indexed snippets only. Do not invent features. Include setup, run, test, and
important module notes when the context supports them. Output Markdown only."""


@dataclass(frozen=True)
class DocumentationProposal:
    request: str
    pack: ContextPack
    markdown: str
    diff_text: str
    raw_response: str


class DocumentationWorkflow:
    def __init__(
        self,
        search: ISearchAgent,
        llm: ILLMManager | None = None,
        context_builder: IContextBuilder | None = None,
    ) -> None:
        self.search = search
        self.llm = llm or LLMManager()
        self.context_builder = context_builder or ContextBuilder()

    async def propose_readme_update(
        self,
        existing_readme: str = "",
        request: str = "Generate README documentation for this project.",
        target_path: str = "README.md",
    ) -> DocumentationProposal:
        results = self._search_for_documentation_context(request)
        request_text = _build_doc_request(request, existing_readme)
        pack = self.context_builder.pack(
            results=results,
            request=request_text,
            task_id="doc-generation",
            step_id=1,
            specialist="doc_agent",
        )
        response = await self.llm.run_specialist("doc_agent", pack)
        markdown = _clean_markdown(response.raw)
        diff_text = build_readme_diff(existing_readme, markdown, target_path)
        return DocumentationProposal(
            request=request,
            pack=pack,
            markdown=markdown,
            diff_text=diff_text,
            raw_response=response.raw,
        )

    def _search_for_documentation_context(self, request: str) -> list[SearchResult]:
        seen: set[tuple[str, int, int]] = set()
        results: list[SearchResult] = []
        for query in _documentation_queries(request):
            for result in self.search.search(query, top_k=5):
                key = (result.file_path, result.line_start, result.line_end)
                if key in seen:
                    continue
                seen.add(key)
                results.append(result)
        return results[:12]


def build_readme_diff(existing_readme: str, proposed_readme: str, target_path: str) -> str:
    old_lines = _split_lines_for_diff(existing_readme)
    new_lines = _split_lines_for_diff(proposed_readme)
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"a/{_normalize_diff_path(target_path)}",
        tofile=f"b/{_normalize_diff_path(target_path)}",
        lineterm="",
    )
    return "\n".join(diff) + "\n"


def load_existing_readme(workspace: Path, relative_path: str = "README.md") -> str:
    readme_path = workspace / relative_path
    if not readme_path.exists() or not readme_path.is_file():
        return ""
    return readme_path.read_text(encoding="utf-8")


def _documentation_queries(request: str) -> list[str]:
    return [
        request,
        "install setup requirements run usage cli",
        "test pytest ruff verification",
        "main entrypoint command workflow",
    ]


def _build_doc_request(request: str, existing_readme: str) -> str:
    existing = existing_readme.strip()
    if existing:
        return (
            f"{DOC_SYSTEM_INSTRUCTIONS}\n\n"
            f"User documentation request: {request}\n\n"
            f"Existing README to improve:\n{existing}"
        )
    return f"{DOC_SYSTEM_INSTRUCTIONS}\n\nUser documentation request: {request}"


def _clean_markdown(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text + "\n"


def _split_lines_for_diff(text: str) -> list[str]:
    if not text:
        return []
    return text.rstrip("\n").splitlines()


def _normalize_diff_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")
