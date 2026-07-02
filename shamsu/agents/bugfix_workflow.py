"""
Bug fix workflow.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from shamsu.context.builder import ContextBuilder
from shamsu.interfaces import IContextBuilder, ILLMManager, IPatchEngine, ISearchAgent
from shamsu.llm.manager import LLMManager
from shamsu.patch.engine import PatchEngine, parse_unified_diff
from shamsu.types import ContextPack, SearchResult

BUGFIX_INSTRUCTIONS = """You are SHAMSU's bug fixer.
Output ONLY a unified diff.
Do not include prose, markdown fences, explanations, or commands.
Use --- a/path and +++ b/path headers.
Make the smallest targeted fix for the reported bug.
Do not refactor unrelated code."""

TRACEBACK_FILE_RE = re.compile(r'File "([^"]+)", line (\d+)')
PLAIN_LOCATION_RE = re.compile(r"(?P<path>[\w./\\-]+\.py):(?P<line>\d+)")
ERROR_LINE_RE = re.compile(r"^(?P<error>[A-Z][\w.]*Error|Exception|AssertionError):\s*(?P<message>.+)$")


@dataclass(frozen=True)
class TracebackLocation:
    file_path: str
    line: int


@dataclass(frozen=True)
class BugFixResult:
    request: str
    pack: ContextPack
    locations: list[TracebackLocation] = field(default_factory=list)
    diff_text: str = ""
    changed_files: list[str] = field(default_factory=list)
    applied: bool = False
    error: str = ""
    test_suggestion: str = "Re-run the failing test or command that produced the bug report."


class BugFixWorkflow:
    def __init__(
        self,
        workspace_root: Path,
        search: ISearchAgent,
        llm: ILLMManager | None = None,
        patch_engine: IPatchEngine | None = None,
        context_builder: IContextBuilder | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.search = search
        self.llm = llm or LLMManager()
        self.patch_engine = patch_engine or PatchEngine(self.workspace_root)
        self.context_builder = context_builder or ContextBuilder()

    async def run(self, report: str) -> BugFixResult:
        locations = parse_traceback_locations(report)
        pack = self._build_pack(report, locations)
        response = await self.llm.run_specialist("bugfix", pack)
        diff_text = _clean_diff(response.raw)
        ok, error = self.patch_engine.validate_diff(diff_text)
        if not ok:
            return BugFixResult(
                request=report,
                pack=pack,
                locations=locations,
                diff_text=diff_text,
                error=f"Invalid diff: {error}",
            )

        changed_files = _changed_files(diff_text)
        applied = self.patch_engine.apply(diff_text, self.workspace_root)
        if not applied:
            return BugFixResult(
                request=report,
                pack=pack,
                locations=locations,
                diff_text=diff_text,
                changed_files=changed_files,
                error="Patch was not applied.",
            )
        return BugFixResult(
            request=report,
            pack=pack,
            locations=locations,
            diff_text=diff_text,
            changed_files=changed_files,
            applied=True,
        )

    def _build_pack(self, report: str, locations: list[TracebackLocation]) -> ContextPack:
        results = _dedupe_results(self._search_bug_context(report, locations))
        request = (
            f"{BUGFIX_INSTRUCTIONS}\n\n"
            f"Bug report, traceback, or failing test output:\n{report.strip()}"
        )
        return self.context_builder.pack(
            results=results,
            request=request,
            task_id="bug-fix",
            step_id=1,
            specialist="bugfix",
        )

    def _search_bug_context(
        self,
        report: str,
        locations: list[TracebackLocation],
    ) -> list[SearchResult]:
        results: list[SearchResult] = []
        for query in _bug_queries(report, locations):
            results.extend(self.search.search(query, top_k=5))
        return results[:12]


def parse_traceback_locations(report: str) -> list[TracebackLocation]:
    seen: set[tuple[str, int]] = set()
    locations: list[TracebackLocation] = []
    for path, line_text in TRACEBACK_FILE_RE.findall(report):
        key = (path, int(line_text))
        if key not in seen:
            seen.add(key)
            locations.append(TracebackLocation(file_path=path, line=int(line_text)))
    for match in PLAIN_LOCATION_RE.finditer(report):
        key = (match.group("path"), int(match.group("line")))
        if key not in seen:
            seen.add(key)
            locations.append(TracebackLocation(file_path=key[0], line=key[1]))
    return locations


def _bug_queries(report: str, locations: list[TracebackLocation]) -> list[str]:
    queries = [report.strip()]
    queries.extend(location.file_path for location in locations)
    error_line = _last_error_line(report)
    if error_line:
        queries.append(error_line)
    return [query for query in _dedupe_strings(queries) if query]


def _last_error_line(report: str) -> str:
    for line in reversed(report.splitlines()):
        stripped = line.strip()
        if ERROR_LINE_RE.match(stripped):
            return stripped
    return ""


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


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique
