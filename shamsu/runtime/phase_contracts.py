"""Deterministic phase contracts for production agent tool execution."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from shamsu.runtime.advanced_capabilities import advanced_capability_denial


class ExecutionPhase(str, Enum):
    EXPLORE = "EXPLORE"
    PLAN = "PLAN"
    AUTHOR = "AUTHOR"
    VERIFY = "VERIFY"
    REPAIR = "REPAIR"
    DEPLOY = "DEPLOY"
    COMPLETE = "COMPLETE"


READ_TOOLS = frozenset(
    {
        "project.inspect",
        "code.search",
        "file.read",
        "git.inspect",
        "list_files",
        "read_file",
        "file_info",
        "find_file",
        "grep_files",
        "search_index",
        "web_search",
        "fetch_url",
        "ingest_docs",
        "search_docs",
        "ask_docs",
        "summarize_docs",
        "git_status",
        "git_status_full",
        "git_diff",
        "git_diff_staged",
        "git_diff_file",
        "git_branch",
        "git_branches",
        "git_remote",
        "git_log",
        "git_unpushed_commits",
        "git_stash_list",
        "ask_user",
    }
)

FILE_MUTATION_TOOLS = frozenset(
    {
        "file.patch",
        "edit_file",
        "append_file",
        "write_file",
        "move_file",
        "delete_file",
    }
)

GIT_MUTATION_TOOLS = frozenset(
    {
        "git.checkpoint",
        "git_add",
        "git_add_all",
        "git_init",
        "git_commit",
        "git_create_branch",
        "git_checkout",
        "git_restore",
        "git_stash_push",
        "git_stash_pop",
        "git_fetch",
        "git_pull",
        "git_push",
    }
)

VERIFY_COMMAND_PATTERNS = (
    r"\bpytest\b",
    r"\bunittest\b",
    r"\btox\b",
    r"\bnpm\s+(?:test|run\s+(?:test|lint|typecheck|build))\b",
    r"\bpnpm\s+(?:test|run\s+(?:test|lint|typecheck|build))\b",
    r"\byarn\s+(?:test|run\s+(?:test|lint|typecheck|build))\b",
    r"\bruff\s+check\b",
    r"\beslint\b",
    r"\bmypy\b",
    r"\bpyright\b",
    r"\btsc\b",
    r"\bpython\s+-m\s+(?:pytest|unittest|compileall|mypy)\b",
    r"\bgo\s+test\b",
    r"\bcargo\s+(?:test|check|build)\b",
    r"\bdotnet\s+(?:test|build)\b",
    r"\bgit\s+(?:status|diff|log|branch|remote)\b",
    r"\bcurl\b",
    r"\binvoke-webrequest\b",
    r"\binvoke-restmethod\b",
)

AUTHOR_CHECK_COMMAND_PATTERNS = (
    r"\bpytest\b",
    r"\bruff\s+check\b",
    r"\bmypy\b",
    r"\bpyright\b",
    r"\btsc\s+--noEmit\b",
    r"\bpython\s+-m\s+(?:pytest|compileall|mypy)\b",
    r"\bnpm\s+(?:test|run\s+(?:test|lint|typecheck))\b",
    r"\bpnpm\s+(?:test|run\s+(?:test|lint|typecheck))\b",
    r"\byarn\s+(?:test|run\s+(?:test|lint|typecheck))\b",
    r"\bgo\s+test\b",
    r"\bcargo\s+(?:test|check)\b",
    r"\bdotnet\s+test\b",
)

DEPLOY_COMMAND_PATTERNS = (
    r"\bdocker\b",
    r"\bdocker-compose\b",
    r"\bdocker\s+compose\b",
    r"\bcurl\b",
    r"\binvoke-webrequest\b",
    r"\binvoke-restmethod\b",
)

DANGEROUS_COMMAND_PATTERNS = (
    r"\brm\s+-rf\b",
    r"\bremove-item\b.*\b-recurse\b",
    r"\bdel\s+/[sq]\b",
    r"\brmdir\s+/[sq]\b",
    r"\bdrop\s+database\b",
    r"\btruncate\s+table\b",
    r"\bkubectl\b",
    r"\bterraform\b",
    r"\baws\b",
    r"\bgcloud\b",
    r"\baz\b",
    r"\bgit\s+(?:push|pull|fetch|checkout|restore|reset|clean)\b",
)


@dataclass(frozen=True)
class PhaseContract:
    phase: ExecutionPhase
    allowed_tools: frozenset[str] = field(default_factory=frozenset)
    allowed_command_patterns: tuple[str, ...] = ()
    no_project_mutation: bool = False
    reason: str = ""


@dataclass(frozen=True)
class PhasePolicyDecision:
    allowed: bool
    requested_tool: str
    current_phase: ExecutionPhase
    allowed_tools: tuple[str, ...]
    reason: str = ""

    def denial_payload(self) -> dict[str, Any]:
        return {
            "requested_tool": self.requested_tool,
            "current_phase": self.current_phase.value,
            "allowed_tools": list(self.allowed_tools),
            "reason": self.reason,
            "phase_denied": True,
        }


PHASE_CONTRACTS: dict[ExecutionPhase, PhaseContract] = {
    ExecutionPhase.EXPLORE: PhaseContract(
        phase=ExecutionPhase.EXPLORE,
        allowed_tools=READ_TOOLS,
        no_project_mutation=True,
        reason="EXPLORE is read-only: inspect files, code, Git, docs, and configuration.",
    ),
    ExecutionPhase.PLAN: PhaseContract(
        phase=ExecutionPhase.PLAN,
        allowed_tools=READ_TOOLS,
        no_project_mutation=True,
        reason="PLAN is read-only: create and analyze the plan without changing the project.",
    ),
    ExecutionPhase.AUTHOR: PhaseContract(
        phase=ExecutionPhase.AUTHOR,
        allowed_tools=READ_TOOLS
        | {"file.patch", "test.run", "edit_file", "append_file", "write_file", "move_file", "run_command"},
        allowed_command_patterns=AUTHOR_CHECK_COMMAND_PATTERNS,
        reason="AUTHOR permits file patches and narrow code checks only.",
    ),
    ExecutionPhase.VERIFY: PhaseContract(
        phase=ExecutionPhase.VERIFY,
        allowed_tools=READ_TOOLS | {"test.run", "git.checkpoint", "run_command"},
        allowed_command_patterns=VERIFY_COMMAND_PATTERNS,
        no_project_mutation=True,
        reason="VERIFY permits tests, lint, type checks, builds, diffs, and health checks; source edits are blocked.",
    ),
    ExecutionPhase.REPAIR: PhaseContract(
        phase=ExecutionPhase.REPAIR,
        allowed_tools=READ_TOOLS
        | {"file.patch", "test.run", "edit_file", "append_file", "write_file", "move_file", "run_command"},
        allowed_command_patterns=AUTHOR_CHECK_COMMAND_PATTERNS + VERIFY_COMMAND_PATTERNS,
        reason="REPAIR permits targeted edits and verification based on failure evidence.",
    ),
    ExecutionPhase.DEPLOY: PhaseContract(
        phase=ExecutionPhase.DEPLOY,
        allowed_tools=READ_TOOLS | {"test.run", "git.checkpoint", "run_command"},
        allowed_command_patterns=DEPLOY_COMMAND_PATTERNS + VERIFY_COMMAND_PATTERNS,
        reason="DEPLOY permits local Docker operations, service health checks, and smoke tests.",
    ),
    ExecutionPhase.COMPLETE: PhaseContract(
        phase=ExecutionPhase.COMPLETE,
        allowed_tools=frozenset(),
        no_project_mutation=True,
        reason="COMPLETE produces the final report from registered runtime evidence; tools are closed.",
    ),
}

_PHASE_ALIASES = {
    "initialized": ExecutionPhase.EXPLORE,
    "created": ExecutionPhase.EXPLORE,
    "running": ExecutionPhase.AUTHOR,
    "planned": ExecutionPhase.PLAN,
    "plan_contract_saved": ExecutionPhase.PLAN,
    "approval": ExecutionPhase.PLAN,
    "executing_plan": ExecutionPhase.AUTHOR,
    "step_completed": ExecutionPhase.VERIFY,
    "replanned": ExecutionPhase.PLAN,
    "blocked": ExecutionPhase.REPAIR,
    "failed": ExecutionPhase.REPAIR,
    "cancelled": ExecutionPhase.COMPLETE,
    "timed_out": ExecutionPhase.COMPLETE,
    "completed": ExecutionPhase.COMPLETE,
}


def normalize_phase(value: str | ExecutionPhase | None) -> ExecutionPhase:
    if isinstance(value, ExecutionPhase):
        return value
    raw = str(value or "").strip()
    if not raw:
        return ExecutionPhase.AUTHOR
    upper = raw.upper()
    if upper in ExecutionPhase.__members__:
        return ExecutionPhase[upper]
    return _PHASE_ALIASES.get(raw.lower(), ExecutionPhase.AUTHOR)


def normalize_risk(value: str | None) -> str:
    risk = str(value or "medium").strip().lower()
    return risk if risk in {"low", "medium", "high"} else "medium"


def phase_allowed_tools(
    phase: str | ExecutionPhase | None,
    available_tools: set[str] | None = None,
    enabled_advanced_capabilities: set[str] | frozenset[str] | None = None,
) -> tuple[str, ...]:
    normalized = normalize_phase(phase)
    allowed = set(PHASE_CONTRACTS[normalized].allowed_tools)
    allowed = {
        tool
        for tool in allowed
        if advanced_capability_denial(tool, enabled=enabled_advanced_capabilities) is None
    }
    if available_tools is not None:
        allowed = {tool for tool in allowed if _matches_tool(tool, available_tools)}
    return tuple(sorted(allowed))


def evaluate_phase_tool_policy(
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    phase: str | ExecutionPhase | None,
    task_risk: str | None = None,
    available_tools: set[str] | None = None,
    enabled_advanced_capabilities: set[str] | frozenset[str] | None = None,
) -> PhasePolicyDecision:
    current_phase = normalize_phase(phase)
    contract = PHASE_CONTRACTS[current_phase]
    requested = str(tool_name or "")
    phase_tools = set(contract.allowed_tools)
    allowed_tools = phase_allowed_tools(
        current_phase,
        available_tools,
        enabled_advanced_capabilities=enabled_advanced_capabilities,
    )
    if not _matches_tool(requested, phase_tools):
        return PhasePolicyDecision(
            False,
            requested,
            current_phase,
            allowed_tools,
            f"{requested} is not permitted in {current_phase.value}. {contract.reason}",
        )

    advanced_denial = advanced_capability_denial(
        requested,
        arguments,
        enabled=enabled_advanced_capabilities,
    )
    if advanced_denial is not None:
        _capability, reason = advanced_denial
        return PhasePolicyDecision(
            False,
            requested,
            current_phase,
            allowed_tools,
            reason,
        )

    risk = normalize_risk(task_risk)
    if requested == "run_command":
        command = str((arguments or {}).get("command") or "")
        command_decision = _evaluate_command(command, current_phase, risk, contract)
        if command_decision:
            return PhasePolicyDecision(
                False,
                requested,
                current_phase,
                allowed_tools,
                command_decision,
            )

    if risk == "high" and requested in {"file.patch", "edit_file", "append_file", "write_file"}:
        if current_phase != ExecutionPhase.REPAIR:
            return PhasePolicyDecision(
                False,
                requested,
                current_phase,
                allowed_tools,
                "High-risk file edits must run through the REPAIR phase.",
            )

    if risk == "high" and requested in {"move_file", "delete_file"} | GIT_MUTATION_TOOLS:
        return PhasePolicyDecision(
            False,
            requested,
            current_phase,
            allowed_tools,
            "High-risk tasks cannot perform destructive file or Git mutation tools.",
        )

    if requested.startswith("mcp__") and current_phase in {
        ExecutionPhase.EXPLORE,
        ExecutionPhase.PLAN,
        ExecutionPhase.VERIFY,
        ExecutionPhase.COMPLETE,
    }:
        return PhasePolicyDecision(
            False,
            requested,
            current_phase,
            allowed_tools,
            "External MCP calls are not trusted as read-only by the phase contract.",
        )

    return PhasePolicyDecision(True, requested, current_phase, allowed_tools)


def phase_for_step(
    *,
    allowed_tools: list[str] | tuple[str, ...],
    required_evidence: list[str] | tuple[str, ...],
    approval_required: bool,
    risk_level: str | None,
) -> ExecutionPhase:
    tools = {str(tool) for tool in allowed_tools}
    evidence = {str(item) for item in required_evidence}
    if tools & FILE_MUTATION_TOOLS:
        return ExecutionPhase.REPAIR if normalize_risk(risk_level) == "high" else ExecutionPhase.AUTHOR
    if any(
        item
        in {
            "test_passed",
            "typecheck_passed",
            "lint_passed",
            "build_passed",
            "service_healthy",
            "migration_passed",
            "smoke_test_passed",
            "git_diff_reviewed",
        }
        for item in evidence
    ):
        return ExecutionPhase.VERIFY
    if approval_required:
        return ExecutionPhase.PLAN
    return ExecutionPhase.EXPLORE


def _matches_tool(tool_name: str, allowed: set[str] | frozenset[str]) -> bool:
    return tool_name in allowed or any(
        pattern.endswith("*") and tool_name.startswith(pattern[:-1]) for pattern in allowed
    )


def _evaluate_command(
    command: str,
    phase: ExecutionPhase,
    risk: str,
    contract: PhaseContract,
) -> str:
    lowered = command.strip().lower()
    if not lowered:
        return "Missing command."
    if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in DANGEROUS_COMMAND_PATTERNS):
        return f"Command is outside the safe command set for {phase.value}."
    if not contract.allowed_command_patterns:
        return f"run_command is not available for commands in {phase.value}."
    if not any(
        re.search(pattern, lowered, flags=re.IGNORECASE)
        for pattern in contract.allowed_command_patterns
    ):
        return f"Command does not match the allowed command set for {phase.value}."
    if risk == "high" and phase not in {ExecutionPhase.VERIFY, ExecutionPhase.DEPLOY}:
        return "High-risk tasks may only run commands in VERIFY or DEPLOY phases."
    return ""
