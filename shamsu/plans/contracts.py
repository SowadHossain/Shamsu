"""Structured executable contracts for approved plan steps."""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from shamsu.indexer.policy import SOURCE_SUFFIXES, walk_workspace_files
from shamsu.runtime.phase_contracts import ExecutionPhase
from shamsu.runtime.task_state import EvidenceType, ExecutionPlan, PlanStep, RiskLevel
from shamsu.safety.sandbox import Sandbox, SecurityError


CONTRACTS_SUFFIX = ".contracts.json"
DEFAULT_MUTATION_EVIDENCE = EvidenceType.FILE_CHANGED.value


@dataclass
class ScopeDecision:
    path: str
    reason: str
    source: str = "preflight"


@dataclass
class FilePreflightResult:
    inspected_paths: list[str] = field(default_factory=list)
    candidate_files: list[str] = field(default_factory=list)
    planner_proposed_files: list[str] = field(default_factory=list)
    selected_files: list[str] = field(default_factory=list)
    allowed_write_paths: list[str] = field(default_factory=list)
    expected_write_paths: list[str] = field(default_factory=list)
    relevant_symbols: list[str] = field(default_factory=list)
    rationale: list[ScopeDecision] = field(default_factory=list)
    confidence: str = "medium"
    unresolved_ambiguity: str = ""


@dataclass
class TaskContract:
    task_id: str
    run_id: str
    objective: str
    requirement_refs: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    allowed_read_paths: list[str] = field(default_factory=list)
    candidate_files: list[str] = field(default_factory=list)
    allowed_write_paths: list[str] = field(default_factory=list)
    expected_write_paths: list[str] = field(default_factory=list)
    forbidden_paths: list[str] = field(default_factory=list)
    relevant_symbols: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    verification_requirements: list[str] = field(default_factory=list)
    phase: str = ExecutionPhase.AUTHOR.value
    status: str = "pending"
    attempt_id: str = "attempt-001"
    attempt_index: int = 1
    evidence_refs: list[str] = field(default_factory=list)
    planner_proposed_files: list[str] = field(default_factory=list)
    file_selection_rationale: list[ScopeDecision] = field(default_factory=list)
    preflight: FilePreflightResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return _contract_to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskContract":
        return _contract_from_dict(data)

    def to_runtime_plan(self, *, runtime_task_id: str, runtime_run_id: str, plan_id: str) -> ExecutionPlan:
        constraints = list(self.constraints)
        if self.allowed_write_paths:
            constraints.append("Locked write scope: " + ", ".join(self.allowed_write_paths))
        if self.forbidden_paths:
            constraints.append("Forbidden paths: " + ", ".join(self.forbidden_paths))
        verification = list(self.verification_requirements)
        if not verification:
            verification = ["Verify the changed files with the narrowest available project check."]
        constraints.extend(f"Verification requirement: {item}" for item in verification)
        step = PlanStep(
            step_id=self.task_id,
            title=self.objective[:80] or self.task_id,
            goal=self.objective,
            inputs=list(self.allowed_read_paths or self.candidate_files),
            expected_outputs=list(self.expected_write_paths or self.allowed_write_paths),
            constraints=constraints,
            allowed_tools=[
                "file.read",
                "code.search",
                "file.patch",
                "test.run",
                "request_scope_expansion",
                "scope.expand",
                "read_file",
                "edit_file",
                "append_file",
                "write_file",
                "run_command",
            ],
            acceptance_criteria=list(self.acceptance_criteria),
            required_evidence=[DEFAULT_MUTATION_EVIDENCE],
            risk_level=RiskLevel.MEDIUM,
            approval_required=False,
            dependencies=list(self.dependencies),
        )
        return ExecutionPlan(
            plan_id=plan_id,
            task_id=runtime_task_id,
            run_id=runtime_run_id,
            title=self.objective[:80] or "Task contract",
            summary="Structured Task Contract persisted from approved plan.",
            steps=[step],
        )


@dataclass(frozen=True)
class ContractValidationResult:
    ok: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScopeExpansionResult:
    ok: bool
    contract: TaskContract
    message: str
    path: str = ""


def contracts_path(workspace: Path, plan_id: str) -> Path:
    return Sandbox(workspace).validate(Path(".shamsu") / "plans" / f"{plan_id}{CONTRACTS_SUFFIX}")


def write_plan_contracts(workspace: Path, plan_id: str, contracts: list[TaskContract]) -> Path:
    path = contracts_path(workspace, plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"plan_id": plan_id, "contracts": [_contract_to_dict(contract) for contract in contracts]}
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    return path


