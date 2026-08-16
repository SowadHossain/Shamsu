"""Durable PRD milestone execution artifacts.

This module does not execute model calls. It owns the small state files a PRD
executor needs so long builds can resume from artifacts instead of chat memory.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from shamsu.prd.contract import PRDContract
from shamsu.prd.requirements import (
    MilestoneRecord,
    RequirementLedger,
    compile_requirement_ledger,
    save_prd_execution_artifacts,
)
from shamsu.registry.blueprints import resolve_blueprints
from shamsu.safety.sandbox import Sandbox

SCHEMA_VERSION = 1
EXECUTIONS_DIRNAME = "prd-executions"
COMPLETED_STATUSES = {"implemented", "verified"}
MAX_MODEL_PREFLIGHT_LIST_ITEMS = 24

_BASE_ALLOWED_TOOLS = {
    "append_file",
    "file_info",
    "find_file",
    "grep_files",
    "list_files",
    "read_file",
    "run_command",
    "write_file",
    "edit_file",
}
_SKILL_TOOL_HINTS = {
    "react-vite": {"browser_open", "browser_read", "browser_screenshot"},
    "ui-designer": {"browser_open", "browser_read", "browser_screenshot"},
    "testing": {"run_command"},
    "sqlite-persistence": {"run_command"},
    "sql-databases": {"run_command"},
    "mcp-tools": {"mcp:*"},
}

MODEL_PREFLIGHT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "milestone_id": {"type": "string"},
        "requirement_ids": {"type": "array", "items": {"type": "string"}},
        "active_skills": {"type": "array", "items": {"type": "string"}},
        "expected_files": {"type": "array", "items": {"type": "string"}},
        "allowed_tools": {"type": "array", "items": {"type": "string"}},
        "verifier": {"type": "string"},
        "context_focus": {"type": "array", "items": {"type": "string"}},
        "implementation_steps": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
        "blocker_question": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": [
        "milestone_id",
        "requirement_ids",
        "active_skills",
        "expected_files",
        "allowed_tools",
        "verifier",
    ],
}


def initialize_prd_execution(
    workspace: Path,
    user_request: str,
    contract: PRDContract,
    *,
    prd_path: str = "",
    execution_key: str = "",
) -> tuple[Path, dict[str, Any]]:
    """Create or load the durable execution state for a PRD contract."""
    ledger = compile_requirement_ledger(contract)
    stack_profile = stack_profile_for_contract(contract)
    root = prd_execution_root(workspace, ledger.contract_hash, execution_key=execution_key)
    root.mkdir(parents=True, exist_ok=True)
    save_prd_execution_artifacts(contract, root)
    _write_preflights(root, ledger, stack_profile)

    path = root / "state.json"
    existing = _read_json(path)
    if existing and existing.get("contract_hash") == ledger.contract_hash:
        state = _merge_state(existing, ledger, user_request, prd_path, stack_profile)
    else:
        state = _new_state(ledger, user_request, prd_path, stack_profile)
    if not _has_acceptance_criteria(ledger):
        state = _block_missing_acceptance_criteria(state)
    state["execution_key"] = execution_key
    _write_json(path, state)
    return root, state


def prd_execution_root(
    workspace: Path,
    contract_hash: str,
    *,
    execution_key: str = "",
) -> Path:
    safe_hash = "".join(ch for ch in contract_hash if ch in "0123456789abcdef")[:16]
    safe_hash = safe_hash or "unknown-contract"
    if execution_key:
        safe_hash = f"{safe_hash}-{sha256(execution_key.encode('utf-8')).hexdigest()[:12]}"
    return Sandbox(workspace).validate(Path(".shamsu") / EXECUTIONS_DIRNAME / safe_hash)


def attach_task_id(root: Path, state: dict[str, Any], task_id: str) -> dict[str, Any]:
    updated = dict(state)
    updated["task_id"] = task_id
    updated["updated_at"] = _now()
    _write_json(root / "state.json", updated)
    return updated


def milestone_lines_from_state(state: dict[str, Any]) -> list[str]:
    return [
        _milestone_line(milestone)
        for milestone in state.get("milestones", [])
        if isinstance(milestone, dict)
    ]


def first_incomplete_milestone_index(state: dict[str, Any]) -> int:
    milestones = [item for item in state.get("milestones", []) if isinstance(item, dict)]
    for index, milestone in enumerate(milestones):
        if str(milestone.get("status", "pending")) not in COMPLETED_STATUSES:
            return index
    return len(milestones)


def load_milestone_preflight(root: Path, milestone_id: str) -> dict[str, Any]:
    return _read_json(root / "preflight" / f"{milestone_id}.json") or {}


def model_preflight_schema() -> dict[str, Any]:
    return json.loads(json.dumps(MODEL_PREFLIGHT_SCHEMA))


def stack_profile_for_contract(contract: PRDContract) -> dict[str, Any]:
    """Durable stack lock shared by every PRD milestone turn."""
    resolution = resolve_blueprints(contract)
    selected = {
        slot: blueprint.id
        for slot, blueprint in sorted(resolution.selected.items())
    }
    suggestions = {
        slot: blueprint.id
        for slot, blueprint in sorted(resolution.suggestions.items())
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "title": contract.title,
        "project_kind": contract.project_kind,
        "stack_hint": contract.stack_hint,
        "required_stack": list(contract.required_stack),
        "selected_blueprints": selected,
        "suggested_blueprints": suggestions,
        "backend": selected.get("backend", ""),
        "frontend": selected.get("frontend", ""),
        "database": selected.get("database", ""),
        "locked": True,
    }


def validate_model_preflight(
    deterministic: dict[str, Any],
    candidate: Any,
) -> tuple[dict[str, Any], list[str]]:
    """Validate and merge a model preflight without letting it rewrite ledger truth."""
    fallback = _with_preflight_meta(
        deterministic,
        source="deterministic_fallback",
        validation_errors=[],
    )
    errors: list[str] = []
    if not isinstance(candidate, dict):
        return fallback | {"validation_errors": ["model preflight was not a JSON object"]}, [
            "model preflight was not a JSON object"
        ]

    milestone_id = str(deterministic.get("milestone_id") or "")
    if str(candidate.get("milestone_id") or "") != milestone_id:
        errors.append("milestone_id did not match the compiled milestone")

    required_ids = _unique_strings(deterministic.get("requirement_ids") or [])
    candidate_ids = _unique_strings(candidate.get("requirement_ids") or [])
    if set(candidate_ids) != set(required_ids):
        errors.append("requirement_ids did not match the compiled ledger")

    base_skills = _unique_strings(deterministic.get("active_skills") or [])
    skills = _candidate_subset(candidate.get("active_skills"), base_skills, "active_skills", errors)
    if not skills:
        skills = base_skills
    elif "developer" in base_skills and "developer" not in skills:
        skills = ["developer", *skills]

    base_tools = _unique_strings(deterministic.get("allowed_tools") or [])
    tools = _candidate_subset(candidate.get("allowed_tools"), base_tools, "allowed_tools", errors)
    if not tools:
        tools = base_tools

    expected_files = _merged_expected_files(
        deterministic.get("expected_files") or [],
        candidate.get("expected_files") or [],
        errors,
    )

    if errors:
        return _with_preflight_meta(deterministic, source="deterministic_fallback", validation_errors=errors), errors

    effective = dict(deterministic)
    effective.update(
        {
            "preflight_source": "model",
            "requirement_ids": required_ids,
            "active_skills": skills,
            "expected_files": expected_files,
            "allowed_tools": tools,
            "verifier": _safe_model_text(candidate.get("verifier"), 160)
            or str(deterministic.get("verifier") or ""),
            "context_focus": _safe_text_list(candidate.get("context_focus"), limit=12, max_chars=160),
            "implementation_steps": _safe_text_list(
                candidate.get("implementation_steps"), limit=8, max_chars=180
            ),
            "risk_flags": _safe_text_list(candidate.get("risk_flags"), limit=8, max_chars=160),
            "blocker_question": _safe_model_text(candidate.get("blocker_question"), 240),
            "notes": _safe_model_text(candidate.get("notes"), 400),
            "validation_errors": [],
        }
    )
    return effective, []


def record_milestone_preflight(
    root: Path,
    state: dict[str, Any],
    milestone_id: str,
    preflight: dict[str, Any],
    *,
    validation_errors: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    source = str(preflight.get("preflight_source") or "deterministic")
    record = {
        "at": _now(),
        "milestone_id": milestone_id,
        "source": source,
        "accepted": source == "model",
        "validation_errors": _unique_strings(validation_errors),
        "active_skills": _unique_strings(preflight.get("active_skills") or []),
        "expected_files": _unique_strings(preflight.get("expected_files") or []),
        "allowed_tools": _unique_strings(preflight.get("allowed_tools") or []),
        "verifier": str(preflight.get("verifier") or ""),
        "context_focus": _unique_strings(preflight.get("context_focus") or []),
        "implementation_steps": _unique_strings(preflight.get("implementation_steps") or []),
        "risk_flags": _unique_strings(preflight.get("risk_flags") or []),
        "blocker_question": str(preflight.get("blocker_question") or "")[:240],
        "notes": str(preflight.get("notes") or "")[:400],
    }
    updated = dict(state)
    records = list(updated.get("preflight_decisions") or [])
    records.append(record)
    updated["preflight_decisions"] = records
    updated["updated_at"] = _now()
    _write_json(root / "state.json", updated)
    _write_json(root / "preflight" / f"{milestone_id}.effective.json", preflight)
    _append_jsonl(root / "preflight-decisions.jsonl", record)
    return updated


def _with_preflight_meta(
    preflight: dict[str, Any],
    *,
    source: str,
    validation_errors: list[str],
) -> dict[str, Any]:
    updated = dict(preflight)
    updated["preflight_source"] = source
    updated["validation_errors"] = list(validation_errors)
    return updated


def _candidate_subset(
    values: Any,
    allowed: list[str],
    field_name: str,
    errors: list[str],
) -> list[str]:
    if values is None:
        return []
    if not isinstance(values, list):
        errors.append(f"{field_name} was not a list")
        return []
    selected = _unique_strings(values)[:MAX_MODEL_PREFLIGHT_LIST_ITEMS]
    disallowed = [item for item in selected if item not in set(allowed)]
    if disallowed:
        errors.append(f"{field_name} contained values outside the compiled allowlist")
        return []
    return selected


def _merged_expected_files(base_values: Any, candidate_values: Any, errors: list[str]) -> list[str]:
    base = [_safe_relative_file(path) for path in _unique_strings(base_values or [])]
    base = [path for path in base if path]
    if not isinstance(candidate_values, list):
        errors.append("expected_files was not a list")
        return base
    candidate: list[str] = []
    for value in candidate_values[:MAX_MODEL_PREFLIGHT_LIST_ITEMS]:
        path = _safe_relative_file(str(value))
        if not path:
            errors.append("expected_files contained an unsafe relative path")
            return base
        candidate.append(path)
    return list(dict.fromkeys([*base, *candidate]))[:MAX_MODEL_PREFLIGHT_LIST_ITEMS]


def _safe_relative_file(path: str) -> str:
    cleaned = str(path or "").strip().replace("\\", "/")
    unsafe = set("\r\n;&|<>`$\"'")
    if not cleaned or any(char in unsafe or char.isspace() for char in cleaned):
        return ""
    if cleaned.startswith("/") or ":" in cleaned:
        return ""
    parts = cleaned.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return ""
    if cleaned.endswith("/"):
        return ""
    return cleaned


def _safe_text_list(values: Any, *, limit: int, max_chars: int) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values[:limit]:
        text = _safe_model_text(value, max_chars)
        if text:
            result.append(text)
    return list(dict.fromkeys(result))


def _safe_model_text(value: Any, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:max_chars]


def mark_milestone_running(root: Path, state: dict[str, Any], milestone_id: str) -> dict[str, Any]:
    updated = _with_milestone_status(state, milestone_id, "running")
    attempts = dict(updated.get("attempts") or {})
    attempts[milestone_id] = int(attempts.get(milestone_id) or 0) + 1
    updated["attempts"] = attempts
    updated["current_milestone_id"] = milestone_id
    updated["status"] = "running"
    updated["updated_at"] = _now()
    _write_json(root / "state.json", updated)
    return updated


def checkpoint_milestone(
    root: Path,
    state: dict[str, Any],
    milestone_id: str,
    *,
    changed_files: list[str] | tuple[str, ...] = (),
    evidence: list[str] | tuple[str, ...] = (),
    status: str = "implemented",
    message: str = "",
    verification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    verification_record = _milestone_verification_record(milestone_id, verification)
    updated = _with_milestone_status(
        state,
        milestone_id,
        status,
        changed_files=list(changed_files),
        evidence=list(evidence),
        message=message,
        verification=verification_record or None,
    )
    checkpoint = {
        "at": _now(),
        "milestone_id": milestone_id,
        "status": status,
        "changed_files": list(dict.fromkeys(str(path) for path in changed_files)),
        "evidence": list(evidence),
        "message": message[:500],
    }
    if verification_record:
        checkpoint["verification"] = verification_record
    checkpoints = list(updated.get("checkpoints") or [])
    checkpoints.append(checkpoint)
    updated["checkpoints"] = checkpoints
    if verification_record:
        verifications = list(updated.get("verifications") or [])
        verifications.append(verification_record)
        updated["verifications"] = verifications
    updated["changed_files"] = list(
        dict.fromkeys([*updated.get("changed_files", []), *checkpoint["changed_files"]])
    )
    if status == "failed":
        updated["current_milestone_id"] = milestone_id
        updated["status"] = "failed"
    else:
        next_index = first_incomplete_milestone_index(updated)
        milestones = [item for item in updated.get("milestones", []) if isinstance(item, dict)]
        updated["current_milestone_id"] = (
            str(milestones[next_index].get("id", "")) if next_index < len(milestones) else ""
        )
        updated["status"] = "complete" if not updated["current_milestone_id"] else "running"
    updated["updated_at"] = _now()
    _write_json(root / "state.json", updated)
    _append_jsonl(root / "checkpoints.jsonl", checkpoint)
    if verification_record:
        _append_jsonl(root / "verification.jsonl", verification_record)
    return updated


def record_milestone_repair(
    root: Path,
    state: dict[str, Any],
    milestone_id: str,
    *,
    attempt: int,
    phase: str,
    status: str,
    changed_files: list[str] | tuple[str, ...] = (),
    verification: dict[str, Any] | None = None,
    message: str = "",
) -> dict[str, Any]:
    """Record one bounded repair transition without finalizing the milestone."""
    repair_record = _milestone_repair_record(
        milestone_id,
        attempt=attempt,
        phase=phase,
        status=status,
        changed_files=changed_files,
        verification=verification,
        message=message,
    )
    verification_record = repair_record.get("verification")
    updated = _with_milestone_status(
        state,
        milestone_id,
        "repairing",
        changed_files=repair_record["changed_files"],
        message=message,
        verification=verification_record if isinstance(verification_record, dict) else None,
        repair=repair_record,
    )
    repairs = list(updated.get("repairs") or [])
    repairs.append(repair_record)
    updated["repairs"] = repairs
    updated["changed_files"] = list(
        dict.fromkeys([*updated.get("changed_files", []), *repair_record["changed_files"]])
    )
    updated["current_milestone_id"] = milestone_id
    updated["status"] = "running"
    updated["updated_at"] = _now()
    _write_json(root / "state.json", updated)
    _append_jsonl(root / "repairs.jsonl", repair_record)
    return updated


def record_milestone_rollback(
    root: Path,
    state: dict[str, Any],
    milestone_id: str,
    *,
    phase: str,
    status: str,
    transaction_ids: list[str] | tuple[str, ...] = (),
    restored_files: list[str] | tuple[str, ...] = (),
    failed_transactions: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    policy: str = "",
    message: str = "",
    preserved_changed_files: list[str] | tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Record automated milestone rollback activity in durable PRD state."""
    rollback_record = _milestone_rollback_record(
        milestone_id,
        phase=phase,
        status=status,
        transaction_ids=transaction_ids,
        restored_files=restored_files,
        failed_transactions=failed_transactions,
        policy=policy,
        message=message,
    )
    milestone_status = "rolling_back"
    if phase == "finished":
        milestone_status = "rolled_back" if status == "rolled_back" else "rollback_failed"
    updated = _with_milestone_status(
        state,
        milestone_id,
        milestone_status,
        message=message,
        rollback=rollback_record,
    )
    if preserved_changed_files is not None and status == "rolled_back":
        updated["changed_files"] = _unique_strings(preserved_changed_files)
        updated = _set_milestone_changed_files(updated, milestone_id, [])
    rollbacks = list(updated.get("rollbacks") or [])
    rollbacks.append(rollback_record)
    updated["rollbacks"] = rollbacks
    updated["current_milestone_id"] = milestone_id
    if phase != "finished" or status == "rolled_back":
        updated["status"] = "running"
    else:
        updated["status"] = "failed"
    updated["updated_at"] = _now()
    _write_json(root / "state.json", updated)
    _append_jsonl(root / "rollbacks.jsonl", rollback_record)
    return updated


