"""Sequential 'council' pattern: draft -> critique -> reconcile.

This is *just more calls* to the existing ILLMManager.run_specialist() —
one model drafts, a second (the "reviewer" role, already a distinct model
in runtime/models.py) critiques, and the drafting specialist reconciles
only if the critique actually flags issues. Models are still swapped one
at a time via Ollama's own keep_alive handling, matching the project's
sub-8GB/one-model-at-a-time hardware constraint — this is not concurrent
multi-model loading.

Council mode is deliberately reserved for high-stakes actions (destructive
operations, security-relevant paths, or low routing confidence) rather than
every request, so the common case stays fast and cheap.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from shamsu.interfaces import ILLMManager
from shamsu.session.manager import SessionLogger
from shamsu.types import ContextPack, LLMResponse, RoutingDecision

CONFIDENCE_THRESHOLD = 0.5

DESTRUCTIVE_ACTION_KINDS = {"file_delete", "run_command"}

SECURITY_SENSITIVE_PATTERNS = [
    r"shamsu/safety/",
    r"settings\.py$",
    r"\.env",
    r"secret",
    r"credential",
    r"password",
]

CLEAN_CRITIQUE_SIGNALS = ("no issues", "looks good", "lgtm", "no problems", "approved")
ISSUE_CRITIQUE_SIGNALS = (
    "issue", "bug", "problem", "vulnerability", "incorrect", "missing", "risk", "should",
)


@dataclass(frozen=True)
class CouncilResult:
    final: LLMResponse
    draft: LLMResponse
    critique: LLMResponse | None = None
    reconciled: bool = False


def should_convene_council(
    routing: RoutingDecision | None = None,
    action_kind: str = "",
    target_paths: list[str] | None = None,
) -> bool:
    """Heuristic gate: convene the (sequential) council only when the stakes
    justify the extra model calls — low routing confidence, a destructive
    action kind, or a security-sensitive target path.
    """
    if routing is not None and routing.confidence < CONFIDENCE_THRESHOLD:
        return True
    if action_kind in DESTRUCTIVE_ACTION_KINDS:
        return True
    for path in target_paths or []:
        lowered = path.lower().replace("\\", "/")
        if any(re.search(pattern, lowered) for pattern in SECURITY_SENSITIVE_PATTERNS):
            return True
    return False


async def run_council(
    llm: ILLMManager,
    pack: ContextPack,
    specialist: str,
    critic: str = "reviewer",
    session_logger: SessionLogger | None = None,
) -> CouncilResult:
    """Draft with `specialist`, critique with `critic`, reconcile with
    `specialist` again only if the critique actually flags a problem.
    """
    draft = await llm.run_specialist(specialist, pack)
    _log(
        session_logger, "council.draft", pack.task_id,
        {"specialist": specialist, "model": draft.model_used},
        f"Council draft from {specialist}",
    )

    critique_pack = _build_critique_pack(pack, draft)
    critique = await llm.run_specialist(critic, critique_pack)
    _log(
        session_logger, "council.critique", pack.task_id,
        {"critic": critic, "model": critique.model_used},
        f"Council critique from {critic}",
    )

    if not _critique_flags_issues(critique.raw):
        return CouncilResult(final=draft, draft=draft, critique=critique, reconciled=False)

    reconcile_pack = _build_reconcile_pack(pack, draft, critique)
    reconciled = await llm.run_specialist(specialist, reconcile_pack)
    _log(
        session_logger, "council.reconciled", pack.task_id,
        {"specialist": specialist, "model": reconciled.model_used},
        f"Council reconciled by {specialist} after critique",
    )
    return CouncilResult(final=reconciled, draft=draft, critique=critique, reconciled=True)


def _critique_flags_issues(critique_text: str) -> bool:
    lowered = critique_text.strip().lower()
    if not lowered:
        return False
    if any(signal in lowered for signal in CLEAN_CRITIQUE_SIGNALS):
        return False
    return any(signal in lowered for signal in ISSUE_CRITIQUE_SIGNALS)


def _build_critique_pack(pack: ContextPack, draft: LLMResponse) -> ContextPack:
    return ContextPack(
        task_id=pack.task_id,
        step_id=pack.step_id,
        specialist="reviewer",
        user_request=(
            f"Review the following proposed output for correctness, safety, and completeness.\n\n"
            f"Original task:\n{pack.user_request}\n\n"
            f"Proposed output:\n{draft.raw}\n\n"
            f"If it looks correct and safe, reply with exactly: No issues found.\n"
            f"Otherwise, list the specific problems."
        ),
        snippets=pack.snippets,
        prd_context=pack.prd_context,
        error_context=pack.error_context,
    )


def _build_reconcile_pack(pack: ContextPack, draft: LLMResponse, critique: LLMResponse) -> ContextPack:
    return ContextPack(
        task_id=pack.task_id,
        step_id=pack.step_id,
        specialist=pack.specialist,
        user_request=(
            f"{pack.user_request}\n\n"
            f"Your previous attempt:\n{draft.raw}\n\n"
            f"A reviewer found these issues:\n{critique.raw}\n\n"
            f"Produce a corrected final answer addressing the reviewer's concerns."
        ),
        snippets=pack.snippets,
        prd_context=pack.prd_context,
        error_context=pack.error_context,
    )


def _log(
    session_logger: SessionLogger | None,
    event_type: str,
    task_id: str,
    payload: dict,
    summary: str,
) -> None:
    if session_logger:
        session_logger.log(event_type, payload, summary, workflow_id=task_id)