def load_plan_contracts(workspace: Path, plan_id: str) -> list[TaskContract]:
    path = contracts_path(workspace, plan_id)
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    raw = data.get("contracts", []) if isinstance(data, dict) else []
    return [_contract_from_dict(item) for item in raw if isinstance(item, dict)]


def contracts_from_planner_data(plan_id: str, data: dict[str, Any], plan_steps: list[Any], task: str) -> list[TaskContract]:
    verification = _listify(data.get("verification"))
    plan_files = _dedupe(_normalize_hint(path) for path in _listify(data.get("files")))
    contracts: list[TaskContract] = []
    previous_step_id = ""
    for index, step in enumerate(plan_steps, 1):
        step_id = f"{plan_id}-task-{index:03d}"
        target_file = _normalize_hint(getattr(step, "target_file", ""))
        proposed = _dedupe([target_file, *plan_files])
        verify = _listify(getattr(step, "verify", "")) or verification
        acceptance = _listify(getattr(step, "acceptance_criteria", "")) or [
            f"Complete step: {getattr(step, 'description', '').strip()}"
        ]
        constraints = _listify(getattr(step, "constraints", ""))
        contracts.append(
            TaskContract(
                task_id=step_id,
                run_id=plan_id,
                objective=str(getattr(step, "description", "")).strip(),
                requirement_refs=[task.strip()] if task.strip() else [],
                dependencies=[previous_step_id] if previous_step_id else [],
                expected_write_paths=[target_file] if target_file else [],
                allowed_write_paths=[target_file] if target_file else [],
                planner_proposed_files=proposed,
                constraints=constraints,
                acceptance_criteria=acceptance,
                verification_requirements=verify,
            )
        )
        previous_step_id = step_id
    return contracts


def contracts_from_markdown(plan_id: str, task: str, steps: list[str], markdown: str = "") -> list[TaskContract]:
    verification = _extract_verification(markdown)
    contracts: list[TaskContract] = []
    previous_step_id = ""
    source_steps = steps or [task]
    for index, text in enumerate(source_steps, 1):
        step_id = f"{plan_id}-task-{index:03d}"
        paths = _paths_from_text(text)
        contracts.append(
            TaskContract(
                task_id=step_id,
                run_id=plan_id,
                objective=text.strip() or task.strip() or "Execute approved plan",
                requirement_refs=[task.strip()] if task.strip() else [],
                dependencies=[previous_step_id] if previous_step_id else [],
                expected_write_paths=paths,
                allowed_write_paths=paths,
                planner_proposed_files=paths,
                acceptance_criteria=[f"Complete approved step: {text.strip() or task.strip()}"],
                verification_requirements=verification,
            )
        )
        previous_step_id = step_id
    return contracts


def validate_contract(contract: TaskContract, workspace: Path) -> ContractValidationResult:
    errors: list[str] = []
    if not contract.task_id:
        errors.append("task_id is required")
    if not contract.run_id:
        errors.append("run_id is required")
    if not contract.objective.strip():
        errors.append(f"{contract.task_id}: objective is required")
    if not contract.acceptance_criteria:
        errors.append(f"{contract.task_id}: acceptance_criteria is required")
    if not contract.verification_requirements:
        errors.append(f"{contract.task_id}: verification_requirements is required")
    normalized_allowed = _normalize_paths(contract.allowed_write_paths, workspace, errors, "allowed_write_paths")
    normalized_expected = _normalize_paths(contract.expected_write_paths, workspace, errors, "expected_write_paths")
    normalized_forbidden = _normalize_paths(contract.forbidden_paths, workspace, errors, "forbidden_paths")
    allowed_set = set(normalized_allowed)
    for path in normalized_expected:
        if allowed_set and not _path_in_scope(path, allowed_set):
            errors.append(f"{contract.task_id}: expected path {path} is outside allowed_write_paths")
    for path in normalized_forbidden:
        if _path_in_scope(path, allowed_set):
            errors.append(f"{contract.task_id}: forbidden path {path} conflicts with allowed_write_paths")
    return ContractValidationResult(not errors, tuple(errors))