def block_milestone(
    root: Path,
    state: dict[str, Any],
    milestone_id: str,
    reason: str,
) -> dict[str, Any]:
    updated = _with_milestone_status(state, milestone_id, "blocked", message=reason)
    blocker = {"at": _now(), "milestone_id": milestone_id, "reason": reason[:1000]}
    updated["blockers"] = [*list(updated.get("blockers") or []), blocker]
    updated["current_milestone_id"] = milestone_id
    updated["status"] = "blocked"
    updated["updated_at"] = _now()
    _write_json(root / "state.json", updated)
    _append_jsonl(root / "blockers.jsonl", blocker)
    return updated


def render_preflight_context(preflight: dict[str, Any]) -> str:
    if not preflight:
        return ""
    stack_profile = preflight.get("stack_profile") if isinstance(preflight, dict) else {}
    stack_line = ""
    if isinstance(stack_profile, dict) and stack_profile:
        selected = stack_profile.get("selected_blueprints") or {}
        if isinstance(selected, dict):
            stack_line = "; ".join(
                f"{slot}={value}"
                for slot, value in selected.items()
                if str(value).strip()
            )
        required_stack = ", ".join(str(item) for item in stack_profile.get("required_stack") or [])
        if required_stack:
            stack_line = f"{stack_line}; required_stack={required_stack}" if stack_line else f"required_stack={required_stack}"
    lines = [
        "## Milestone Preflight",
        f"Milestone: {preflight.get('milestone_id', '')} - {preflight.get('title', '')}",
        f"Project root: {preflight.get('project_root') or '.'}",
        f"Preflight source: {preflight.get('preflight_source') or 'deterministic'}",
        "Requirement IDs: " + ", ".join(preflight.get("requirement_ids") or []),
        "Active skills: " + ", ".join(preflight.get("active_skills") or []),
        "Expected files: " + ", ".join(preflight.get("expected_files") or []),
        f"Verifier: {preflight.get('verifier') or 'not selected'}",
        f"Attempt budget: {preflight.get('attempt_budget', 2)}",
        "",
        "Requirements for this milestone:",
    ]
    if stack_line:
        lines.insert(4, f"Stack lock: {stack_line}")
    for requirement in list(preflight.get("requirements") or [])[:12]:
        lines.append(f"- {requirement.get('id')}: {requirement.get('text')}")
    context_focus = list(preflight.get("context_focus") or [])[:8]
    if context_focus:
        lines.extend(["", "Context focus:"])
        lines.extend(f"- {item}" for item in context_focus)
    implementation_steps = list(preflight.get("implementation_steps") or [])[:8]
    if implementation_steps:
        lines.extend(["", "Implementation plan:"])
        lines.extend(f"{index}. {item}" for index, item in enumerate(implementation_steps, start=1))
    risk_flags = list(preflight.get("risk_flags") or [])[:6]
    if risk_flags:
        lines.extend(["", "Risk flags:"])
        lines.extend(f"- {item}" for item in risk_flags)
    blocker_question = str(preflight.get("blocker_question") or "").strip()
    if blocker_question:
        lines.extend(["", f"Potential blocker: {blocker_question}"])
    lines.extend(
        [
            "",
            "Preserve the stack lock across every file and command. Do not introduce a different backend, frontend, database, ORM, or verifier unless the PRD stack profile explicitly names it.",
            "Use only the context and tools needed for these requirement IDs.",
            "Checkpoint evidence must come from files changed, commands run, or a blocker.",
        ]
    )
    return "\n".join(lines)


