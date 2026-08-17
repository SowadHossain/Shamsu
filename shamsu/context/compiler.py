"""State-frame context compiler for small local executor models."""
from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shamsu.artifacts.code import artifact_brief
from shamsu.context.budget import RESERVE_OUTPUT_TOKENS, SAFETY_MARGIN_TOKENS, count_tokens
from shamsu.context.project_snapshot import (
    ProjectSnapshot,
    load_project_snapshot,
    render_project_identity,
    render_project_invariants,
    render_tech_stack,
)
from shamsu.runtime.phase_contracts import ExecutionPhase, normalize_phase
from shamsu.runtime.task_state import EvidenceRecord, ExecutionPlan, PlanStep, RuntimeStateStore, TaskState


@dataclass(frozen=True)
class ContextFrame:
    phase: str
    task: TaskState | None = None
    plan: ExecutionPlan | None = None
    current_step: PlanStep | None = None
    evidence: tuple[EvidenceRecord, ...] = ()
    relevant_files: tuple[str, ...] = ()
    latest_observation: dict[str, Any] = field(default_factory=dict)
    allowed_tools: tuple[str, ...] = ()
    artifact_context: str = ""
    project_snapshot: ProjectSnapshot | None = None

    def render(self, source_sections: list[str]) -> str:
        source = "\n\n".join(source_sections) or "No source snippets selected."
        sections = {
            "PHASE": self.phase,
            "PROJECT IDENTITY": _render_project_identity(self.project_snapshot),
            "TECH STACK": _render_tech_stack(self.project_snapshot),
            "PROJECT INVARIANTS": _render_project_invariants(self.project_snapshot),
            "CURRENT TASK": _render_task(self.task),
            "CURRENT STEP": _render_step(self.current_step),
            "ACCEPTANCE CRITERIA": _render_acceptance(self.current_step, self.task),
            "PROJECT FACTS": _render_project_facts(self.task, self.plan, self.artifact_context),
            "RELEVANT ARTIFACTS": _render_artifacts(self.task, self.evidence),
            "RELEVANT SOURCE CODE": source,
            "LATEST OBSERVATION": _render_observation(self.latest_observation),
            "COMPLETED STEP SUMMARY": _render_completed_steps(self.plan),
            "ALLOWED TOOLS": _render_allowed_tools(self.allowed_tools),
            "OUTPUT CONTRACT": _output_contract(self.phase),
        }
        return "\n\n".join(
            f"[{title}]\n{sections[title].strip()}" for title in _section_order_for_phase(self.phase)
        )