def run_file_preflight(contract: TaskContract, workspace: Path, *, max_candidates: int = 8) -> TaskContract:
    existing = _workspace_source_files(workspace, limit=160)
    proposed = _dedupe([*contract.planner_proposed_files, *contract.expected_write_paths, *contract.allowed_write_paths])
    candidate_scores: dict[str, int] = {}
    for path in proposed:
        if path:
            candidate_scores[path] = candidate_scores.get(path, 0) + 10
    keywords = _keywords(contract.objective + " " + " ".join(contract.requirement_refs))
    for path in existing:
        lower = path.lower()
        score = 0
        for keyword in keywords:
            if keyword in lower:
                score += 2
        if score:
            candidate_scores[path] = candidate_scores.get(path, 0) + score
    candidates = [
        path for path, _score in sorted(candidate_scores.items(), key=lambda item: (-item[1], item[0]))
    ][:max_candidates]
    selected = _dedupe(path for path in proposed if path)
    if not selected:
        selected = candidates[:2]
    selected_set = set(selected)
    rationale = [
        ScopeDecision(path=path, reason=_scope_reason(path, proposed, existing), source="preflight")
        for path in selected
    ]
    preflight = FilePreflightResult(
        inspected_paths=[path for path in candidates if Path(path).suffix],
        candidate_files=candidates,
        planner_proposed_files=proposed,
        selected_files=selected,
        allowed_write_paths=selected,
        expected_write_paths=_dedupe(contract.expected_write_paths or selected),
        relevant_symbols=[],
        rationale=rationale,
        confidence="high" if proposed else ("medium" if selected else "low"),
        unresolved_ambiguity="" if selected else "No concrete file target could be inferred.",
    )
    contract.candidate_files = candidates
    contract.allowed_write_paths = selected
    contract.expected_write_paths = _dedupe(contract.expected_write_paths or selected)
    contract.allowed_read_paths = _dedupe([*contract.allowed_read_paths, *candidates])
    contract.file_selection_rationale = rationale
    contract.preflight = preflight
    contract.phase = ExecutionPhase.AUTHOR.value
    return contract


def request_scope_expansion(contract: TaskContract, workspace: Path, path: str, reason: str) -> ScopeExpansionResult:
    normalized = _normalize_hint(path)
    if not normalized:
        return ScopeExpansionResult(False, contract, "Scope expansion rejected: missing path.")
    errors: list[str] = []
    normalized_list = _normalize_paths([normalized], workspace, errors, "scope_expansion")
    if errors:
        return ScopeExpansionResult(False, contract, "Scope expansion rejected: " + "; ".join(errors), normalized)
    normalized = normalized_list[0]
    if normalized in contract.forbidden_paths:
        return ScopeExpansionResult(False, contract, f"Scope expansion rejected: {normalized} is forbidden.", normalized)
    if not reason.strip():
        return ScopeExpansionResult(False, contract, "Scope expansion rejected: reason is required.", normalized)
    if normalized in contract.allowed_write_paths:
        return ScopeExpansionResult(True, contract, f"{normalized} is already in the locked write scope.", normalized)
    contract.allowed_write_paths = _dedupe([*contract.allowed_write_paths, normalized])
    contract.expected_write_paths = _dedupe([*contract.expected_write_paths, normalized])
    contract.candidate_files = _dedupe([*contract.candidate_files, normalized])
    contract.file_selection_rationale.append(
        ScopeDecision(path=normalized, reason=reason.strip()[:300], source="scope_expansion")
    )
    if contract.preflight is not None:
        contract.preflight.allowed_write_paths = list(contract.allowed_write_paths)
        contract.preflight.expected_write_paths = list(contract.expected_write_paths)
        contract.preflight.selected_files = list(contract.allowed_write_paths)
        contract.preflight.rationale = list(contract.file_selection_rationale)
    return ScopeExpansionResult(True, contract, f"Scope expansion approved for {normalized}.", normalized)


def contract_prompt(contract: TaskContract) -> str:
    lines = [
        "Task Contract:",
        f"- task_id: {contract.task_id}",
        f"- attempt_id: {contract.attempt_id}",
        f"- objective: {contract.objective}",
        "- locked_write_scope: " + (", ".join(contract.allowed_write_paths) or "none"),
        "- expected_write_paths: " + (", ".join(contract.expected_write_paths) or "none"),
        "- candidate_files: " + (", ".join(contract.candidate_files[:8]) or "none"),
        "- acceptance_criteria: " + ("; ".join(contract.acceptance_criteria) or "none"),
        "- verification_requirements: " + ("; ".join(contract.verification_requirements) or "none"),
    ]
    if contract.file_selection_rationale:
        lines.append(
            "- scope_rationale: "
            + "; ".join(f"{item.path}: {item.reason}" for item in contract.file_selection_rationale[:6])
        )
    lines.append(
        "Do not write outside locked_write_scope. If another file is genuinely required, "
        "call request_scope_expansion with the path and reason before writing it."
    )
    return "\n".join(lines)


