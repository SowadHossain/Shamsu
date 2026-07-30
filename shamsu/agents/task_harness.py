"""Structured task harness for routing decisions and specialist handoffs."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shamsu.action_ledger.context import get_current_run
from shamsu.retriever.documents import DocumentStore
from shamsu.skills.selector import (
    render_skill_context,
    select_skills_for_task,
    selection_log_payload,
)
from shamsu.skills.types import SkillSelection
from shamsu.types import RoutingDecision


@dataclass(frozen=True)
class HarnessStep:
    id: int
    specialist: str
    task: str


@dataclass(frozen=True)
class TaskPlan:
    mode: str
    goal: str
    executor_role: str
    steps: list[HarnessStep] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    skill_selection: SkillSelection | None = None
    document_context: str = ""
    document_hits: tuple[dict, ...] = field(default_factory=tuple)
    confidence: float = 1.0


def build_task_plan(
    decision: RoutingDecision,
    user_request: str,
    *,
    workspace: Path | None = None,
) -> TaskPlan:
    mode = _mode_for_intent(decision.intent)
    executor_role = _executor_for_intent(decision.intent)
    steps = _normalize_steps(decision.steps, decision.intent, user_request)
    required_tools = _dedupe(decision.needs_tools or _default_tools(decision.intent))
    target_files = _dedupe(decision.target_files)
    skill_selection = select_skills_for_task(
        workspace,
        user_request,
        intent=decision.intent,
        required_tools=required_tools,
        target_files=target_files,
    )
    _log_skill_selection(skill_selection)
    document_context = ""
    document_hits: tuple[dict, ...] = ()
    if workspace is not None:
        document_context, hits = DocumentStore(workspace).relevant_context(user_request)
        document_hits = tuple(hit.to_dict() for hit in hits)
    return TaskPlan(
        mode=mode,
        goal=user_request.strip(),
        executor_role=executor_role,
        steps=steps,
        required_tools=required_tools,
        target_files=target_files,
        verification=_verification_for_intent(decision.intent),
        skill_selection=skill_selection,
        document_context=document_context,
        document_hits=document_hits,
        confidence=decision.confidence,
    )


def render_task_handoff(plan: TaskPlan, agent_context: str = "") -> str:
    lines = [
        "## SHAMSU Task Harness",
        f"Mode: {plan.mode}",
        f"Goal: {plan.goal}",
        f"Executor role: {plan.executor_role}",
        f"Confidence: {plan.confidence:.2f}",
        "",
        "Required tools:",
        *_bullet_list(plan.required_tools or ["deterministic workspace context"]),
        "",
        "Target files:",
        *_bullet_list(plan.target_files or ["discover with search_index/read_file before editing"]),
        "",
        "Execution plan:",
        *[
            f"- Step {step.id} ({step.specialist}): {step.task}"
            for step in plan.steps
        ],
        "",
        "Verification:",
        *_bullet_list(plan.verification),
        "",
        "Rules:",
        "- Inspect relevant files before editing.",
        "- Apply changes only through SHAMSU's approval-backed file/patch paths.",
        "- Run commands only through CommandRunner.",
        "- Do not claim success unless the write/patch/command result confirms it.",
    ]
    if plan.document_context:
        lines.extend(
            [
                "- The Registered Document Evidence below has already been retrieved from the "
                "source. Use it directly; do not read the original document again unless the "
                "needed fact is absent from these excerpts.",
                "- When the requested target does not exist, create it with write_file. Do not "
                "probe a new target with edit_file or append_file first.",
            ]
        )
    skill_context = render_skill_context(plan.skill_selection) if plan.skill_selection else ""
    if skill_context:
        lines.extend(["", skill_context])
        _log_skill_context_injected(plan.skill_selection)
    if plan.document_context:
        lines.extend(["", plan.document_context])
        _log_document_context_injected(plan.document_hits)
    if agent_context.strip():
        lines.extend(["", "## Deterministic Context", agent_context.strip()])
    return "\n".join(lines).strip()


def append_task_handoff(user_request: str, plan: TaskPlan, agent_context: str = "") -> str:
    return f"{user_request.strip()}\n\n{render_task_handoff(plan, agent_context)}"


def plan_log_payload(plan: TaskPlan) -> dict:
    return {
        "mode": plan.mode,
        "goal": plan.goal,
        "executor_role": plan.executor_role,
        "steps": [
            {"id": step.id, "specialist": step.specialist, "task": step.task}
            for step in plan.steps
        ],
        "required_tools": plan.required_tools,
        "target_files": plan.target_files,
        "verification": plan.verification,
        "skills": (
            selection_log_payload(plan.skill_selection)
            if plan.skill_selection is not None
            else {"mode": "off", "selected": []}
        ),
        "documents": {
            "injected": list(plan.document_hits),
            "count": len(plan.document_hits),
        },
        "confidence": plan.confidence,
    }


def _log_skill_selection(selection: SkillSelection) -> None:
    ledger = get_current_run()
    if ledger is None:
        return
    payload = selection_log_payload(selection)
    selected_names = [
        str(item.get("name"))
        for item in payload.get("selected", [])
        if isinstance(item, dict)
    ]
    ledger.log_event("skills_discovered", issues=payload.get("issues", []))
    ledger.log_decision(
        "select_skills",
        reason_summary="Deterministically selected task skills from request and workspace evidence.",
        evidence=[
            f"mode:{payload.get('mode')}",
            "selected:" + ",".join(selected_names),
        ],
        chosen_action=",".join(selected_names),
        outcome="selected" if selected_names else "none",
    )


def _log_skill_context_injected(selection: SkillSelection) -> None:
    ledger = get_current_run()
    if ledger is None or not selection.active:
        return
    selected = [item.skill for item in selection.selected]
    ledger.log_event(
        "skill_context_injected",
        skills=[skill.name for skill in selected],
        sources=[skill.source for skill in selected],
        references=[
            {
                "name": skill.name,
                "source": skill.metadata.get("reference_source", ""),
                "path": str(skill.skill_path),
            }
            for skill in selected
            if str(skill.metadata.get("kind") or "").lower() == "reference"
        ],
    )


def _log_document_context_injected(hits: tuple[dict, ...]) -> None:
    ledger = get_current_run()
    if ledger is None or not hits:
        return
    ledger.log_event(
        "document_context_injected",
        chunks=[
            {
                "chunk_id": hit.get("chunk_id", ""),
                "document_name": hit.get("document_name", ""),
                "citation": hit.get("citation", ""),
                "score": hit.get("score", 0.0),
                "match_kind": hit.get("match_kind", ""),
            }
            for hit in hits
        ],
    )


def _normalize_steps(raw_steps: list[dict], intent: str, user_request: str) -> list[HarnessStep]:
    steps: list[HarnessStep] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            continue
        task = str(raw.get("task") or "").strip()
        if not task:
            continue
        steps.append(
            HarnessStep(
                id=int(raw.get("id") or index),
                specialist=str(raw.get("specialist") or _executor_for_intent(intent)),
                task=task,
            )
        )
    if steps:
        return steps
    return [
        HarnessStep(1, "planner", "Identify the relevant files, commands, and success criteria."),
        HarnessStep(2, _executor_for_intent(intent), user_request.strip()),
        HarnessStep(3, "reviewer", "Verify the result and report any remaining risk."),
    ]


def _mode_for_intent(intent: str) -> str:
    return {
        "code_edit": "code_edit",
        "bug_fix": "bugfix",
        "audit": "review",
        "test_gen": "test_generation",
        "doc_gen": "documentation",
        "generate": "planning",
        "explain": "thinking",
        "qa": "thinking",
    }.get(intent, "thinking")


def _executor_for_intent(intent: str) -> str:
    return {
        "code_edit": "coder",
        "bug_fix": "bugfix",
        "audit": "reviewer",
        "test_gen": "test_gen",
        "doc_gen": "doc_agent",
        "generate": "planner",
        "explain": "qa",
        "qa": "qa",
    }.get(intent, "qa")


def _default_tools(intent: str) -> list[str]:
    if intent in {"code_edit", "bug_fix", "test_gen", "doc_gen"}:
        return [
            "search_index",
            "read_file",
            "edit_file",
            "append_file",
            "write_file",
            "run_command",
        ]
    if intent == "audit":
        return ["search_index", "read_file"]
    return ["search_index", "read_file"]


def _verification_for_intent(intent: str) -> list[str]:
    return {
        "code_edit": ["review changed files", "run relevant tests when safe"],
        "bug_fix": ["rerun the failing command or test when safe", "confirm the reported error is addressed"],
        "test_gen": ["run generated tests when safe"],
        "doc_gen": ["check documentation matches indexed project facts"],
        "audit": ["report findings with file and line references"],
        "generate": ["validate generated files and run setup/tests when safe"],
    }.get(intent, ["answer only from confirmed context"])


def _bullet_list(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
