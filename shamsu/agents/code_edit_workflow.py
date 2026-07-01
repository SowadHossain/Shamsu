"""
Code edit workflow.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shamsu.context.builder import ContextBuilder
from shamsu.interfaces import IContextBuilder, ILLMManager, IPatchEngine, ISearchAgent
from shamsu.llm.manager import LLMManager
from shamsu.patch.engine import PatchEngine, parse_unified_diff
from shamsu.types import ContextPack

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
    test_suggestion: str = "Run the relevant project tests after reviewing the patch."


class CodeEditWorkflow:
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

    async def run(self, request: str) -> CodeEditResult:
        pack = self._build_pack(request)
        response = await self.llm.run_specialist("coder", pack)
        diff_text = _clean_diff(response.raw)
        ok, error = self.patch_engine.validate_diff(diff_text)
        if not ok:
            return CodeEditResult(
                request=request,
                pack=pack,
                diff_text=diff_text,
                error=f"Invalid diff: {error}",
            )

        changed_files = _changed_files(diff_text)
        applied = self.patch_engine.apply(diff_text, self.workspace_root)
        if not applied:
            return CodeEditResult(
                request=request,
                pack=pack,
                diff_text=diff_text,
                changed_files=changed_files,
                error="Patch was not applied.",
            )
        return CodeEditResult(
            request=request,
            pack=pack,
            diff_text=diff_text,
            changed_files=changed_files,
            applied=True,
        )

    def _build_pack(self, request: str) -> ContextPack:
        results = self.search.search(request, top_k=8)
        return self.context_builder.pack(
            results=results,
            request=f"{CODE_EDIT_INSTRUCTIONS}\n\nUser request: {request}",
            task_id="code-edit",
            step_id=1,
            specialist="coder",
        )


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