def _write_preflights(
    root: Path,
    ledger: RequirementLedger,
    stack_profile: dict[str, Any],
) -> None:
    by_id = {record.id: record for record in ledger.requirements}
    preflight_dir = root / "preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    for milestone in ledger.milestones:
        requirements = [
            asdict(by_id[requirement_id])
            for requirement_id in milestone.requirement_ids
            if requirement_id in by_id
        ]
        payload = {
            "schema_version": SCHEMA_VERSION,
            "contract_hash": ledger.contract_hash,
            "stack_profile": dict(stack_profile),
            "milestone_id": milestone.id,
            "title": milestone.title,
            "requirement_ids": list(milestone.requirement_ids),
            "requirements": requirements,
            "dependencies": list(milestone.dependencies),
            "active_skills": list(milestone.active_skills),
            "expected_files": list(milestone.expected_files),
            "allowed_tools": sorted(_allowed_tools_for(milestone)),
            "verifier": milestone.verifier,
            "acceptance_conditions": list(milestone.acceptance_conditions),
            "attempt_budget": milestone.attempt_budget,
            "rollback_policy": milestone.rollback_policy,
            "status": "pending",
        }
        _write_json(preflight_dir / f"{milestone.id}.json", payload)


def _allowed_tools_for(milestone: MilestoneRecord) -> set[str]:
    tools = set(_BASE_ALLOWED_TOOLS)
    for skill in milestone.active_skills:
        tools.update(_SKILL_TOOL_HINTS.get(skill, set()))
    return tools


