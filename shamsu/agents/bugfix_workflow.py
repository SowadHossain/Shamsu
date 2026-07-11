"""Bug fix workflow."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from shamsu.abstract.context import build_codebase_memory_brief
from shamsu.agents.planner import create_plan
from shamsu.agents.rewrite_fallback import (
    full_file_results,
    lenient_diff_target_paths,
    mentioned_workspace_files,
    rewrite_files_fully,
)
from shamsu.context.builder import ContextBuilder
from shamsu.interfaces import IContextBuilder, ILLMManager, IPatchEngine, ISearchAgent
from shamsu.llm.council import run_council, should_convene_council
from shamsu.llm.manager import LLMManager
from shamsu.memory.service import MemoryService
from shamsu.patch.engine import PatchEngine, parse_file_patches, parse_unified_diff, _apply_hunks
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.tools.path_resolve import remap_diff_paths, resolve_reported_path
from shamsu.types import ContextPack, SearchResult

BUGFIX_INSTRUCTIONS = """You are SHAMSU's bug fixer.
Output ONLY a unified diff.
Do not include prose, markdown fences, explanations, or commands.
Use --- a/path and +++ b/path headers.
Make the smallest targeted fix for the reported bug.
Do not refactor unrelated code.
Read the current file context. Preserve existing imports, exports, and public APIs.

Repair checklist (follow in order):
1. Identify the single root error and the exact file it points at.
2. Use the REAL workspace path shown in the provided file context, not the path
   copied verbatim from the error - a build may report `src/App.tsx` when the
   file actually lives at `client/src/App.tsx`.
3. Base every hunk on the current file content given to you; match the existing
   lines exactly so the patch applies cleanly.
