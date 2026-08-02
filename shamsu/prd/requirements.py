"""Deterministic PRD requirement ledger compiler."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from shamsu.prd.contract import PRDContract

MAX_REQUIREMENTS_PER_MILESTONE = 4


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
    _extend(records, "ROLE", "role", contract.roles, "roles", contract)
    _extend(records, "AUTH", "auth", contract.authentication_rules, "authentication", contract)
    _extend(
        records,
        "AUTHZ",
        "authorization",
        contract.authorization_rules,
        "authorization",
        contract,
    )
    _extend(records, "FLOW", "workflow", contract.user_journeys, "user_journeys", contract)
    _extend(
        records, "API", "interface", _endpoint_requirements(contract), "api_endpoints", contract
    )
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
    _extend(records, "CONS", "constraint", contract.constraints, "constraints", contract)
    _extend(
        records,
        "SCOPE",
        "out_of_scope",
        contract.out_of_scope,
        "out_of_scope",
        contract,
        scope="out",
    )
    deduped = _dedupe_records(records)
    executable_records = [record for record in deduped if record.scope == "in"]
    milestones, milestone_assignment = _expanded_milestones(executable_records)
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
            milestone_id=milestone_assignment.get(record.id, ""),
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
            dependencies=[
                dependency for dependency in milestone.dependencies if dependency in by_id
            ],
            active_skills=list(milestone.active_skills),
            expected_files=sorted(
                dict.fromkeys(
                    [
                        *_architecture_expected_files_for_milestone(milestone.id, contract),
                        *_milestone_expected_files(milestone.id, assigned_records),
                    ]
                )
            ),
            verifier=milestone.verifier,
            acceptance_conditions=[
                record.id
                for record in assigned_records
                if record.milestone_id == milestone.id
                and record.scope == "in"
                and record.priority == "must"
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


def is_complex_prd_contract(
    contract: PRDContract,
    ledger: RequirementLedger | None = None,
) -> bool:
    """Return whether a PRD needs durable milestone execution."""
    compiled = ledger or compile_requirement_ledger(contract)
    if contract.requires_full_stack:
        return True
    return len(compiled.requirements) >= 6 and len(compiled.milestones) >= 2


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
        "components": _architecture_components(contract),
        "source_authoring": "react_tool_loop",
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
    *,
    scope: str = "in",
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
                scope=scope,
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
        if entity.get("inferred"):
            # Designed, not specified. Name the entity so it still gets built,
            # but do not emit the `: fields ...` form the validator enforces -
            # an invented field going missing must not fail a milestone.
            rows.append(f"{name}: entity with designed fields")
            continue
        rows.append(f"{name}: fields {', '.join(field for field in fields if field)}")
    return rows


def _endpoint_requirements(contract: PRDContract) -> list[str]:
    rows: list[str] = []
    for endpoint in contract.api_endpoints:
        method = str(endpoint.get("method") or "").upper().strip()
        path = str(endpoint.get("path") or "").strip()
        if not method or not path:
            continue
        access = "authenticated" if endpoint.get("auth_required", True) else "public"
        rows.append(f"{method} {path} ({access})")
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


def _expanded_milestones(
    records: list[RequirementRecord],
) -> tuple[list[MilestoneRecord], dict[str, str]]:
    base_milestones = _assign_milestones(records)
    records_by_base = {
        milestone.id: [record for record in records if _milestone_for(record) == milestone.id]
        for milestone in base_milestones
    }
    chunk_ids: dict[str, list[str]] = {}
    for milestone in base_milestones:
        chunk_count = max(
            1,
            (len(records_by_base[milestone.id]) + MAX_REQUIREMENTS_PER_MILESTONE - 1)
            // MAX_REQUIREMENTS_PER_MILESTONE,
        )
        base_number = int(milestone.id.removeprefix("M-"))
        chunk_ids[milestone.id] = [
            milestone.id,
            *[f"M-{base_number * 100 + chunk_index:03d}" for chunk_index in range(1, chunk_count)],
        ]

    expanded: list[MilestoneRecord] = []
    assignment: dict[str, str] = {}
    for milestone in base_milestones:
        chunks = [
            records_by_base[milestone.id][index : index + MAX_REQUIREMENTS_PER_MILESTONE]
            for index in range(
                0, len(records_by_base[milestone.id]), MAX_REQUIREMENTS_PER_MILESTONE
            )
        ]
        ids = chunk_ids[milestone.id]
        for index, (chunk_id, chunk) in enumerate(zip(ids, chunks, strict=True)):
            if index:
                dependencies = [ids[index - 1]]
            else:
                dependencies = [
                    chunk_ids[dependency][-1]
                    for dependency in milestone.dependencies
                    if dependency in chunk_ids
                ]
            title = milestone.title
            if len(chunks) > 1:
                title = f"{title} (part {index + 1}/{len(chunks)})"
            expanded.append(
                MilestoneRecord(
                    id=chunk_id,
                    title=title,
                    requirement_ids=[],
                    dependencies=dependencies,
                    active_skills=list(milestone.active_skills),
                    verifier=milestone.verifier,
                    attempt_budget=milestone.attempt_budget,
                    rollback_policy=milestone.rollback_policy,
                )
            )
            assignment.update({record.id: chunk_id for record in chunk})
    return expanded, assignment


def _milestone_for(record: RequirementRecord) -> str:
    if record.kind in {"entity", "role", "auth", "authorization", "security"}:
        return "M-001"
    if record.kind in {"feature", "mechanic", "screen", "workflow", "interface", "nonfunctional"}:
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
    # The requirement compiler does not own application architecture. Mapping
    # every React feature to App.tsx and every entity to src/data.ts caused
    # unrelated workflows to collapse into a generic dashboard. Expected files
    # are now supplied by the milestone agent after it inspects the project, or
    # inferred from files explicitly named by the PRD.
    del contract
    matches = re.findall(
        r"(?:^|[\s`'\"])([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+\.[A-Za-z0-9]+)",
        record.text,
    )
    return sorted(dict.fromkeys(path.replace("\\", "/") for path in matches))


def _architecture_components(contract: PRDContract) -> list[dict[str, Any]]:
    """Describe component ownership without generating framework source files."""
    stack = " ".join([contract.stack_hint, *contract.required_stack]).lower()
    components: list[dict[str, Any]] = []
    if any(token in stack for token in ("react", "vite", "vue", "svelte", "frontend")):
        components.append(
            {
                "id": "frontend",
                "root": "frontend",
                "required": True,
                "responsibility": "browser UI and API client",
            }
        )
    if any(token in stack for token in ("django", "fastapi", "flask", "express", "backend")):
        components.append(
            {
                "id": "backend",
                "root": "backend",
                "required": True,
                "responsibility": "API, authentication, and domain logic",
            }
        )
    if any(token in stack for token in ("sqlite", "postgres", "mysql", "database")):
        components.append(
            {
                "id": "database",
                "root": "backend" if any(item["id"] == "backend" for item in components) else ".",
                "required": True,
                "responsibility": "persistent application state owned by the backend",
            }
        )
    if not components:
        components.append(
            {
                "id": "application",
                "root": ".",
                "required": True,
                "responsibility": "primary application",
            }
        )
    return components


def _architecture_expected_files_for_milestone(
    milestone_id: str,
    contract: PRDContract,
) -> list[str]:
    """Give small models a framework file map without authoring source code."""
    stack = " ".join([contract.stack_hint, *contract.required_stack]).lower()
    number = int(milestone_id.removeprefix("M-") or 0)
    foundation = number == 1 or 100 <= number < 200
    product = number == 2 or 200 <= number < 300
    release = number in {3, 4} or 300 <= number < 500
    paths: list[str] = []
    if "django" in stack and (foundation or release):
        paths.extend(
            [
                "backend/manage.py",
                "backend/requirements.txt",
                "backend/config/__init__.py",
                "backend/config/settings.py",
                "backend/config/urls.py",
                "backend/core/__init__.py",
                "backend/core/apps.py",
                "backend/core/models.py",
                "backend/core/migrations/__init__.py",
            ]
        )
    if any(token in stack for token in ("react", "vite")) and (product or release):
        typed = "typescript" in stack or "tsx" in stack
        extension = "tsx" if typed else "jsx"
        paths.extend(
            [
                "frontend/package.json",
                "frontend/index.html",
                f"frontend/src/main.{extension}",
                f"frontend/src/App.{extension}",
                "frontend/src/styles.css",
            ]
        )
    return paths


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