def _new_state(
    ledger: RequirementLedger,
    user_request: str,
    prd_path: str,
    stack_profile: dict[str, Any],
) -> dict[str, Any]:
    now = _now()
    return {
        "schema_version": SCHEMA_VERSION,
        "title": ledger.title,
        "contract_hash": ledger.contract_hash,
        "stack_profile": dict(stack_profile),
        "user_request": user_request,
        "prd_path": prd_path,
        "task_id": "",
        "status": "pending",
        "current_milestone_id": ledger.milestones[0].id if ledger.milestones else "",
        "created_at": now,
        "updated_at": now,
        "attempts": {},
        "blockers": [],
        "checkpoints": [],
        "preflight_decisions": [],
        "repairs": [],
        "rollbacks": [],
        "verifications": [],
        "changed_files": [],
        "milestones": [_milestone_state(milestone) for milestone in ledger.milestones],
    }


def _has_acceptance_criteria(ledger: RequirementLedger) -> bool:
    return any(
        record.kind == "acceptance" and record.scope == "in"
        for record in ledger.requirements
    )


def _block_missing_acceptance_criteria(state: dict[str, Any]) -> dict[str, Any]:
    summary = (
        "PRD execution is blocked because the acceptance matrix contains no "
        "criteria. Add explicit acceptance criteria before milestone verification."
    )
    updated = dict(state)
    updated["status"] = "blocked"
    updated["current_milestone_id"] = ""
    blockers = list(updated.get("blockers") or [])
    blocker = {
        "kind": "missing_acceptance_criteria",
        "summary": summary,
        "at": _now(),
    }
    if not any(item.get("kind") == blocker["kind"] for item in blockers if isinstance(item, dict)):
        blockers.append(blocker)
    updated["blockers"] = blockers
    milestones: list[dict[str, Any] | Any] = []
    for milestone in updated.get("milestones", []):
        if not isinstance(milestone, dict):
            milestones.append(milestone)
            continue
        if str(milestone.get("status") or "") in COMPLETED_STATUSES:
            milestones.append(milestone)
            continue
        milestones.append({**milestone, "status": "blocked", "last_message": summary})
    updated["milestones"] = milestones
    return updated


