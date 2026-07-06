"""Structured task harness for routing decisions and specialist handoffs."""
from __future__ import annotations

from dataclasses import dataclass, field

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
    confidence: float = 1.0


def build_task_plan(decision: RoutingDecision, user_request: str) -> TaskPlan:
    mode = _mode_for_intent(decision.intent)
    executor_role = _executor_for_intent(decision.intent)
    steps = _normalize_steps(decision.steps, decision.intent, user_request)
    required_tools = _dedupe(decision.needs_tools or _default_tools(decision.intent))
    target_files = _dedupe(decision.target_files)
    return TaskPlan(
        mode=mode,
        goal=user_request.strip(),
        executor_role=executor_role,
        steps=steps,
        required_tools=required_tools,
        target_files=target_files,
        verification=_verification_for_intent(decision.intent),
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
        "confidence": plan.confidence,
    }


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
        return ["search_index", "read_file", "write_file", "run_command"]
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