class ContextCompiler:
    def __init__(
        self,
        compile_messages: Callable[[int, list[str]], Awaitable[list[dict[str, Any]]]] | None = None,
        *,
        store: RuntimeStateStore | None = None,
        task_id_getter: Callable[[], str] | None = None,
        workspace_root: Path | None = None,
        system_prompt_getter: Callable[[], str] | None = None,
        allowed_tools_getter: Callable[[], list[dict[str, Any]]] | None = None,
        recent_messages_getter: Callable[[], list[dict[str, Any]]] | None = None,
        trace: Callable[[str, str, dict[str, Any] | None], None] | None = None,
    ) -> None:
        self._compile_messages = compile_messages
        self.store = store
        self.task_id_getter = task_id_getter
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        self.system_prompt_getter = system_prompt_getter
        self.allowed_tools_getter = allowed_tools_getter
        self.recent_messages_getter = recent_messages_getter
        self.trace = trace

    async def compile(
        self,
        token_budget: int,
        written_files: list[str],
    ) -> list[dict[str, Any]]:
        if self.store is None or self.task_id_getter is None or self.system_prompt_getter is None:
            if self._compile_messages is None:
                return []
            return await self._compile_messages(token_budget, written_files)
        task_id = self.task_id_getter()
        task = self.store.load_task(task_id) if task_id else None
        if task is None:
            if self._compile_messages is None:
                return [{"role": "system", "content": self.system_prompt_getter()}]
            return await self._compile_messages(token_budget, written_files)

        plan = self.store.load_task_plan(task.task_id)
        current_step = self.store.current_active_step(task.task_id)
        evidence = tuple(self.store.list_evidence(task.task_id)[-12:])
        allowed_tools = tuple(_tool_names(self.allowed_tools_getter() if self.allowed_tools_getter else []))
        phase = normalize_phase(task.current_phase).value
        relevant_files = _relevant_files(task, current_step, written_files)
        source_sections = self._source_sections(relevant_files, token_budget)
        artifact_context = ""
        project_snapshot = None
        if self.workspace_root is not None:
            project_snapshot = load_project_snapshot(self.workspace_root)
            if _phase_uses_artifact_context(phase):
                artifact_context = artifact_brief(
                    self.workspace_root,
                    query=f"{task.user_request} {current_step.goal if current_step else ''}",
                    files=relevant_files[:4],
                )
        latest_observation = _latest_observation_with_hot_guidance(
            task.last_tool_result,
            self.recent_messages_getter() if self.recent_messages_getter else [],
        )
        frame = ContextFrame(
            phase=phase,
            task=task,
            plan=plan,
            current_step=current_step,
            evidence=evidence,
            relevant_files=tuple(relevant_files),
            latest_observation=latest_observation,
            allowed_tools=allowed_tools,
            artifact_context=artifact_context,
            project_snapshot=project_snapshot,
        )
        messages = [
            {"role": "system", "content": self.system_prompt_getter()},
            {"role": "user", "content": frame.render(source_sections)},
        ]
        messages, trimmed = self._trim(messages, token_budget)
        if self.trace:
            self.trace(
                "context.compiled",
                "Compiled fresh runtime state frame for model decision.",
                {
                    "task_id": task.task_id,
                    "phase": frame.phase,
                    "files": list(relevant_files),
                    "evidence": len(evidence),
                    "allowed_tools": len(allowed_tools),
                    "project_snapshot": bool(project_snapshot),
                    "trimmed": trimmed,
                },
            )
        return messages

    def _source_sections(self, files: list[str], token_budget: int) -> list[str]:
        if self.workspace_root is None:
            return []
        usable = max(1200, token_budget - RESERVE_OUTPUT_TOKENS - SAFETY_MARGIN_TOKENS)
        per_file_chars = max(1200, min(5000, (usable * 4) // max(1, min(len(files), 4))))
        sections: list[str] = []
        for relative in files[:6]:
            try:
                target = (self.workspace_root / relative).resolve()
                target.relative_to(self.workspace_root)
                if not target.is_file():
                    continue
                body = target.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
            if len(body) > per_file_chars:
                body = body[:per_file_chars].rstrip() + "\n...[truncated]"
            sections.append(f"--- {relative} ---\n{body.rstrip()}")
        return sections

    def _trim(
        self,
        messages: list[dict[str, Any]],
        token_budget: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        target = max(1024, token_budget - RESERVE_OUTPUT_TOKENS - SAFETY_MARGIN_TOKENS)
        text = "\n".join(str(message.get("content", "")) for message in messages)
        if count_tokens(text) <= target:
            return messages, False
        trimmed = [dict(message) for message in messages]
        frame = str(trimmed[-1].get("content", ""))
        char_budget = max(2000, target * 4)
        if len(frame) > char_budget:
            trimmed[-1]["content"] = _trim_state_frame(frame, char_budget)
        return trimmed, True


def _tool_names(schemas: list[dict[str, Any]]) -> list[str]:
    return [
        str((schema.get("function") or {}).get("name") or "")
        for schema in schemas
        if str((schema.get("function") or {}).get("name") or "")
    ]


def _section_order_for_phase(phase: str) -> tuple[str, ...]:
    normalized = normalize_phase(phase)
    common_front = (
        "PHASE",
        "PROJECT IDENTITY",
        "TECH STACK",
        "PROJECT INVARIANTS",
        "CURRENT TASK",
        "CURRENT STEP",
        "ACCEPTANCE CRITERIA",
    )
    common_back = (
        "ALLOWED TOOLS",
        "OUTPUT CONTRACT",
    )
    if normalized == ExecutionPhase.REPAIR:
        return (
            *common_front,
            "RELEVANT ARTIFACTS",
            "LATEST OBSERVATION",
            "RELEVANT SOURCE CODE",
            *common_back,
        )
    if normalized == ExecutionPhase.VERIFY:
        return (
            "PHASE",
            "PROJECT INVARIANTS",
            "CURRENT TASK",
            "ACCEPTANCE CRITERIA",
            "RELEVANT ARTIFACTS",
            "LATEST OBSERVATION",
            "RELEVANT SOURCE CODE",
            *common_back,
        )
    if normalized == ExecutionPhase.AUTHOR:
        return (
            *common_front,
            "RELEVANT SOURCE CODE",
            "LATEST OBSERVATION",
            "COMPLETED STEP SUMMARY",
            *common_back,
        )
    return (
        *common_front,
        "PROJECT FACTS",
        "RELEVANT ARTIFACTS",
        "RELEVANT SOURCE CODE",
        "LATEST OBSERVATION",
        "COMPLETED STEP SUMMARY",
        *common_back,
    )


def _phase_uses_artifact_context(phase: str) -> bool:
    normalized = normalize_phase(phase)
    return normalized not in {ExecutionPhase.AUTHOR, ExecutionPhase.REPAIR, ExecutionPhase.VERIFY}


def _render_task(task: TaskState | None) -> str:
    if task is None:
        return "No runtime task state loaded."
    return "\n".join(
        [
            f"task_id: {task.task_id}",
            f"run_id: {task.run_id}",
            f"status: {task.status.value}",
            f"user_request: {task.user_request}",
            f"action_count: {task.action_count}",
            f"repair_count: {task.repair_count}",
            f"changed_files: {', '.join(task.changed_files) or 'none'}",
            f"last_checkpoint: {task.last_checkpoint or 'none'}",
        ]
    )


def _render_project_identity(snapshot: ProjectSnapshot | None) -> str:
    if snapshot is None:
        return "No project snapshot available."
    return render_project_identity(snapshot)


def _render_tech_stack(snapshot: ProjectSnapshot | None) -> str:
    if snapshot is None:
        return "No deterministic tech stack detected."
    return render_tech_stack(snapshot)


def _render_project_invariants(snapshot: ProjectSnapshot | None) -> str:
    if snapshot is None:
        return "No deterministic project invariants registered."
    return render_project_invariants(snapshot)


def _render_step(step: PlanStep | None) -> str:
    if step is None:
        return "No active plan step."
    return "\n".join(
        [
            f"step_id: {step.step_id}",
            f"title: {step.title}",
            f"goal: {step.goal}",
            f"status: {step.status.value}",
            f"risk_level: {getattr(step.risk_level, 'value', step.risk_level)}",
            f"approval_required: {step.approval_required}",
            f"dependencies: {', '.join(step.dependencies) or 'none'}",
            f"expected_outputs: {'; '.join(step.expected_outputs) or 'none'}",
            f"constraints: {'; '.join(step.constraints) or 'none'}",
        ]
    )


def _render_acceptance(step: PlanStep | None, task: TaskState | None) -> str:
    criteria = list(step.acceptance_criteria if step is not None else [])
    required = list(step.required_evidence if step is not None else [])
    if task is not None:
        required.extend(item for item in task.required_evidence if item not in required)
    lines = [f"- {item}" for item in criteria] or ["- No explicit acceptance criteria registered."]
    lines.append("required_evidence: " + (", ".join(required) if required else "none"))
    return "\n".join(lines)


def _render_project_facts(
    task: TaskState | None,
    plan: ExecutionPlan | None,
    artifact_context: str = "",
) -> str:
    lines: list[str] = []
    if task is not None:
        lines.append(f"project_id: {task.project_id}")
        if task.plan_id:
            lines.append(f"plan_id: {task.plan_id}")
    if plan is not None:
        lines.append(f"plan_summary: {plan.compact_summary()}")
    if artifact_context:
        lines.append("code_artifacts:\n" + artifact_context)
    return "\n".join(lines) if lines else "No project facts registered."


def _render_artifacts(task: TaskState | None, evidence: tuple[EvidenceRecord, ...]) -> str:
    lines: list[str] = []
    if task is not None:
        if task.changed_files:
            lines.append("changed_files: " + ", ".join(task.changed_files[-12:]))
        if task.collected_evidence:
            lines.append("collected_evidence: " + ", ".join(task.collected_evidence[-12:]))
    if evidence:
        lines.append("recent_evidence:")
        for record in evidence[-8:]:
            related = ", ".join(record.related_files) or record.related_command or "none"
            lines.append(
                f"- {record.evidence_type.value} {record.status.value} via {record.source_tool}: {related}"
            )
    return "\n".join(lines) if lines else "No registered artifacts yet."


def _render_observation(observation: dict[str, Any]) -> str:
    if not observation:
        return "No tool observation recorded yet."
    return _json_budget(observation, 3500)


def _latest_observation_with_hot_guidance(
    latest_observation: dict[str, Any],
    recent_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = dict(latest_observation or {})
    guidance: list[str] = []
    for message in recent_messages[-8:]:
        role = str(message.get("role") or "")
        content = str(message.get("content") or "").strip()
        if role != "user" or not content:
            continue
        if _is_state_frame(content):
            continue
        guidance.append(content[-1800:])
    if guidance:
        payload["recent_hot_guidance"] = guidance[-4:]
    return payload


def _is_state_frame(content: str) -> bool:
    return content.lstrip().startswith("[PHASE]\n")


def _render_completed_steps(plan: ExecutionPlan | None) -> str:
    if plan is None:
        return "No execution plan registered."
    return plan.completed_summary()


def _render_allowed_tools(names: tuple[str, ...]) -> str:
    if not names:
        return "No tools are available in this phase."
    return "\n".join(f"- {name}" for name in names)


def _output_contract(phase: str) -> str:
    lines = [
        "Use only the allowed tools.",
        "Choose exactly one action for this response.",
        "Do not rely on old chat turns as state.",
        "When more work is needed, call exactly one appropriate tool.",
        "Do not describe future actions without making the tool call now.",
        "Only claim completion when acceptance criteria and required evidence are satisfied.",
    ]
    if str(phase).upper() not in {"COMPLETE", "EXPLORE"}:
        lines.append("Do not claim this step is complete in the current phase.")
    return "\n".join(f"- {line}" for line in lines)


def _relevant_files(
    task: TaskState,
    step: PlanStep | None,
    written_files: list[str],
) -> list[str]:
    candidates: list[str] = []
    candidates.extend(written_files)
    candidates.extend(task.changed_files)
    candidates.extend(_paths_from_payload(task.last_tool_call))
    candidates.extend(_paths_from_payload(task.last_tool_result))
    if step is not None:
        for value in [*step.inputs, *step.expected_outputs, step.goal]:
            candidates.extend(_path_tokens(value))
    candidates.extend(_path_tokens(task.user_request))
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        normalized = candidate.replace("\\", "/").strip().strip("'\"")
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _paths_from_payload(payload: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    if not isinstance(payload, dict):
        return paths
    for key in ("filepath", "resolved_filepath", "path", "source", "destination"):
        value = payload.get(key)
        if isinstance(value, str):
            paths.append(value)
    for key in ("related_files", "touched_files", "changed_files", "deleted_files"):
        value = payload.get(key)
        if isinstance(value, list):
            paths.extend(str(item) for item in value if item)
    for value in payload.values():
        if isinstance(value, dict):
            paths.extend(_paths_from_payload(value))
    return paths


_PATH_RE = re.compile(
    r"(?<![\w.-])(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.(?:py|js|ts|tsx|jsx|json|md|txt|html|css|yml|yaml|toml|ini|cfg|env|sql|sh|ps1)"
)


def _path_tokens(text: str) -> list[str]:
    return [match.group(0) for match in _PATH_RE.finditer(str(text or ""))]


def _json_budget(value: Any, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _trim_state_frame(frame: str, char_budget: int) -> str:
    sections = _split_state_frame(frame)
    if not sections:
        marker = "\n...[context frame truncated to fit model window]...\n"
        keep = max(1000, char_budget - len(marker))
        head = int(keep * 0.7)
        tail = keep - head
        return frame[:head].rstrip() + marker + frame[-tail:].lstrip()

    caps = {
        "PROJECT FACTS": 1400,
        "RELEVANT ARTIFACTS": 900,
        "RELEVANT SOURCE CODE": 1800,
        "LATEST OBSERVATION": 1200,
        "COMPLETED STEP SUMMARY": 800,
    }
    trimmed = [
        (title, _clip_section(body, caps.get(title, len(body))))
        for title, body in sections
    ]
    rendered = _render_sections(trimmed)
    if len(rendered) <= char_budget:
        return rendered

    low_priority = [
        "PROJECT FACTS",
        "COMPLETED STEP SUMMARY",
        "RELEVANT ARTIFACTS",
        "LATEST OBSERVATION",
        "RELEVANT SOURCE CODE",
    ]
    for title_to_trim in low_priority:
        overflow = len(rendered) - char_budget
        next_sections: list[tuple[str, str]] = []
        for title, body in trimmed:
            if title != title_to_trim:
                next_sections.append((title, body))
                continue
            minimum = 500 if title == "RELEVANT SOURCE CODE" else 180
            next_sections.append((title, _clip_section(body, max(minimum, len(body) - overflow - 120))))
        trimmed = next_sections
        rendered = _render_sections(trimmed)
        if len(rendered) <= char_budget:
            return rendered

    marker = "\n...[context frame truncated to fit model window]...\n"
    keep = max(1000, char_budget - len(marker))
    head = int(keep * 0.75)
    tail = keep - head
    return rendered[:head].rstrip() + marker + rendered[-tail:].lstrip()


def _split_state_frame(frame: str) -> list[tuple[str, str]]:
    matches = list(re.finditer(r"(?m)^\[([A-Z][A-Z ]+)\]\n", frame))
    sections: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        title = match.group(1)
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(frame)
        sections.append((title, frame[body_start:body_end].strip()))
    return sections


def _render_sections(sections: list[tuple[str, str]]) -> str:
    return "\n\n".join(f"[{title}]\n{body.strip()}" for title, body in sections)


def _clip_section(body: str, max_chars: int) -> str:
    if len(body) <= max_chars:
        return body
    if max_chars <= 40:
        return body[:max_chars].rstrip()
    return body[: max_chars - 16].rstrip() + "\n...[truncated]"
