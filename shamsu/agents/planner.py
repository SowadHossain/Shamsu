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

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from json_repair import repair_json

from shamsu.agents.plan_mode import workspace_source_files
from shamsu.interfaces import IContextBuilder, ILLMManager
from shamsu.types import ContextPack, SearchResult

PLANNER_INSTRUCTIONS = """You are SHAMSU's planner.
Review the task and the code context below, then produce a short plan:
- the relevant file(s) and approach
- risks or constraints worth flagging
- a shell command that would verify the change, if one is obvious from context
Do not write code, diffs, or markdown. Keep it under 10 lines of plain text."""

DECISION_SYSTEM = """You decide ONE thing about a coding task: must the user choose something
before work starts? You do not plan and you do not write code. Output ONLY JSON matching the
schema.

needs_input: true ONLY when the task cannot be done well without a decision that is the USER's
to make, not yours:
- choosing between valid approaches or designs the task does not specify
- naming, scope, or product behavior the task leaves open
- anything destructive or hard to undo where the target is ambiguous
Set it false when the task is clear enough to just do, even if details remain - you are expected
to use good judgment on ordinary implementation choices, and most tasks need no question at all.
Do not ask about things that can be looked up in the code.

question: when needs_input is true, the single question to ask. Concrete, not "please clarify".
options: 2-4 concrete choices for that question, when choices exist."""

DECISION_SCHEMA: dict = {
    "type": "object",
    "properties": {
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
    "required": ["needs_input"],
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
    workspace: Path | None = None,
) -> PlanResult:
    # Grounding of last resort: with no search results AND a workspace to look
    # at, list the real files. The chat loop always calls this with results=[]
    # (context-blind), which is the same trap that made plan_mode hallucinate a
    # React component into a vanilla-JS workspace - a planner grounded in
    # nothing invents. Measured before/after on the chat_plan grounding eval;
    # the proven PLANNER_INSTRUCTIONS wording itself is untouched.
    grounding = ""
    if not results and workspace is not None:
        try:
            files = workspace_source_files(workspace)
        except Exception:
            files = []
        if files:
            listing = "\n".join(f"- {name}" for name in files)
            grounding = (
                "\n\nReal files in the workspace (reference ONLY these; a new file is fine "
                f"if the task needs one, but never invent existing ones):\n{listing}"
            )
    pack = context_builder.pack(
        results=results,
        request=f"{PLANNER_INSTRUCTIONS}{grounding}\n\nTask: {goal.strip()}",
        task_id=task_id,
        step_id=1,
        specialist="planner",
    )
    # The plan itself stays on the ORIGINAL free-text path. Folding it into the
    # decision's JSON call measurably degraded it: asked for a plan inside a
    # schema, the model wrote keystroke-level instructions ("write the exact
    # string 'hello world' using a text editor") and the coder obeyed, producing
    # a file of plain text instead of Python - create_file went 3/3 -> 0/3.
    # Rewording the schema prompt then broke edit_file_targeted instead. Planner
    # wording is load-bearing and tuning it by feel is how you trade one green
    # case for another, so it is left exactly as it was and measured as-is.
    response = await llm.run_specialist("planner", pack)
    decision = await _decide_needs_input(llm, pack, goal)
    return PlanResult(
        text=response.raw.strip(),
        pack=pack,
        needs_input=decision[0],
        question=decision[1],
        options=decision[2],
    )


async def _decide_needs_input(
    llm: ILLMManager, pack: ContextPack, goal: str
) -> tuple[bool, str, list[dict[str, str]]]:
    """Ask ONE narrow question: is a decision here the user's to make? (J6)

    A second call, deliberately: it keeps the plan text on its proven prompt,
    and this one is small and schema-constrained (one bool + one question), so
    it is far cheaper than the plan call it sits beside. Any failure - no schema
    support on this LLM, a raise, junk output - degrades to "don't ask", which
    is exactly the old behavior.
    """
    deterministic = deterministic_user_decision(goal)
    generate = getattr(llm, "generate_structured", None)
    if not callable(generate):
        return deterministic or (False, "", [])
    try:
        raw = await generate(
            "planner", DECISION_SYSTEM, _prompt_from_pack(pack, goal), DECISION_SCHEMA
        )
    except Exception:
        return deterministic or (False, "", [])
    data = _loads(raw)
    if not isinstance(data, dict):
        return deterministic or (False, "", [])
    question = str(data.get("question") or "").strip()
    if not (bool(data.get("needs_input")) and question) or _is_degenerate_question(question):
        return deterministic or (False, "", [])
    return True, question, _options_from(data.get("options"))


_ADD_AUTH_RE = re.compile(
    r"\b(add|build|create|implement|introduce|set\s*up)\b"
    r"(?:(?![.!?\n]).){0,80}\b(auth|authentication|login|sign[ -]?in)\b",
    re.IGNORECASE,
)
_AUTH_APPROACH_RE = re.compile(
    # Plurals must match. The option this very question offers is labelled
    # "Server sessions", and `\bsession\b` does not match "sessions", so
    # answering it left the request still unspecified and the question was
    # asked again on the next turn, indefinitely (observed live 2026-08-02).
    r"\b(jwt|json web tokens?|sessions?(?:-based)?|cookies?(?:-based)?|oauth2?|oidc|"
    r"openid connect|saml|basic auth|api keys?|passkeys?|magic links?|"
    r"firebase auth|auth0|clerk|supabase auth|nextauth|auth\.js)\b",
    re.IGNORECASE,
)
_PLAN_ONLY_RE = re.compile(
    r"\b(plan|planning|development plan|implementation plan|breakdown|roadmap)\b",
    re.IGNORECASE,
)


def deterministic_user_decision(
    goal: str,
) -> tuple[bool, str, list[dict[str, str]]] | None:
    """Catch a narrow expensive choice that small planners make inconsistently.

    This intentionally does not classify generic "auth" work. Fixes, audits,
    and tasks that name an approach still go through the normal planner. Only
    an explicit request to introduce authentication with no scheme selected is
    stopped before execution.
    """
    if _PLAN_ONLY_RE.search(goal):
        return None
    if not _ADD_AUTH_RE.search(goal) or _AUTH_APPROACH_RE.search(goal):
        return None
    return (
        True,
        "Which authentication approach should I implement?",
        [
            {
                "label": "Server sessions",
                "description": "Store login state on the server and use a secure session cookie.",
            },
            {
                "label": "JWT",
                "description": "Use signed tokens for stateless API authentication.",
            },
            {
                "label": "OAuth/OIDC",
                "description": "Delegate sign-in to an external identity provider.",
            },
        ],
    )


_DEGENERATE_QUESTION_RE = re.compile(
    r"\b(do i need to ask|should i ask|need to ask (?:a|the) question|"
    r"is there a question|any questions?\??$)\b",
    re.IGNORECASE,
)


def _is_degenerate_question(question: str) -> bool:
    """A small model asked to decide *whether* to ask sometimes echoes that
    meta-instruction back as the question itself - "Do I need to ask a
    question before proceeding?" - instead of a real, concrete question about
    the task (live repro: reproduced verbatim on two unrelated prompts).
    That gives the user nothing to answer, so treat it the same as no
    question at all - the same "junk output degrades to don't ask" rule this
    function already applies to a missing/unparseable response."""
    return bool(_DEGENERATE_QUESTION_RE.search(question))


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
