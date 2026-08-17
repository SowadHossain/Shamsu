"""Phase-specific prompt fragments for the production agent loop."""
from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from shamsu.runtime.phase_contracts import ExecutionPhase, normalize_phase


class PromptProfile(str, Enum):
    SMALL = "SMALL"
    STANDARD = "STANDARD"


@dataclass(frozen=True)
class PromptStepFragment:
    step_id: str = ""
    title: str = ""
    goal: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    required_evidence: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()


_SMALL_MODEL_HINTS = ("3b", "4b", "gemma", "deepseek", "phi3", "codellama")
_VALID_PROFILES = {profile.value for profile in PromptProfile}


def prompt_profile_for_model(model_name: str, explicit: str = "") -> PromptProfile:
    requested = (explicit or os.environ.get("SHAMSU_PROMPT_PROFILE", "")).strip().upper()
    if requested in _VALID_PROFILES:
        return PromptProfile(requested)
    lowered = (model_name or "").lower()
    if not lowered or any(hint in lowered for hint in _SMALL_MODEL_HINTS):
        return PromptProfile.SMALL
    return PromptProfile.SMALL


def compose_agent_prompt(
    workspace: Path,
    *,
    profile: PromptProfile | str = PromptProfile.SMALL,
    phase: ExecutionPhase | str | None = None,
    current_step: Any = None,
    available_tools: Iterable[str] = (),
    include_tool_protocol: bool = False,
    include_raw_write_protocol: bool = True,
) -> str:
    resolved_profile = profile if isinstance(profile, PromptProfile) else PromptProfile(str(profile).upper())
    resolved_phase = normalize_phase(phase)
    step = step_fragment_from_runtime(current_step, fallback_tools=tuple(available_tools))
    sections = [
        ("BASE_RULES", _base_rules(workspace, resolved_profile)),
        ("PHASE_RULES", _phase_rules(resolved_phase, resolved_profile)),
        ("CURRENT_STEP", _current_step(step, resolved_phase)),
        (
            "TOOL_PROTOCOL",
            _tool_protocol(
                step.allowed_tools,
                include_tool_protocol=include_tool_protocol,
                include_raw_write_protocol=include_raw_write_protocol,
            ),
        ),
        ("OUTPUT_SCHEMA", _output_schema(resolved_phase, resolved_profile, include_tool_protocol)),
        ("FAILURE_RECOVERY_RULES", _failure_recovery_rules(resolved_profile)),
    ]
    return "\n\n".join(f"[{title}]\n{body.strip()}" for title, body in sections) + "\n"


def step_fragment_from_runtime(current_step: Any, *, fallback_tools: Iterable[str] = ()) -> PromptStepFragment:
    if current_step is None:
        return PromptStepFragment(allowed_tools=tuple(_clean_list(fallback_tools)))
    allowed_tools = _clean_list(getattr(current_step, "allowed_tools", ()) or fallback_tools)
    return PromptStepFragment(
        step_id=str(getattr(current_step, "step_id", "") or ""),
        title=str(getattr(current_step, "title", "") or ""),
        goal=str(getattr(current_step, "goal", "") or ""),
        acceptance_criteria=tuple(_clean_list(getattr(current_step, "acceptance_criteria", ()))),
        required_evidence=tuple(_clean_list(getattr(current_step, "required_evidence", ()))),
        constraints=tuple(_clean_list(getattr(current_step, "constraints", ()))),
        allowed_tools=tuple(allowed_tools),
    )


def _base_rules(workspace: Path, profile: PromptProfile) -> str:
    lines = [
        "You are SHAMSU.",
        "Role: SHAMSU, a local-first coding agent in one workspace.",
        f"Workspace: {workspace}",
        "Use runtime state, source reads, and tool results as truth.",
        "Never claim a file changed, command ran, search happened, or test passed without tool evidence.",
        "Keep all paths workspace-relative and inside the workspace.",
        "Use exactly the tools made available for this call.",
        "Do not expose hidden reasoning.",
    ]
    if profile == PromptProfile.STANDARD:
        lines.append("Briefly summarize observed facts when answering without a tool.")
    return "\n".join(f"- {line}" for line in lines)


