from __future__ import annotations

import json
from pathlib import Path

from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.cli import repl
from shamsu.prd.contract import extract_contract
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.prd.requirements import (
    compile_prd_execution_artifacts,
    compile_requirement_ledger,
    render_requirement_summary,
    save_prd_execution_artifacts,
)


def test_compile_requirement_ledger_assigns_stable_ids_and_milestones():
    prd_text = Path("evals/fixtures/prds/atlasdesk_long.md").read_text(encoding="utf-8")
    contract = extract_contract(parse_prd_text(prd_text, markdown=True))

    ledger = compile_requirement_ledger(contract)

    assert ledger.schema_version == 1
    assert any(record.id == "ACC-001" for record in ledger.requirements)
    assert any("scripts/seed.mjs" in record.text for record in ledger.requirements)
    assert any(record.verification == "run acceptance command" for record in ledger.requirements)
    assert {"M-001", "M-002", "M-003", "M-004"} <= {milestone.id for milestone in ledger.milestones}
    assert all(record.milestone_id for record in ledger.requirements)
    assert any(record.implementing_files for record in ledger.requirements)
    assert any(milestone.active_skills for milestone in ledger.milestones)


def test_requirement_summary_is_compact_and_auditable():
    contract = extract_contract(
        parse_prd_text(
            "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
            markdown=True,
        )
    )
    ledger = compile_requirement_ledger(contract)

    summary = render_requirement_summary(ledger)

    assert "Requirement ledger: Demo" in summary
    assert "FEAT-001" in summary
    assert "ACC-001" in summary


def test_prd_contract_logging_writes_requirement_artifact(tmp_path: Path):
    prd = tmp_path / "PRD.md"
    prd.write_text(
        "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
        encoding="utf-8",
    )
    project = build_project_spec(parse_prd_text(prd.read_text(encoding="utf-8"), markdown=True))
    ledger = start_run(tmp_path, "build from PRD")
    set_current_run(ledger)
    try:
        repl._log_prd_contract_summary(project)
    finally:
        clear_current_run()

    path = ledger.run_dir / "prd-requirements.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert any(record["id"] == "ACC-001" for record in payload["requirements"])
    assert "prd_requirement_ledger_compiled" in ledger.events_path.read_text(encoding="utf-8")


def test_prd_execution_artifacts_are_written_as_separate_files(tmp_path: Path):
    contract = extract_contract(
        parse_prd_text(
            "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
            markdown=True,
        )
    )

    paths = save_prd_execution_artifacts(contract, tmp_path)

    assert paths == {
        "prd_requirements": "prd-requirements.json",
        "requirements": "requirements.jsonl",
        "milestones": "milestones.json",
        "architecture": "architecture.json",
        "acceptance_matrix": "acceptance-matrix.json",
        "decisions": "decisions.jsonl",
        "progress": "progress.json",
    }
    assert (tmp_path / "requirements.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads((tmp_path / "milestones.json").read_text(encoding="utf-8"))["milestones"]
    matrix = json.loads((tmp_path / "acceptance-matrix.json").read_text(encoding="utf-8"))
    assert matrix["criteria"][0]["requirement_id"] == "ACC-001"
    progress = json.loads((tmp_path / "progress.json").read_text(encoding="utf-8"))
    assert progress["current_milestone_id"]


def test_prd_execution_artifacts_are_stable_for_same_contract():
    contract = extract_contract(
        parse_prd_text(
            "# Demo\n\n## Features\n- Search tasks\n\n## Acceptance\n- `npm test` exits 0.\n",
            markdown=True,
        )
    )

    first = compile_prd_execution_artifacts(contract)
    second = compile_prd_execution_artifacts(contract)

    assert first.requirement_ledger.to_dict() == second.requirement_ledger.to_dict()
    assert first.acceptance_matrix == second.acceptance_matrix
