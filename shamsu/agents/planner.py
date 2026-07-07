"""Shared planner step for the two-model (planner -> coder) architecture.

Every file-mutating workflow (CodeEditWorkflow, BugFixWorkflow,
TestGenerationWorkflow, DocumentationWorkflow) calls `create_plan()` once,
before its own coder-style specialist call, per
`agent context/prompts/pipeline.md` section 3. The planner never writes
files or diffs - it only produces a short plan whose text is folded into the
coder-style specialist's own request. This module intentionally does not
touch PatchEngine, ContextBuilder's `_format_pack`, or the structured
`ChangePlan`/`execute_change_request` contract - see the approved plan for why
that's a separate, larger change.
"""
from __future__ import annotations

from dataclasses import dataclass

from shamsu.interfaces import IContextBuilder, ILLMManager
from shamsu.types import ContextPack, SearchResult

PLANNER_INSTRUCTIONS = """You are SHAMSU's planner.
Review the task and the code context below, then produce a short plan:
- the relevant file(s) and approach
- risks or constraints worth flagging
- a shell command that would verify the change, if one is obvious from context
Do not write code, diffs, or markdown. Keep it under 10 lines of plain text."""


@dataclass(frozen=True)
class PlanResult:
    text: str
    pack: ContextPack


async def create_plan(
    llm: ILLMManager,
    context_builder: IContextBuilder,
    results: list[SearchResult],
    goal: str,
    task_id: str,
) -> PlanResult:
    pack = context_builder.pack(
        results=results,
        request=f"{PLANNER_INSTRUCTIONS}\n\nTask: {goal.strip()}",
        task_id=task_id,
        step_id=1,
        specialist="planner",
    )
    response = await llm.run_specialist("planner", pack)
    return PlanResult(text=response.raw.strip(), pack=pack)