def _phase_rules(phase: ExecutionPhase, profile: PromptProfile) -> str:
    common = {
        ExecutionPhase.EXPLORE: [
            "Read, search, and inspect only.",
            "No project mutations.",
            "Gather facts before proposing edits.",
        ],
        ExecutionPhase.PLAN: [
            "Create or refine a plan contract only.",
            "No project mutations.",
            "Every mutating step needs acceptance criteria and evidence.",
        ],
        ExecutionPhase.AUTHOR: [
            "Implement the active step only.",
            "Modify only files related to the current step.",
            "Do not claim the step is complete.",
        ],
        ExecutionPhase.VERIFY: [
            "Run tests, lint, type checks, builds, diffs, or health checks.",
            "Source modification is blocked.",
            "Report only verification evidence.",
        ],
        ExecutionPhase.REPAIR: [
            "Use failure evidence to repair the related files only.",
            "Do not broaden scope.",
            "Run targeted verification after the repair.",
        ],
        ExecutionPhase.DEPLOY: [
            "Use deployment or local service checks only.",
            "Prefer smoke tests and health checks.",
            "Do not change source unless phase changes to REPAIR.",
        ],
        ExecutionPhase.COMPLETE: [
            "No mutation.",
            "Produce the final report from registered evidence.",
            "Do not introduce new claims.",
        ],
    }
    lines = list(common.get(phase, common[ExecutionPhase.EXPLORE]))
    if profile == PromptProfile.SMALL:
        lines.append("Choose exactly one action for this response.")
    return "\n".join(f"- {line}" for line in lines)


def _current_step(step: PromptStepFragment, phase: ExecutionPhase) -> str:
    lines = [f"Current phase: {phase.value}"]
    if step.step_id:
        lines.append(f"step_id: {step.step_id}")
    lines.append(f"title: {step.title or 'No active plan step.'}")
    lines.append(f"goal: {step.goal or 'Follow the current task frame.'}")
    lines.append("available_tools:")
    lines.extend(f"- {name}" for name in step.allowed_tools) if step.allowed_tools else lines.append("- none")
    lines.append("acceptance_criteria:")
    lines.extend(f"- {item}" for item in step.acceptance_criteria) if step.acceptance_criteria else lines.append("- none registered")
    lines.append("required_evidence:")
    lines.extend(f"- {item}" for item in step.required_evidence) if step.required_evidence else lines.append("- none registered")
    if step.constraints:
        lines.append("constraints:")
        lines.extend(f"- {item}" for item in step.constraints)
    return "\n".join(lines)


def _tool_protocol(
    available_tools: tuple[str, ...],
    *,
    include_tool_protocol: bool,
    include_raw_write_protocol: bool,
) -> str:
    available_tool_set = set(available_tools)
    raw_write_tools = tuple(
        name for name in ("write_file", "append_file", "edit_file") if name in available_tool_set
    )
    lines = [
        "Call only one available tool, or answer only when no tool is needed.",
        "Do not describe a future tool call; make the call now.",
        "Do not use an unavailable tool.",
    ]
    if available_tools:
        lines.append("Available tool names: " + ", ".join(available_tools))
    if include_tool_protocol:
        lines.extend(
            [
                "This model does not use native tool-calls.",
                "To use a tool, emit exactly one JSON object and no prose:",
                '{"name": "<tool>", "arguments": {}}',
                "file content is never JSON; use the raw file-write form below for multiline content.",
            ]
        )
    else:
        lines.append("Use the native tool-call interface when a tool is needed; do not emit tool JSON as prose.")
    if include_raw_write_protocol and raw_write_tools:
        example_tool = raw_write_tools[0]
        raw_write_headers = ", ".join(f"# {name}: path" for name in raw_write_tools)
        lines.extend(
            [
                "Send file content as a RAW block, never as escaped JSON.",
                "Raw fallback header example:",
                "```html",
                f"# {example_tool}: templates/my_orders.html",
                "<h1>Orders</h1>",
                "```",
                f"Use {raw_write_headers}.",
            ]
        )
    return "\n".join(f"- {line}" for line in lines)


def _output_schema(
    phase: ExecutionPhase,
    profile: PromptProfile,
    include_tool_protocol: bool,
) -> str:
    action_line = "one JSON tool object" if include_tool_protocol else "one native tool call"
    lines = [
        f"Return exactly one action: {action_line}, or a final answer only when evidence already supports it.",
        "If calling a tool, return no extra prose.",
        "If answering, keep it brief and evidence-backed.",
        "Never output STEP_COMPLETE or TASK_COMPLETE unless the runtime requested completion evidence.",
    ]
    if phase not in {ExecutionPhase.COMPLETE, ExecutionPhase.EXPLORE}:
        lines.append("Do not claim the task or step is complete in this phase.")
    if profile == PromptProfile.SMALL:
        lines.append("No plans, previews, apologies, or future-tense promises.")
    return "\n".join(f"- {line}" for line in lines)


def _failure_recovery_rules(profile: PromptProfile) -> str:
    lines = [
        "If a tool failed, do not repeat the same action unchanged.",
        "If a file path failed, search or read a concrete candidate next.",
        "If a phase denies a tool, choose an allowed tool.",
        "If required evidence is missing, verify instead of declaring success.",
        "If user input is required, ask one specific question.",
    ]
    if profile == PromptProfile.STANDARD:
        lines.append("Use the latest observation to explain the blocker only when no tool can progress.")
    return "\n".join(f"- {line}" for line in lines)


def _clean_list(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        text = str(getattr(value, "value", value) or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
    return cleaned