def _merge_state(
    existing: dict[str, Any],
    ledger: RequirementLedger,
    user_request: str,
    prd_path: str,
    stack_profile: dict[str, Any],
) -> dict[str, Any]:
    old_by_id = {
        str(item.get("id")): item
        for item in existing.get("milestones", [])
        if isinstance(item, dict)
    }
    milestones = []
    for milestone in ledger.milestones:
        item = _milestone_state(milestone)
        old = old_by_id.get(milestone.id)
        if old:
            item["status"] = str(old.get("status") or item["status"])
            item["last_evidence"] = list(old.get("last_evidence") or [])
            item["changed_files"] = list(old.get("changed_files") or [])
            if isinstance(old.get("last_verification"), dict):
                item["last_verification"] = dict(old["last_verification"])
            if isinstance(old.get("last_repair"), dict):
                item["last_repair"] = dict(old["last_repair"])
            if isinstance(old.get("last_rollback"), dict):
                item["last_rollback"] = dict(old["last_rollback"])
            if str(old.get("last_message") or ""):
                item["last_message"] = str(old["last_message"])
        milestones.append(item)
    updated = dict(existing)
    updated.update(
        {
            "schema_version": SCHEMA_VERSION,
            "title": ledger.title,
            "contract_hash": ledger.contract_hash,
            "stack_profile": dict(stack_profile),
            "user_request": user_request,
            "prd_path": prd_path,
            "milestones": milestones,
            "updated_at": _now(),
        }
    )
    if not str(updated.get("current_milestone_id") or ""):
        next_index = first_incomplete_milestone_index(updated)
        if next_index < len(milestones):
            updated["current_milestone_id"] = milestones[next_index]["id"]
    return updated


