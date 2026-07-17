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

The plan is now requested as SCHEMA-CONSTRAINED JSON rather than free prose,
and it carries one extra field: whether the task needs a decision only the user
can make. Two reasons:

* Free planner prose was the last model output still spliced raw into a coder's
  prompt with no validation - a hallucinated file became trusted context (C1).
* Asking upfront had no home. A prompt-only nudge to `ask_user` measurably did
  NOT make a 7B model ask about a design decision (the
  `ask_before_choosing_an_approach` eval stayed red): mid-loop, a model that can
  always do *something* just does it. The planner call already happens on every
  request, so deciding "is this the user's call?" *before* work starts costs no
  extra model call (J6).

Schema support is optional: an LLM without `generate_structured` (test doubles,
narrower interfaces) falls back to the original free-text `run_specialist` path,
so this never hard-depends on a capability the interface doesn't promise.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from json_repair import repair_json

from shamsu.interfaces import IContextBuilder, ILLMManager
from shamsu.types import ContextPack, SearchResult

PLANNER_INSTRUCTIONS = """You are SHAMSU's planner.
Review the task and the code context below, then produce a short plan:
- the relevant file(s) and approach
- risks or constraints worth flagging
- a shell command that would verify the change, if one is obvious from context
Do not write code, diffs, or markdown. Keep it under 10 lines of plain text."""

PLANNER_SYSTEM = """You are SHAMSU's planner. You never write code or files - you plan, and you
decide whether the user must choose something first. Output ONLY JSON matching the schema.

plan: a short plan (under 10 lines) - the relevant real file(s), the approach, risks, and a
verify command if one is obvious. Reference only files present in the context; never invent
files or frameworks.

needs_input: true ONLY when the task cannot be done well without a decision that is the USER's
to make, not yours:
- choosing between valid approaches or designs the task does not specify
- naming, scope, or product behavior the task leaves open
- anything destructive or hard to undo where the target is ambiguous
Set it false when the task is clear enough to just do, even if details remain - you are expected
to use good judgment on ordinary implementation choices. Do not ask about things you can look up.

question: when needs_input is true, the single question to ask. Concrete, not "please clarify".
options: 2-4 concrete choices for that question, when choices exist."""

PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "plan": {"type": "string"},
        "needs_input": {"type": "boolean"},
        "question": {"type": "string"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["label"],
            },
        },
    },
    "required": ["plan"],
}


@dataclass(frozen=True)
class PlanResult:
    text: str
    pack: ContextPack
    # Set when the planner judged that a decision belongs to the user. The
    # caller decides what to do with it (the chat loop asks and ends the turn);
    # workflows that ignore these fields keep the old behavior exactly.
    needs_input: bool = False
    question: str = ""
    options: list[dict[str, str]] = field(default_factory=list)


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
    structured = await _structured_plan(llm, pack, goal)
    if structured is not None:
        return structured
    # Fallback: free-text planner (an LLM without schema support, or a schema
    # call that produced nothing usable). Never fail the caller over planning.
    response = await llm.run_specialist("planner", pack)
    return PlanResult(text=response.raw.strip(), pack=pack)


async def _structured_plan(
    llm: ILLMManager, pack: ContextPack, goal: str
) -> PlanResult | None:
    generate = getattr(llm, "generate_structured", None)
    if not callable(generate):
        return None
    try:
        raw = await generate("planner", PLANNER_SYSTEM, _prompt_from_pack(pack, goal), PLAN_SCHEMA)
    except Exception:
        return None
    data = _loads(raw)
    if not isinstance(data, dict):
        return None
    plan_text = str(data.get("plan") or "").strip()
    question = str(data.get("question") or "").strip()
    needs_input = bool(data.get("needs_input")) and bool(question)
    if not plan_text and not needs_input:
        return None
    return PlanResult(
        text=plan_text,
        pack=pack,
        needs_input=needs_input,
        question=question,
        options=_options_from(data.get("options")),
    )


def _prompt_from_pack(pack: ContextPack, goal: str) -> str:
    parts = [f"## Task\n{goal.strip()}"]
    snippets = "\n\n".join(
        f"### {snippet.file_path}\n{snippet.content}" for snippet in (pack.snippets or [])[:6]
    )
    parts.append(f"## Code context\n{snippets or '(no code context found)'}")
    if pack.prd_context:
        parts.append(f"## Product context\n{pack.prd_context[:4000]}")
    if pack.error_context:
        parts.append(f"## Error context\n{pack.error_context[:2000]}")
    parts.append("Produce the plan JSON now.")
    return "\n\n".join(parts)


def _options_from(value: Any) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if not isinstance(value, list):
        return out
    for item in value[:4]:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if label:
            out.append({"label": label, "description": str(item.get("description") or "").strip()})
    return out


def _loads(raw: str) -> Any:
    """Parse the model's JSON, repairing the near-misses small models produce."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return repair_json(text, return_objects=True)
    except Exception:
        return None