def _contract_to_dict(contract: TaskContract) -> dict[str, Any]:
    return asdict(contract)


def _contract_from_dict(data: dict[str, Any]) -> TaskContract:
    data = dict(data)
    if isinstance(data.get("preflight"), dict):
        preflight = dict(data["preflight"])
        preflight["rationale"] = [
            ScopeDecision(**item) for item in preflight.get("rationale", []) if isinstance(item, dict)
        ]
        data["preflight"] = FilePreflightResult(**preflight)
    else:
        data["preflight"] = None
    data["file_selection_rationale"] = [
        ScopeDecision(**item) for item in data.get("file_selection_rationale", []) if isinstance(item, dict)
    ]
    for key in (
        "requirement_refs",
        "dependencies",
        "allowed_read_paths",
        "candidate_files",
        "allowed_write_paths",
        "expected_write_paths",
        "forbidden_paths",
        "relevant_symbols",
        "constraints",
        "acceptance_criteria",
        "verification_requirements",
        "evidence_refs",
        "planner_proposed_files",
    ):
        value = data.get(key)
        data[key] = value if isinstance(value, list) else []
    data.setdefault("attempt_id", "attempt-001")
    data.setdefault("attempt_index", 1)
    data.setdefault("phase", ExecutionPhase.AUTHOR.value)
    data.setdefault("status", "pending")
    return TaskContract(**data)


def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def _dedupe(values: Any) -> list[str]:
    out: list[str] = []
    for value in values:
        text = _normalize_hint(str(value))
        if text and text not in out:
            out.append(text)
    return out


def _normalize_hint(path: str) -> str:
    return str(path or "").strip().replace("\\", "/").lstrip("./")


def _normalize_paths(paths: list[str], workspace: Path, errors: list[str], label: str) -> list[str]:
    sandbox = Sandbox(workspace)
    out: list[str] = []
    for raw in paths:
        path = _normalize_hint(raw)
        if not path:
            continue
        try:
            resolved = sandbox.validate(path)
            normalized = resolved.relative_to(Path(workspace).resolve()).as_posix()
        except (SecurityError, ValueError) as exc:
            errors.append(f"{label}: {path} is outside workspace ({exc})")
            continue
        if normalized not in out:
            out.append(normalized)
    return out


def _path_in_scope(path: str, allowed: set[str]) -> bool:
    return any(path == item or path.startswith(item.rstrip("/") + "/") for item in allowed)


def _paths_from_text(text: str) -> list[str]:
    found = re.findall(r"`([^`]+\.[A-Za-z0-9]+)`", text)
    found += re.findall(r"\b([A-Za-z0-9_./-]+\.(?:py|js|ts|tsx|jsx|html|css|md|json|toml|yaml|yml))\b", text)
    return _dedupe(found)


def _extract_verification(markdown: str) -> list[str]:
    lines: list[str] = []
    in_verification = False
    for line in markdown.splitlines():
        heading = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading:
            in_verification = heading.group(1).strip().lower().startswith("verification")
            continue
        if in_verification and line.strip():
            lines.append(line.strip().lstrip("-*").strip())
    return lines or ["Run the narrowest available project verification."]


def _keywords(text: str) -> list[str]:
    stop = {"the", "and", "for", "with", "this", "that", "into", "from", "add", "fix", "update"}
    words = re.findall(r"[A-Za-z][A-Za-z0-9_]{2,}", text.lower())
    return [word for word in dict.fromkeys(words) if word not in stop][:12]


def _scope_reason(path: str, proposed: list[str], existing: list[str]) -> str:
    if path in proposed and path in existing:
        return "planner proposed this existing workspace file"
    if path in proposed:
        return "planner proposed this path"
    return "preflight selected this candidate from repository filenames"


def _workspace_source_files(workspace: Path, limit: int = 160) -> list[str]:
    found: list[tuple[float, str]] = []
    try:
        for path in walk_workspace_files(workspace, suffixes=SOURCE_SUFFIXES, indexable_only=True):
            try:
                found.append((path.stat().st_mtime, path.relative_to(workspace).as_posix()))
            except OSError:
                continue
    except OSError:
        return []
    found.sort(reverse=True)
    return [name for _mtime, name in found[:limit]]


def new_attempt_id(index: int) -> str:
    return f"attempt-{index:03d}-{uuid.uuid4().hex[:6]}"
