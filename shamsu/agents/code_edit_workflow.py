"""
Code edit workflow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shamsu.abstract.context import build_codebase_memory_brief
from shamsu.agents.planner import create_plan
from shamsu.agents.rewrite_fallback import lenient_diff_target_paths, rewrite_files_fully
from shamsu.context.builder import ContextBuilder
from shamsu.interfaces import IContextBuilder, ILLMManager, IPatchEngine, ISearchAgent
from shamsu.llm.council import run_council, should_convene_council
from shamsu.llm.manager import LLMManager
from shamsu.memory.service import MemoryService
from shamsu.patch.engine import PatchEngine, parse_unified_diff
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
    used_full_rewrite: bool = False
    test_suggestion: str = "Run the relevant project tests after reviewing the patch."
    plan: str = ""


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
        pack, target_paths, plan_text = await self._build_pack(request)
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
            applied = self.patch_engine.apply(diff_text, self.workspace_root)
            return CodeEditResult(
                request=request,
                pack=pack,
                diff_text=diff_text,
                changed_files=changed_files,
                applied=applied,
                error="" if applied else "Patch was not applied.",
                plan=plan_text,
            )

        # The diff was malformed (a formatting failure — common with small
        # models), NOT a user denial. Instead of aborting with unchanged/broken
        # code, rewrite the whole target file(s) from scratch and overwrite them.
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
        return pack, target_paths, plan.text


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


def _target_paths(results: list[SearchResult]) -> list[str]:
    paths: list[str] = []
    for result in results:
        if result.file_path not in paths:
            paths.append(result.file_path)
    return paths