4. Change only what the root error requires."""

TRACEBACK_FILE_RE = re.compile(r'File "([^"]+)", line (\d+)')
PLAIN_LOCATION_RE = re.compile(r"(?P<path>[\w./\\-]+\.(?:py|ts|tsx|js|jsx|html|css)):(?P<line>\d+)(?::\d+)?")
TS_LOCATION_RE = re.compile(r"(?P<path>[\w./\\-]+\.(?:ts|tsx|js|jsx))\((?P<line>\d+),\d+\)")
ERROR_LINE_RE = re.compile(r"^(?P<error>[A-Z][\w.]*Error|Exception|AssertionError):\s*(?P<message>.+)$")
MISSING_EXPORT_RE = re.compile(
    r"(?:has no exported member|does not provide an export named|no exported member)\s+['\"]?(?P<name>[A-Za-z_$][\w$]*)['\"]?",
    re.IGNORECASE,
)
MODULE_PATH_RE = re.compile(r"(?:module|Module)\s+['\"](?P<module>[^'\"]+)['\"]|requested module ['\"](?P<requested>[^'\"]+)['\"]")
IMPORT_FROM_RE = re.compile(r"import\s+[^;\n]*\b(?P<name>[A-Za-z_$][\w$]*)\b[^;\n]*\s+from\s+['\"](?P<module>[^'\"]+)['\"]")
EXPORT_RE = re.compile(
    r"^\s*export\s+(?:declare\s+)?(?:(?:async\s+)?function|const|let|var|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)\b|"
    r"^\s*export\s*\{([^}]+)\}|^\s*export\s+default\b",
    re.MULTILINE,
)
SEARCH_REPLACE_RE = re.compile(r"(?s)FILE:\s*(?P<file>[^\n]+)\nSEARCH:\n(?P<search>.*?)\nREPLACE:\n(?P<replace>.*)")


@dataclass(frozen=True)
class TracebackLocation:
    file_path: str
    line: int


@dataclass(frozen=True)
class ImportExportError:
    missing_export: str
    module_path: str
    importing_file: str = ""


@dataclass(frozen=True)
class BugFixResult:
    request: str
    pack: ContextPack
    locations: list[TracebackLocation] = field(default_factory=list)
    diff_text: str = ""
    changed_files: list[str] = field(default_factory=list)
    applied: bool = False
    error: str = ""
    used_full_rewrite: bool = False
    verification_status: str = "Change applied, not yet verified."
    test_suggestion: str = "Re-run the failing test or command that produced the bug report."
    plan: str = ""
    # (reported_path, resolved_path) pairs where the model/error used a path that
    # did not exist and we remapped it to the real workspace file before applying.
    remapped_paths: list[tuple[str, str]] = field(default_factory=list)


class BugFixWorkflow:
    def __init__(
        self,
        workspace_root: Path,
        search: ISearchAgent,
        llm: ILLMManager | None = None,
        patch_engine: IPatchEngine | None = None,
        context_builder: IContextBuilder | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.search = search
        self.llm = llm or LLMManager()
        self.patch_engine = patch_engine or PatchEngine(self.workspace_root)
        self.context_builder = context_builder or ContextBuilder()
        self.memory_service = memory_service or MemoryService(self.workspace_root)

    async def run(self, report: str) -> BugFixResult:
        locations = parse_traceback_locations(report)
        import_error = parse_import_export_error(report)
        if import_error and not locations:
            locations = _locations_for_import_error(self.workspace_root, report, import_error)
        # Map every reported path to the real workspace path BEFORE reading files
        # or asking the model, so the pack is built from actual file content and
        # the model never fixes a file it cannot see (e.g. src/App.tsx that really
        # lives at client/src/App.tsx).
        locations = self._resolve_locations(locations)
        pack, searched_paths, plan_text = await self._build_pack(report, locations)
        target_paths = [location.file_path for location in locations]
        if should_convene_council(target_paths=target_paths):
            council_result = await run_council(self.llm, pack, specialist="bugfix")
            response = council_result.final
        else:
            response = await self.llm.run_specialist("bugfix", pack)

        remaps: dict[str, str] = {}
        diff_text = self._prepare_diff(response.raw, remaps)
        ok, error = self.patch_engine.validate_diff(diff_text)
        repair_attempts = 0
        max_repair_attempts = 4 if "autonomy" in report.lower() else 2
        while not ok and repair_attempts < max_repair_attempts:
            repair_attempts += 1
            repair_pack = self._build_repair_pack(
                report, diff_text, error or "Diff validation failed.", locations, searched_paths, plan_text,
            )
            repair_response = await self.llm.run_specialist("bugfix", repair_pack)
            diff_text = self._prepare_diff(repair_response.raw, remaps)
            ok, error = self.patch_engine.validate_diff(diff_text)
        remapped_paths = list(remaps.items())

        if ok:
            changed_files = _changed_files(diff_text)
            contract_error = _module_contract_error(self.workspace_root, diff_text, report)
            if contract_error:
                return BugFixResult(
                    request=report,
                    pack=pack,
                    locations=locations,
                    diff_text=diff_text,
                    error=contract_error,
                    verification_status="No file was changed.",
                    plan=plan_text,
                    remapped_paths=remapped_paths,
                )
            applied = self.patch_engine.apply(diff_text, self.workspace_root)
            return BugFixResult(
                request=report,
                pack=pack,
                locations=locations,
                diff_text=diff_text,
                changed_files=changed_files,
                applied=applied,
                error="" if applied else "Patch was not applied.",
                verification_status="Change applied, not yet verified." if applied else "No file was changed.",
                plan=plan_text,
                remapped_paths=remapped_paths,
            )

        fallback = _parse_search_replace(response.raw)
        if fallback:
            rewritten, fallback_error = _apply_unique_search_replace(self.workspace_root, fallback, self.patch_engine)
            if rewritten:
                return BugFixResult(
                    request=report,
                    pack=pack,
                    locations=locations,
                    diff_text=diff_text,
                    changed_files=[rewritten],
                    applied=True,
                    verification_status="Change applied, not yet verified.",
                    plan=plan_text,
                    remapped_paths=remapped_paths,
                )
            return BugFixResult(
                request=report,
                pack=pack,
                locations=locations,
                diff_text=diff_text,
                error=f"Invalid diff: {error}; targeted edit fallback refused: {fallback_error}",
                verification_status="No file was changed.",
                plan=plan_text,
                remapped_paths=remapped_paths,
            )
        # Resolve fallback targets to real workspace paths so the full-file
        # rewrite edits the existing file instead of creating a wrong-path
        # duplicate (src/App.tsx as a NEW file next to the real client/src/App.tsx).
        fallback_targets = self._resolve_paths(
            lenient_diff_target_paths(diff_text) or target_paths or searched_paths
        )
        rewritten = await rewrite_files_fully(
            llm=self.llm,
            context_builder=self.context_builder,
            patch_engine=self.patch_engine,
            workspace_root=self.workspace_root,
            request=report,
            target_paths=fallback_targets,
            specialist="bugfix",
            plan_text=plan_text,
        )
        if rewritten:
            return BugFixResult(
                request=report,
                pack=pack,
                locations=locations,
                diff_text=diff_text,
                changed_files=rewritten,
                applied=True,
                used_full_rewrite=True,
                verification_status="Change applied, not yet verified.",
                plan=plan_text,
                remapped_paths=remapped_paths,
            )
        return BugFixResult(
            request=report,
            pack=pack,
            locations=locations,
            diff_text=diff_text,
            error=f"Invalid diff: {error}. Targeted files: {', '.join(fallback_targets) or 'unknown'}. No file was changed.",
            verification_status="No file was changed.",
            plan=plan_text,
            remapped_paths=remapped_paths,
        )

    def _resolve_locations(self, locations: list[TracebackLocation]) -> list[TracebackLocation]:
        """Rewrite each location's path to the real workspace file when it can be
        resolved unambiguously; keep the original path otherwise (search still
        uses it as a query). Deduplicates on (resolved_path, line)."""
        resolved: list[TracebackLocation] = []
        seen: set[tuple[str, int]] = set()
        for location in locations:
            real = resolve_reported_path(self.workspace_root, location.file_path) or location.file_path
            key = (real, location.line)
            if key in seen:
                continue
            seen.add(key)
            resolved.append(TracebackLocation(file_path=real, line=location.line))
        return resolved

    def _resolve_paths(self, paths: list[str]) -> list[str]:
        return _dedupe_strings(
            [resolve_reported_path(self.workspace_root, path) or path for path in paths]
        )

    def _prepare_diff(self, raw: str, remaps: dict[str, str]) -> str:
        """Clean fences off a model diff, then remap any header path that does not
        exist to the real workspace file. Accumulates remaps across repair
        attempts so the caller can report exactly which paths were corrected."""
        diff_text, applied = remap_diff_paths(self.workspace_root, _clean_diff(raw))
        for reported, resolved in applied:
            remaps.setdefault(reported, resolved)
        return diff_text

    async def _build_pack(
        self, report: str, locations: list[TracebackLocation],
    ) -> tuple[ContextPack, list[str], str]:
        results = _dedupe_results(self._search_bug_context(report, locations))
        target_paths = _target_paths(results)
        # Ground the first attempt in the real, full content of the files being
        # fixed (traceback targets + searched files), so the model edits against
        # ground truth instead of inventing lines. Previously only the repair
        # pass (after a rejected diff) saw full files; doing it up front prevents
        # the hallucinated-diff failure in the first place.
        grounding_paths = _dedupe_strings(
            [loc.file_path for loc in locations]
            + mentioned_workspace_files(self.workspace_root, report)
            + target_paths
        )
        results = full_file_results(self.workspace_root, grounding_paths) + results
        memory_brief = build_codebase_memory_brief(self.workspace_root, target_paths)
        graphiti_brief = self.memory_service.render_relevant(report)
        plan = await create_plan(self.llm, self.context_builder, results, goal=report, task_id="bugfix-plan")
        request = (
            f"{BUGFIX_INSTRUCTIONS}\n\n"
            + (f"{memory_brief}\n\n" if memory_brief else "")
            + (f"{graphiti_brief}\n\n" if graphiti_brief else "")
            + f"Plan from planner model:\n{plan.text}\n\n"
            + f"Bug report, traceback, or failing test output:\n{report.strip()}"
        )
        pack = self.context_builder.pack(results=results, request=request, task_id="bug-fix", step_id=2, specialist="bugfix")
        return pack, target_paths, plan.text

    def _build_repair_pack(
        self,
        report: str,
        bad_diff: str,
        validation_error: str,
        locations: list[TracebackLocation],
        searched_paths: list[str],
        plan_text: str = "",
    ) -> ContextPack:
        paths = _dedupe_strings([location.file_path for location in locations] + searched_paths)
        results = _full_file_results(self.workspace_root, paths)
        request = (
            f"{BUGFIX_INSTRUCTIONS}\n\n"
            + (f"Plan from planner model:\n{plan_text}\n\n" if plan_text.strip() else "")
            + "The previous unified diff was rejected by PatchEngine.\n"
            f"Patch validation error: {validation_error}\n\n"
            f"Original bug report:\n{report.strip()}\n\n"
            f"Rejected diff:\n{bad_diff.strip()}\n\n"
            "Return a corrected unified diff only."
        )
        return self.context_builder.pack(results=results, request=request, task_id="bug-fix-diff-repair", step_id=3, specialist="bugfix")

    def _search_bug_context(self, report: str, locations: list[TracebackLocation]) -> list[SearchResult]:
        boost_paths = [location.file_path for location in locations]
        results: list[SearchResult] = _location_snippets(self.workspace_root, locations)
        import_error = parse_import_export_error(report)
        if import_error:
            results.extend(_full_file_results(self.workspace_root, _import_export_paths(self.workspace_root, report, import_error)))
        for query in _bug_queries(report, locations):
            results.extend(self.search.search(query, top_k=5, boost_paths=boost_paths))
        return results[:16]


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
    for match in TS_LOCATION_RE.finditer(report):
        key = (match.group("path"), int(match.group("line")))
        if key not in seen:
            seen.add(key)
            locations.append(TracebackLocation(file_path=key[0], line=key[1]))
    return locations


def parse_import_export_error(report: str) -> ImportExportError | None:
    missing = MISSING_EXPORT_RE.search(report)
    module = MODULE_PATH_RE.search(report)
    if not missing or not module:
        return None
    importing_file = parse_traceback_locations(report)[0].file_path if parse_traceback_locations(report) else ""
    return ImportExportError(
        missing_export=missing.group("name"),
        module_path=module.group("module") or module.group("requested") or "",
        importing_file=importing_file,
    )


def scan_ts_exports(content: str) -> set[str]:
    exports: set[str] = set()
    for match in EXPORT_RE.finditer(content):
        if match.group(1):
            exports.add(match.group(1))
        elif match.group(2):
            for item in match.group(2).split(","):
                name = item.strip().split(" as ")[-1].strip()
                if name:
                    exports.add(name)
        else:
            exports.add("default")
    return exports


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
    """Extract a unified diff from a model reply, tolerating markdown fences and
    prose. Models often wrap the diff in ```diff ... ``` (sometimes after a
    sentence of explanation), which used to fail with 'Invalid hunk line marker:
    ```'. We take the first fenced block if present, drop any stray fence lines,
    and trim leading prose before the diff header."""
    text = raw.strip()
    # Prefer the contents of the first fenced code block, wherever it appears.
    fenced = re.search(r"```[a-zA-Z0-9_+-]*\n(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    # Drop any surviving fence lines (unbalanced/nested fences).
    lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
    # Trim any leading prose before the real diff starts.
    diff_starts = ("--- ", "+++ ", "diff --git", "*** ", "Index: ", "@@")
    for index, line in enumerate(lines):
        if line.startswith(diff_starts):
            lines = lines[index:]
            break
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


def _target_paths(results: list[SearchResult]) -> list[str]:
    paths: list[str] = []
    for result in results:
        if result.file_path not in paths:
            paths.append(result.file_path)
    return paths


def _location_snippets(workspace_root: Path, locations: list[TracebackLocation]) -> list[SearchResult]:
    snippets: list[SearchResult] = []
    for location in locations:
        target = (workspace_root / location.file_path).resolve()
        try:
            target.relative_to(workspace_root)
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except (OSError, ValueError):
            continue
        start = max(location.line - 6, 1)
        end = min(location.line + 5, len(lines))
        content = "\n".join(lines[start - 1:end])
        snippets.append(
            SearchResult(
                file_path=location.file_path,
                language=target.suffix.lstrip(".") or "text",
                line_start=start,
                line_end=end,
                content=content,
                score=10.0,
            )
        )
    return snippets


def _full_file_results(workspace_root: Path, paths: list[str]) -> list[SearchResult]:
    results: list[SearchResult] = []
    for path in _dedupe_strings(paths):
        target = (workspace_root / path).resolve()
        try:
            target.relative_to(workspace_root)
            content = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        results.append(
            SearchResult(
                file_path=path,
                language=target.suffix.lstrip(".") or "text",
                line_start=1,
                line_end=max(1, len(content.splitlines())),
                content=content,
                score=20.0,
            )
        )
    return results


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _locations_for_import_error(workspace_root: Path, report: str, error: ImportExportError) -> list[TracebackLocation]:
    return [TracebackLocation(path, 1) for path in _import_export_paths(workspace_root, report, error)]


def _import_export_paths(workspace_root: Path, report: str, error: ImportExportError) -> list[str]:
    importer = error.importing_file or _find_importing_file(workspace_root, error.module_path, error.missing_export)
    paths = [importer] if importer else []
    exporter = _resolve_module_path(workspace_root, importer, error.module_path)
    if exporter:
        paths.append(exporter)
    return paths


def _find_importing_file(workspace_root: Path, module_path: str, name: str) -> str:
    for path in workspace_root.rglob("*"):
        if path.suffix.lower() not in {".ts", ".tsx", ".js", ".jsx"} or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if module_path in text and name in text:
            return path.relative_to(workspace_root).as_posix()
    return ""


def _resolve_module_path(workspace_root: Path, importer: str, module_path: str) -> str:
    module_path = module_path.replace("\\", "/")
    if module_path.startswith("/"):
        module_path = module_path.lstrip("/")
    bases: list[Path] = []
    if importer:
        bases.append((workspace_root / importer).parent)
    bases.append(workspace_root)
    if module_path.startswith("."):
        candidates = [base / module_path for base in bases]
    else:
        candidates = [workspace_root / module_path]
    suffixes = ["", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js", "/index.jsx"]
    for candidate in candidates:
        for suffix in suffixes:
            path = Path(str(candidate) + suffix).resolve()
            if path.is_file():
                try:
                    return path.relative_to(workspace_root).as_posix()
                except ValueError:
                    return ""
    return ""


def _module_contract_error(workspace_root: Path, diff_text: str, report: str) -> str:
    sandbox = Sandbox(workspace_root)
    try:
        patches = parse_file_patches(diff_text, sandbox)
    except Exception as exc:
        return f"Invalid diff: {exc}"
    for patch in patches:
        path = patch.display_path
        if not path.endswith((".ts", ".tsx", ".js", ".jsx")) or patch.is_create or patch.is_delete:
            continue
        target = (workspace_root / path).resolve()
        try:
            before = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return f"Target file could not be read before patch: {path}"
        before_exports = scan_ts_exports(before)
        after_lines = _apply_hunks(before.splitlines(), patch.hunks)
        after_exports = scan_ts_exports("\n".join(after_lines))
        removed = sorted(before_exports - after_exports)
        if removed and not _explicitly_allows_export_removal(report):
            return f"Patch rejected: it removes existing export(s) from {path}: {', '.join(removed)}"
    return ""


def _explicitly_allows_export_removal(report: str) -> bool:
    text = report.lower()
    return "remove export" in text or "rename export" in text or "delete export" in text


def _parse_search_replace(raw: str) -> tuple[str, str, str] | None:
    match = SEARCH_REPLACE_RE.search(raw)
    if not match:
        return None
    return match.group("file").strip(), match.group("search"), match.group("replace")


def _apply_unique_search_replace(workspace_root: Path, fallback: tuple[str, str, str], patch_engine: IPatchEngine | None = None) -> tuple[str, str]:
    file_path, search, replace = fallback
    try:
        Sandbox(workspace_root).validate(file_path)
    except SecurityError as exc:
        return "", str(exc)
    target = (workspace_root / file_path).resolve()
    try:
        content = target.read_text(encoding="utf-8")
    except OSError as exc:
        return "", str(exc)
    count = content.count(search)
    if count != 1:
        return "", f"target text matched {count} time(s), expected exactly once"
    approval_func = getattr(patch_engine, "approval_func", None)
    result = AgentToolRegistry(workspace_root, approval_func=approval_func).write_file(
        file_path,
        content.replace(search, replace, 1),
        overwrite=True,
    )
    if not result.ok:
        return "", result.message
    return file_path, ""
