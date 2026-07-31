"""Deterministic PRD requirement ledger compiler."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from shamsu.prd.contract import PRDContract


@dataclass(frozen=True)
class RequirementRecord:
    id: str
    kind: str
    text: str
    source: str
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    priority: str = "must"
    scope: str = "in"
    status: str = "pending"
    verification: str = ""
    milestone_id: str = ""
    implementing_files: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class MilestoneRecord:
    id: str
    title: str
    requirement_ids: list[str]
    dependencies: list[str] = field(default_factory=list)
    active_skills: list[str] = field(default_factory=list)
    expected_files: list[str] = field(default_factory=list)
    verifier: str = ""
    acceptance_conditions: list[str] = field(default_factory=list)
    attempt_budget: int = 2
    rollback_policy: str = "rollback changed files on failed verifier"
    status: str = "pending"


@dataclass(frozen=True)
class RequirementLedger:
    schema_version: int
    title: str
    contract_hash: str
    requirements: list[RequirementRecord]
    milestones: list[MilestoneRecord]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PRDExecutionArtifacts:
    """File set consumed by future milestone execution and audits."""

    requirement_ledger: RequirementLedger
    architecture: dict[str, Any]
    acceptance_matrix: dict[str, Any]
    decisions: list[dict[str, Any]]
    progress: dict[str, Any]


def compile_requirement_ledger(contract: PRDContract) -> RequirementLedger:
    records: list[RequirementRecord] = []
    _extend(records, "FEAT", "feature", contract.features, "features", contract)
    _extend(records, "MECH", "mechanic", contract.mechanics, "mechanics", contract)
    _extend(records, "SCREEN", "screen", contract.screens, "screens", contract)
    _extend(records, "DATA", "entity", _entity_requirements(contract), "entities", contract)
    _extend(records, "AUTH", "auth", contract.authentication_rules, "authentication", contract)
    _extend(
        records,
        "PERSIST",
        "persistence",
        contract.persistence_requirements,
        "persistence",
        contract,
    )
    _extend(records, "SEC", "security", contract.security_requirements, "security", contract)
    _extend(
        records,
        "NFR",
        "nonfunctional",
        contract.nonfunctional_requirements,
        "nonfunctional",
        contract,
    )
    _extend(records, "TEST", "test", contract.required_tests, "required_tests", contract)
    _extend(records, "ACC", "acceptance", contract.acceptance_criteria, "acceptance", contract)
    deduped = _dedupe_records(records)
    milestones = _assign_milestones(deduped)
    by_id = {milestone.id: milestone for milestone in milestones}
    assigned_records = [
        RequirementRecord(
            id=record.id,
            kind=record.kind,
            text=record.text,
            source=record.source,
            source_refs=list(record.source_refs),
            priority=record.priority,
            scope=record.scope,
            status=record.status,
            verification=record.verification or _verification_for(record),
            milestone_id=_milestone_for(record),
            implementing_files=_expected_files_for(record, contract),
            evidence=list(record.evidence),
        )
        for record in deduped
    ]
    milestones = [
        MilestoneRecord(
            id=milestone.id,
            title=milestone.title,
            requirement_ids=[
                record.id for record in assigned_records if record.milestone_id == milestone.id
            ],
            dependencies=list(milestone.dependencies),
            active_skills=list(milestone.active_skills),
            expected_files=_milestone_expected_files(milestone.id, assigned_records),
            verifier=milestone.verifier,
            acceptance_conditions=[
                record.id
                for record in assigned_records
                if record.milestone_id == milestone.id and record.kind == "acceptance"
            ],
            attempt_budget=milestone.attempt_budget,
            rollback_policy=milestone.rollback_policy,
            status=milestone.status,
        )
        for milestone in by_id.values()
    ]
    return RequirementLedger(
        schema_version=1,
        title=contract.title,
        contract_hash=_hash(contract.to_dict()),
        requirements=assigned_records,
        milestones=[milestone for milestone in milestones if milestone.requirement_ids],
    )


def compile_prd_execution_artifacts(contract: PRDContract) -> PRDExecutionArtifacts:
    requirement_ledger = compile_requirement_ledger(contract)
    architecture = {
        "schema_version": 1,
        "title": contract.title,
        "contract_hash": requirement_ledger.contract_hash,
        "project_kind": contract.project_kind,
        "stack_hint": contract.stack_hint,
        "required_stack": list(contract.required_stack),
        "architecture": list(contract.architecture),
        "assumptions": list(contract.assumptions),
        "warnings": list(contract.extraction_warnings),
    }
    acceptance_records = [
        {
            "requirement_id": record.id,
            "milestone_id": record.milestone_id,
            "criterion": record.text,
            "verification": record.verification,
            "status": record.status,
            "evidence": list(record.evidence),
        }
        for record in requirement_ledger.requirements
        if record.kind == "acceptance"
    ]
    acceptance_matrix = {
        "schema_version": 1,
        "title": contract.title,
        "contract_hash": requirement_ledger.contract_hash,
        "criteria": acceptance_records,
        "unmapped": [],
    }
    decisions = _decision_records(contract)
    first_pending = requirement_ledger.milestones[0].id if requirement_ledger.milestones else ""
    progress = {
        "schema_version": 1,
        "title": contract.title,
        "contract_hash": requirement_ledger.contract_hash,
        "status": "pending",
        "current_milestone_id": first_pending,
        "attempts": {},
        "blockers": [],
        "checkpoints": [],
        "milestones": [
            {
                "id": milestone.id,
                "status": milestone.status,
                "requirement_ids": list(milestone.requirement_ids),
                "verifier": milestone.verifier,
            }
            for milestone in requirement_ledger.milestones
        ],
    }
    return PRDExecutionArtifacts(
        requirement_ledger=requirement_ledger,
        architecture=architecture,
        acceptance_matrix=acceptance_matrix,
        decisions=decisions,
        progress=progress,
    )


def save_requirement_ledger(ledger: RequirementLedger, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(ledger.to_dict(), indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )
    return path


def save_prd_execution_artifacts(contract: PRDContract, directory: Path) -> dict[str, str]:
    artifacts = compile_prd_execution_artifacts(contract)
    directory.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}
    paths["prd_requirements"] = _write_json(
        directory / "prd-requirements.json",
        artifacts.requirement_ledger.to_dict(),
    )
    paths["requirements"] = _write_jsonl(
        directory / "requirements.jsonl",
        [record.__dict__ for record in artifacts.requirement_ledger.requirements],
    )
    paths["milestones"] = _write_json(
        directory / "milestones.json",
        {
            "schema_version": 1,
            "title": artifacts.requirement_ledger.title,
            "contract_hash": artifacts.requirement_ledger.contract_hash,
            "milestones": [
                milestone.__dict__ for milestone in artifacts.requirement_ledger.milestones
            ],
        },
    )
    paths["architecture"] = _write_json(directory / "architecture.json", artifacts.architecture)
    paths["acceptance_matrix"] = _write_json(
        directory / "acceptance-matrix.json",
        artifacts.acceptance_matrix,
    )
    paths["decisions"] = _write_jsonl(directory / "decisions.jsonl", artifacts.decisions)
    paths["progress"] = _write_json(directory / "progress.json", artifacts.progress)
    return {key: Path(value).name for key, value in paths.items()}


def render_requirement_summary(ledger: RequirementLedger, limit: int = 20) -> str:
    lines = [
        f"Requirement ledger: {ledger.title or 'Untitled'}",
        f"Requirements: {len(ledger.requirements)}",
        f"Milestones: {len(ledger.milestones)}",
    ]
    for milestone in ledger.milestones:
        lines.append(f"- {milestone.id}: {milestone.title} ({len(milestone.requirement_ids)} reqs)")
    if ledger.requirements:
        lines.append("Top requirements:")
        for record in ledger.requirements[:limit]:
            lines.append(f"- {record.id} [{record.kind}] {record.text}")
    return "\n".join(lines)


def _extend(
    records: list[RequirementRecord],
    prefix: str,
    kind: str,
    items: list[str],
    source: str,
    contract: PRDContract,
) -> None:
    for index, item in enumerate(items, start=1):
        text = str(item).strip()
        if not text:
            continue
        records.append(
            RequirementRecord(
                id=f"{prefix}-{index:03d}",
                kind=kind,
                text=text,
                source=source,
                source_refs=_source_refs_for(contract, source),
                verification=_verification_for_text(kind, text),
            )
        )


def _entity_requirements(contract: PRDContract) -> list[str]:
    rows: list[str] = []
    for entity in contract.entities:
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        fields = [
            str(field.get("name") if isinstance(field, dict) else field)
            for field in list(entity.get("fields") or [])
        ]
        rows.append(f"{name}: fields {', '.join(field for field in fields if field)}")
    return rows


def _dedupe_records(records: list[RequirementRecord]) -> list[RequirementRecord]:
    seen: set[tuple[str, str]] = set()
    result: list[RequirementRecord] = []
    counters: dict[str, int] = {}
    for record in records:
        key = (record.kind, " ".join(record.text.lower().split()))
        if key in seen:
            continue
        seen.add(key)
        prefix = record.id.split("-", 1)[0]
        counters[prefix] = counters.get(prefix, 0) + 1
        result.append(
            RequirementRecord(
                id=f"{prefix}-{counters[prefix]:03d}",
                kind=record.kind,
                text=record.text,
                source=record.source,
                source_refs=list(record.source_refs),
                priority=record.priority,
                scope=record.scope,
                verification=record.verification,
            )
        )
    return result


def _assign_milestones(records: list[RequirementRecord]) -> list[MilestoneRecord]:
    needed = {_milestone_for(record) for record in records}
    order = [
        MilestoneRecord(
            "M-001",
            "Foundation and data model",
            [],
            active_skills=["developer", "prd-planner", "sqlite-persistence"],
            verifier="schema/data checks",
        ),
        MilestoneRecord(
            "M-002",
            "Product workflows and UI",
            [],
            dependencies=["M-001"],
            active_skills=["developer", "react-vite", "ui-designer"],
            verifier="focused app tests/build",
        ),
        MilestoneRecord(
            "M-003",
            "Persistence, scripts, and integrations",
            [],
            dependencies=["M-001"],
            active_skills=["developer", "sqlite-persistence", "mcp-tools"],
            verifier="seed/status checks",
        ),
        MilestoneRecord(
            "M-004",
            "Acceptance and release verification",
            [],
            dependencies=["M-001", "M-002", "M-003"],
            active_skills=["developer", "testing"],
            verifier="acceptance commands",
        ),
    ]
    return [milestone for milestone in order if milestone.id in needed]


def _milestone_for(record: RequirementRecord) -> str:
    if record.kind in {"entity", "auth", "security"}:
        return "M-001"
    if record.kind in {"feature", "mechanic", "screen", "nonfunctional"}:
        return "M-002"
    if record.kind in {"persistence"} or _mentions_script_or_seed(record.text):
        return "M-003"
    return "M-004"


def _verification_for(record: RequirementRecord) -> str:
    return record.verification or _verification_for_text(record.kind, record.text)


def _verification_for_text(kind: str, text: str) -> str:
    lowered = text.lower()
    if "`" in text and any(word in lowered for word in ("prints", "exits", "exit")):
        return "run acceptance command"
    if kind == "test":
        return "run named test command"
    if kind in {"entity", "persistence"} or _mentions_script_or_seed(text):
        return "inspect generated files and run persistence/script checks"
    if kind in {"screen", "feature", "mechanic"}:
        return "inspect implementation and run app tests/build"
    return "inspect implementation evidence"


def _expected_files_for(record: RequirementRecord, contract: PRDContract) -> list[str]:
    lowered = record.text.lower()
    files: list[str] = []
    if _is_react_stack(contract):
        if record.kind in {"feature", "mechanic", "screen", "nonfunctional"}:
            files.extend(["src/App.tsx", "src/styles.css"])
        if record.kind in {"entity", "persistence"}:
            files.extend(["src/data.ts", "scripts/seed.mjs", "scripts/status.mjs"])
        if record.kind in {"test", "acceptance"}:
            files.extend(["src/app.test.ts", "package.json"])
        if "seed" in lowered:
            files.append("scripts/seed.mjs")
        if "status" in lowered:
            files.append("scripts/status.mjs")
    elif "python" in " ".join(contract.required_stack).lower() or contract.stack_hint == "python":
        if record.kind in {"feature", "mechanic", "acceptance", "test"}:
            files.append(_python_entrypoint(contract))
    return sorted(dict.fromkeys(files))


def _milestone_expected_files(
    milestone_id: str,
    records: list[RequirementRecord],
) -> list[str]:
    files = [
        path
        for record in records
        if record.milestone_id == milestone_id
        for path in record.implementing_files
    ]
    return sorted(dict.fromkeys(files))


def _is_react_stack(contract: PRDContract) -> bool:
    text = " ".join([contract.stack_hint, *contract.required_stack]).lower()
    return "react" in text or "vite" in text or contract.project_kind == "web_app"


def _python_entrypoint(contract: PRDContract) -> str:
    title = (contract.title or "app").lower()
    if "ledgerlite" in title or "expense" in title:
        return "ledgerlite.py"
    return "app.py"


def _source_refs_for(contract: PRDContract, source: str) -> list[dict[str, Any]]:
    groups = {
        "features": ("feature", "functional", "workflow", "browser ui", "script", "demo data"),
        "mechanics": ("mechanic", "gameplay", "functional"),
        "screens": ("screen", "page", "browser ui"),
        "entities": ("entity", "data model", "database schema"),
        "authentication": ("auth", "login", "logout", "registration"),
        "persistence": ("database", "sqlite", "persistence", "seed", "backup"),
        "security": ("security", "password", "authorization", "secret"),
        "nonfunctional": ("accessibility", "responsive", "performance", "logging"),
        "required_tests": ("test", "testing", "acceptance"),
        "acceptance": ("acceptance",),
    }
    needles = groups.get(source, (source,))
    refs: list[dict[str, Any]] = []
    for section, section_refs in contract.source_refs.items():
        lowered = section.lower()
        if any(needle in lowered for needle in needles):
            refs.extend(dict(ref) for ref in section_refs[:3])
    return refs[:5]


def _decision_records(contract: PRDContract) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for index, assumption in enumerate(contract.assumptions, start=1):
        records.append(
            {
                "id": f"DEC-{index:03d}",
                "status": "assumption",
                "source": "contract.assumptions",
                "decision": assumption,
                "evidence": [],
            }
        )
    offset = len(records)
    for index, warning in enumerate(contract.extraction_warnings, start=1):
        records.append(
            {
                "id": f"DEC-{offset + index:03d}",
                "status": "open",
                "source": "contract.extraction_warnings",
                "decision": warning,
                "evidence": [],
            }
        )
    return records


def _mentions_script_or_seed(text: str) -> bool:
    lowered = text.lower()
    return "script" in lowered or "seed" in lowered or ".mjs" in lowered or "cli" in lowered


def _hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> str:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return str(path)


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> str:
    lines = [json.dumps(record, ensure_ascii=True, sort_keys=True) for record in records]
    path.write_text(("\n".join(lines) + "\n") if lines else "", encoding="utf-8")
    return str(path)
