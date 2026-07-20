"""
Code edit workflow.
"""
from __future__ import annotations

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
from shamsu.patch.engine import PatchEngine, parse_unified_diff
from shamsu.patch.sanitize import sanitize_model_diff
from shamsu.patch.types import apply_diff_with_result
from shamsu.templates.frontend import frontend_prompt_rules
from shamsu.types import ContextPack, SearchResult

CODE_EDIT_INSTRUCTIONS = """You are SHAMSU's code editor.
Output ONLY a unified diff.
Do not include prose, markdown fences, explanations, or commands.
Use --- a/path and +++ b/path headers.
Keep changes minimal and directly related to the user request."""


@dataclass(frozen=True)
class CodeEditResult:
    request: str
    pack: ContextPack
    diff_text: str = ""
    changed_files: list[str] = field(default_factory=list)
    applied: bool = False
    error: str = ""
    mutation_status: str = ""
    used_full_rewrite: bool = False
    test_suggestion: str = "Run the relevant project tests after reviewing the patch."
    plan: str = ""
    # Set when the planner judged a decision here is the USER's to make (J1/J6):
    # the workflow stops BEFORE the coder runs and the caller asks. Previously
    # the planner's verdict was computed and then silently ignored on this path.
    needs_input: bool = False
    question: str = ""
    options: list[dict[str, str]] = field(default_factory=list)


class CodeEditWorkflow:
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

    async def run(self, request: str) -> CodeEditResult:
        pack, target_paths, plan = await self._build_pack(request)
        plan_text = plan.text
        # The planner says this needs the user's decision (sessions vs JWT,
        # ambiguous target, destructive scope): stop before generating a diff
        # against a guess. The caller turns this into a pending question.
        if plan.needs_input and plan.question:
            return CodeEditResult(
                request=request,
                pack=pack,
                plan=plan_text,
                needs_input=True,
                question=plan.question,
                options=list(plan.options),
            )
        if should_convene_council(target_paths=target_paths):
            council_result = await run_council(self.llm, pack, specialist="coder")
            response = council_result.final
        else:
            response = await self.llm.run_specialist("coder", pack)
        diff_text = _clean_diff(response.raw)
        ok, error = self.patch_engine.validate_diff(diff_text)
        if ok:
            # A well-formed diff — apply it (this path honours approval/denial).
            changed_files = _changed_files(diff_text)
            mutation = apply_diff_with_result(self.patch_engine, diff_text, self.workspace_root)
            # Applied, or the user denied it: both are final. Only a well-formed
            # diff that FAILED to apply (context mismatch - the model's context
            # lines don't match the file) falls through to the rewrite below,
            # the same recovery a malformed diff gets.
            if mutation.ok or mutation.status == "denied":
                return CodeEditResult(
                    request=request,
                    pack=pack,
                    diff_text=diff_text,
                    changed_files=changed_files,
                    applied=mutation.ok,
                    error=mutation.error,
                    mutation_status=mutation.status,
                    plan=plan_text,
                )
            if not error:
                error = mutation.error or "Patch did not apply to the target file."

        # The diff was malformed (a formatting failure — common with small
        # models) or well-formed but unappliable, NOT a user denial. Instead of
        # aborting with unchanged/broken code, rewrite the whole target file(s)
        # from scratch and overwrite them.
        targets = lenient_diff_target_paths(diff_text) or target_paths
        rewritten = await rewrite_files_fully(
            llm=self.llm,
            context_builder=self.context_builder,
            patch_engine=self.patch_engine,
            workspace_root=self.workspace_root,
            request=request,
            target_paths=targets,
            specialist="coder",
            plan_text=plan_text,
        )
        if rewritten:
            return CodeEditResult(
                request=request,
                pack=pack,
                diff_text=diff_text,
                changed_files=rewritten,
                applied=True,
                used_full_rewrite=True,
                plan=plan_text,
            )
        return CodeEditResult(
            request=request,
            pack=pack,
            diff_text=diff_text,
            error=f"Invalid diff: {error}",
            plan=plan_text,
        )

    async def _build_pack(self, request: str) -> tuple[ContextPack, list[str], str]:
        results = self.search.search(request, top_k=8)
        target_paths = _target_paths(results)
        # Ground the model in the REAL current content of the files it's about
        # to edit (not just index snippets), so it can't invent lines that
        # aren't there. Include files the user named explicitly ("edit calc.py")
        # even when search misses them. Full files lead; ContextBuilder dedupes
        # the overlapping snippets. First-attempt version of what the repair
        # path already did only after a failure.
        grounding_paths = _dedupe_strings(
            mentioned_workspace_files(self.workspace_root, request) + target_paths
        )
        results = full_file_results(self.workspace_root, grounding_paths) + results
        memory_brief = build_codebase_memory_brief(self.workspace_root, target_paths)
        graphiti_brief = self.memory_service.render_relevant(request)
        plan = await create_plan(self.llm, self.context_builder, results, goal=request, task_id="code-edit-plan")
        prompt_request = (
            f"{CODE_EDIT_INSTRUCTIONS}\n\n"
            f"{frontend_prompt_rules()}\n\n"
            + (f"{memory_brief}\n\n" if memory_brief else "")
            + (f"{graphiti_brief}\n\n" if graphiti_brief else "")
            + f"Plan from planner model:\n{plan.text}\n\n"
            + f"User request: {request}"
        )
        pack = self.context_builder.pack(
            results=results,
            request=prompt_request,
            task_id="code-edit",
            step_id=2,
            specialist="coder",
        )
        return pack, target_paths, plan


def _clean_diff(raw: str) -> str:
    # Strip any <think> reasoning first (a reasoning model would otherwise embed
    # its trace in the diff) via the shared model-I/O boundary, then remove a
    # ``` fence wrapping the whole diff.
    return sanitize_model_diff(raw)


def _changed_files(diff_text: str) -> list[str]:
    return [patch.display_path for patch in parse_unified_diff(diff_text)]


def _target_paths(results: list[SearchResult]) -> list[str]:
    paths: list[str] = []
    for result in results:
        if result.file_path not in paths:
            paths.append(result.file_path)
    return paths


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out
