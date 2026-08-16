"""Advanced capability contracts and deny-by-default runtime gates."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class AdvancedCapability(str, Enum):
    DOCUMENTATION_RETRIEVAL = "documentation_retrieval"
    PACKAGE_INSTALLATION = "package_installation"
    DOCKER = "docker"
    DATABASES = "databases"
    PRD_WORKFLOWS = "prd_workflows"
    LARGER_PROJECT_AUTONOMY = "larger_project_autonomy"


@dataclass(frozen=True)
class AdvancedCapabilityContract:
    capability: AdvancedCapability
    order: int
    phase_rules: tuple[str, ...]
    risk_policy: str
    evidence_types: tuple[str, ...]
    verification_strategy: tuple[str, ...]
    task_evaluations: tuple[str, ...]
    tool_names: frozenset[str] = field(default_factory=frozenset)
    command_patterns: tuple[str, ...] = ()


ADVANCED_CAPABILITY_CONTRACTS: dict[AdvancedCapability, AdvancedCapabilityContract] = {
    AdvancedCapability.DOCUMENTATION_RETRIEVAL: AdvancedCapabilityContract(
        capability=AdvancedCapability.DOCUMENTATION_RETRIEVAL,
        order=1,
        phase_rules=(
            "EXPLORE and PLAN may retrieve and summarize external documentation.",
            "AUTHOR may consume cited documentation already registered as project context.",
        ),
        risk_policy="Read-only, citation-required, untrusted text cannot override runtime policy.",
        evidence_types=("documentation_retrieved", "documentation_cited"),
        verification_strategy=(
            "Persist source URI/path and content hash.",
            "Require cited snippets for decisions derived from documentation.",
        ),
        task_evaluations=("documentation_retrieval_precision", "malicious_document_instruction"),
        tool_names=frozenset({"ingest_docs", "search_docs", "ask_docs", "summarize_docs"}),
    ),
    AdvancedCapability.PACKAGE_INSTALLATION: AdvancedCapabilityContract(
        capability=AdvancedCapability.PACKAGE_INSTALLATION,
        order=2,
        phase_rules=("AUTHOR may propose dependency changes.", "VERIFY runs install checks in isolation."),
        risk_policy="Mutating, approval-controlled, lockfile diff must be reviewed.",
        evidence_types=("package_install_planned", "lockfile_changed", "install_verified"),
        verification_strategy=("Inspect manifest diff.", "Run install with timeout.", "Run tests after install."),
        task_evaluations=("package_install_success", "dependency_conflict_recovery"),
        command_patterns=(
            r"\b(?:pip|pip3|python\s+-m\s+pip)\s+install\b",
            r"\b(?:npm|pnpm|yarn)\s+(?:install|add)\b",
            r"\bpoetry\s+add\b",
            r"\buv\s+add\b",
            r"\bcargo\s+add\b",
            r"\bgo\s+get\b",
            r"\bdotnet\s+add\s+package\b",
        ),
    ),
    AdvancedCapability.DOCKER: AdvancedCapabilityContract(
        capability=AdvancedCapability.DOCKER,
        order=3,
        phase_rules=("DEPLOY owns Docker validation, build, start, status, logs, health, and smoke tests.",),
        risk_policy="Local-only Docker operations; no registry push or destructive volume operations by default.",
        evidence_types=(
            "compose_validated",
            "docker_build_passed",
            "service_healthy",
            "smoke_test_passed",
        ),
        verification_strategy=(
            "Run compose config validation before build.",
            "Capture build/status/log evidence.",
            "Require health check and smoke test evidence before completion.",
        ),
        task_evaluations=("compose_validation", "docker_smoke_test", "service_health_recovery"),
        command_patterns=(r"\bdocker\b", r"\bdocker-compose\b", r"\bdocker\s+compose\b"),
    ),
    AdvancedCapability.DATABASES: AdvancedCapabilityContract(
        capability=AdvancedCapability.DATABASES,
        order=4,
        phase_rules=(
            "EXPLORE may inspect schemas and run read-only queries.",
            "AUTHOR may generate migrations.",
            "VERIFY tests migrations against disposable databases.",
            "Writes require explicit approval and rollback evidence.",
        ),
        risk_policy="Read-only by default; writes and migrations require approval and rollback plan.",
        evidence_types=(
            "schema_inspected",
            "readonly_query_passed",
            "migration_generated",
            "migration_passed",
            "rollback_verified",
        ),
        verification_strategy=(
            "Classify query mutability before execution.",
            "Run generated migrations on disposable state.",
            "Record rollback commands and results.",
        ),
        task_evaluations=("readonly_query_safety", "migration_disposable_test", "rollback_recovery"),
        command_patterns=(
            r"\bpsql\b",
            r"\bmysql\b",
            r"\bsqlite3\b",
            r"\balembic\b",
            r"\bprisma\s+migrate\b",
            r"\bmanage\.py\s+migrate\b",
        ),
    ),
    AdvancedCapability.PRD_WORKFLOWS: AdvancedCapabilityContract(
        capability=AdvancedCapability.PRD_WORKFLOWS,
        order=5,
        phase_rules=("PLAN owns PRD extraction, ambiguity surfacing, architecture proposals, and backlog shaping.",),
        risk_policy="Planning-only until acceptance criteria and approvals are explicit.",
        evidence_types=("requirements_extracted", "ambiguities_recorded", "acceptance_criteria_defined"),
        verification_strategy=(
            "Extract structured requirements.",
            "Reject unverifiable milestones.",
            "Tie backlog items to acceptance criteria.",
        ),
        task_evaluations=("prd_requirement_extraction", "prd_ambiguity_detection", "vertical_slice_quality"),
    ),
    AdvancedCapability.LARGER_PROJECT_AUTONOMY: AdvancedCapabilityContract(
        capability=AdvancedCapability.LARGER_PROJECT_AUTONOMY,
        order=6,
        phase_rules=("All phases participate through bounded plan steps and checkpoints.",),
        risk_policy="Requires proven checkpoint/resume, repair, and verification performance.",
        evidence_types=("multi_step_verified", "checkpoint_recovered", "bounded_autonomy_verified"),
        verification_strategy=(
            "Limit every step.",
            "Require checkpoints between steps.",
            "Compare benchmark deltas against the previous version.",
        ),
        task_evaluations=("multi_file_feature", "checkpoint_resume", "bounded_repair_failure"),
    ),
}


def normalize_advanced_capabilities(values: set[str] | frozenset[str] | None) -> frozenset[AdvancedCapability]:
    normalized: set[AdvancedCapability] = set()
    for value in values or set():
        try:
            normalized.add(value if isinstance(value, AdvancedCapability) else AdvancedCapability(str(value)))
        except ValueError:
            continue
    return frozenset(normalized)


def required_capability_for_tool_or_command(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> AdvancedCapability | None:
    requested = str(tool_name or "")
    for capability, contract in ADVANCED_CAPABILITY_CONTRACTS.items():
        if requested in contract.tool_names:
            return capability
    if requested not in {"run_command", "test.run"}:
        return None
    command = str((arguments or {}).get("command") or "")
    lowered = command.lower()
    for capability, contract in ADVANCED_CAPABILITY_CONTRACTS.items():
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in contract.command_patterns):
            return capability
    return None


def advanced_capability_denial(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    enabled: set[str] | frozenset[str] | None = None,
) -> tuple[AdvancedCapability, str] | None:
    capability = required_capability_for_tool_or_command(tool_name, arguments)
    if capability is None:
        return None
    enabled_capabilities = normalize_advanced_capabilities(enabled)
    if capability in enabled_capabilities:
        return None
    contract = ADVANCED_CAPABILITY_CONTRACTS[capability]
    return (
        capability,
        (
            f"Advanced capability {capability.value} is disabled until the core coding "
            "benchmark passes inspect->plan->retrieve->patch->test->verify->checkpoint. "
            f"Enable order {contract.order} only through the readiness gate."
        ),
    )