def _milestone_state(milestone: MilestoneRecord) -> dict[str, Any]:
    return {
        "id": milestone.id,
        "title": milestone.title,
        "status": milestone.status,
        "requirement_ids": list(milestone.requirement_ids),
        "dependencies": list(milestone.dependencies),
        "active_skills": list(milestone.active_skills),
        "expected_files": list(milestone.expected_files),
        "verifier": milestone.verifier,
        "last_evidence": [],
        "last_verification": {},
        "last_repair": {},
        "last_rollback": {},
        "changed_files": [],
    }


def _with_milestone_status(
    state: dict[str, Any],
    milestone_id: str,
    status: str,
    *,
    changed_files: list[str] | None = None,
    evidence: list[str] | None = None,
    message: str = "",
    verification: dict[str, Any] | None = None,
    repair: dict[str, Any] | None = None,
    rollback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updated = dict(state)
    milestones: list[dict[str, Any]] = []
    for item in state.get("milestones", []):
        if not isinstance(item, dict):
            continue
        milestone = dict(item)
        if milestone.get("id") == milestone_id:
            milestone["status"] = status
            milestone["updated_at"] = _now()
            if changed_files is not None:
                milestone["changed_files"] = list(
                    dict.fromkeys([*milestone.get("changed_files", []), *changed_files])
                )
            if evidence is not None:
                milestone["last_evidence"] = list(evidence)
            if message:
                milestone["last_message"] = message[:500]
            if verification:
                milestone["last_verification"] = dict(verification)
            if repair:
                milestone["last_repair"] = dict(repair)
            if rollback:
                milestone["last_rollback"] = dict(rollback)
        milestones.append(milestone)
    updated["milestones"] = milestones
    return updated


def _set_milestone_changed_files(
    state: dict[str, Any],
    milestone_id: str,
    changed_files: list[str] | tuple[str, ...],
) -> dict[str, Any]:
    updated = dict(state)
    milestones: list[dict[str, Any]] = []
    for item in state.get("milestones", []):
        if not isinstance(item, dict):
            continue
        milestone = dict(item)
        if milestone.get("id") == milestone_id:
            milestone["changed_files"] = _unique_strings(changed_files)
        milestones.append(milestone)
    updated["milestones"] = milestones
    return updated


def _milestone_verification_record(
    milestone_id: str,
    verification: dict[str, Any] | None,
) -> dict[str, Any]:
    if not verification:
        return {}
    status = str(verification.get("status") or "")
    return {
        "at": str(verification.get("at") or _now()),
        "milestone_id": milestone_id,
        "status": status,
        "verified": bool(verification.get("verified")),
        "unverifiable": bool(verification.get("unverifiable")),
        "exit_code": verification.get("exit_code"),
        "command": str(verification.get("command") or ""),
        "files": _unique_strings(verification.get("files") or []),
        "summary": str(verification.get("summary") or "")[:1000],
    }


def _milestone_repair_record(
    milestone_id: str,
    *,
    attempt: int,
    phase: str,
    status: str,
    changed_files: list[str] | tuple[str, ...],
    verification: dict[str, Any] | None,
    message: str,
) -> dict[str, Any]:
    try:
        attempt_number = max(1, int(attempt))
    except (TypeError, ValueError):
        attempt_number = 1
    record: dict[str, Any] = {
        "at": _now(),
        "milestone_id": milestone_id,
        "attempt": attempt_number,
        "phase": str(phase or "")[:40],
        "status": str(status or "")[:40],
        "changed_files": _unique_strings(changed_files),
        "message": str(message or "")[:1000],
    }
    verification_record = _milestone_verification_record(milestone_id, verification)
    if verification_record:
        record["verification"] = verification_record
    return record


def _milestone_rollback_record(
    milestone_id: str,
    *,
    phase: str,
    status: str,
    transaction_ids: list[str] | tuple[str, ...],
    restored_files: list[str] | tuple[str, ...],
    failed_transactions: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    policy: str,
    message: str,
) -> dict[str, Any]:
    failures: list[dict[str, Any]] = []
    for item in failed_transactions:
        if not isinstance(item, dict):
            continue
        failures.append(
            {
                "transaction_id": str(item.get("transaction_id") or "")[:80],
                "message": str(item.get("message") or "")[:500],
            }
        )
    return {
        "at": _now(),
        "milestone_id": milestone_id,
        "phase": str(phase or "")[:40],
        "status": str(status or "")[:40],
        "policy": str(policy or "")[:200],
        "transaction_ids": _unique_strings(transaction_ids),
        "restored_files": _unique_strings(restored_files),
        "failed_transactions": failures,
        "message": str(message or "")[:1000],
    }


def _unique_strings(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(value) for value in values if str(value)))


def _milestone_line(milestone: dict[str, Any]) -> str:
    requirement_ids = list(milestone.get("requirement_ids") or [])
    requirement_preview = ", ".join(str(item) for item in requirement_ids[:8])
    if len(requirement_ids) > 8:
        requirement_preview += f", +{len(requirement_ids) - 8} more"
    suffix = f" [{requirement_preview}]" if requirement_preview else ""
    return f"{milestone.get('id', '')}: {milestone.get('title', '')}{suffix}"


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
