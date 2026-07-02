"""
Test generation workflow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shamsu.context.builder import ContextBuilder
from shamsu.interfaces import (
    ICommandRunner,
    IContextBuilder,
    ILLMManager,
    IPatchEngine,
    ISearchAgent,
)
from shamsu.llm.manager import LLMManager
from shamsu.patch.engine import PatchEngine, parse_unified_diff
from shamsu.tools.executor import CommandRunner
from shamsu.types import ContextPack, SearchResult, TestRunResult

TEST_GEN_INSTRUCTIONS = """You are SHAMSU's test writer.
Output ONLY a unified diff.
Do not include prose, markdown fences, explanations, or commands.
Use --- a/path and +++ b/path headers.
Generate pytest-compatible tests unless the project context clearly uses another Python test framework.
Keep tests focused on the selected file, function, class, or API endpoint."""


@dataclass(frozen=True)
class TestGenerationResult:
    request: str
    pack: ContextPack
    diff_text: str = ""
    changed_files: list[str] = field(default_factory=list)
    applied: bool = False
    test_result: TestRunResult | None = None
    error: str = ""


class TestGenerationWorkflow:
    __test__ = False

    def __init__(
        self,
        workspace_root: Path,
        search: ISearchAgent,
        llm: ILLMManager | None = None,
        patch_engine: IPatchEngine | None = None,
        context_builder: IContextBuilder | None = None,
        command_runner: ICommandRunner | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.search = search
        self.llm = llm or LLMManager()
        self.patch_engine = patch_engine or PatchEngine(self.workspace_root)
        self.context_builder = context_builder or ContextBuilder()
        self.command_runner = command_runner

    async def run(self, request: str, run_after_apply: bool = False) -> TestGenerationResult:
        pack = self._build_pack(request)
        response = await self.llm.run_specialist("test_gen", pack)
        diff_text = _clean_diff(response.raw)
        ok, error = self.patch_engine.validate_diff(diff_text)
        if not ok:
            return TestGenerationResult(
                request=request,
                pack=pack,
                diff_text=diff_text,
                error=f"Invalid diff: {error}",
            )

        changed_files = _changed_files(diff_text)
        applied = self.patch_engine.apply(diff_text, self.workspace_root)
        if not applied:
            return TestGenerationResult(
                request=request,
                pack=pack,
                diff_text=diff_text,
                changed_files=changed_files,
                error="Patch was not applied.",
            )

        test_result = None
        if run_after_apply:
            runner = self.command_runner or CommandRunner(self.workspace_root)
            test_result = runner.run_tests(self.workspace_root)

        return TestGenerationResult(
            request=request,
            pack=pack,
            diff_text=diff_text,
            changed_files=changed_files,
            applied=True,
            test_result=test_result,
        )

    def _build_pack(self, request: str) -> ContextPack:
        results = _dedupe_results(self._search_test_context(request))
        return self.context_builder.pack(
            results=results,
            request=f"{TEST_GEN_INSTRUCTIONS}\n\nTest request: {request}",
            task_id="test-generation",
            step_id=1,
            specialist="test_gen",
        )

    def _search_test_context(self, request: str) -> list[SearchResult]:
        results: list[SearchResult] = []
        for query in _test_queries(request):
            results.extend(self.search.search(query, top_k=5))
        return results[:12]


def _test_queries(request: str) -> list[str]:
    return [
        request,
        f"{request} tests pytest fixtures",
        "pytest tests assertions fixtures",
    ]


def _clean_diff(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return text + "\n" if text else ""


def _changed_files(diff_text: str) -> list[str]:
    return [patch.display_path for patch in parse_unified_diff(diff_text)]


def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[tuple[str, int, int]] = set()
    unique: list[SearchResult] = []
    for result in results:
        key = (result.file_path, result.line_start, result.line_end)
        if key in seen:
            continue
        seen.add(key)
        unique.append(result)
    return unique
