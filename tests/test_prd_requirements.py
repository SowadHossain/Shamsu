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
    MAX_REQUIREMENTS_PER_MILESTONE,
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
    assert all(record.milestone_id for record in ledger.requirements if record.scope == "in")
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


def test_requirement_ledger_includes_cross_cutting_project_contract():
    contract = extract_contract(
        parse_prd_text(
            "PRD: Orbit Desk\n"
            "1. Overview\nA full-stack web application.\n"
            "4. Users & Roles\n• Admin: manages workspaces.\n"
            "6. Data Model\nWorkspace\n• id, name, owner_id (FK → User)\n"
            "7.2 Admin Flows\nCreate a workspace.\n"
            "8. API Surface\nPOST\n/api/workspaces\n"
            "9. Permissions Rules\nOnly admins create workspaces.\n"
            "10. Acceptance Criteria\nAn admin can create a workspace.\n"
            "11. Non-Goals\nRealtime collaboration.\n"
        )
    )

    ledger = compile_requirement_ledger(contract)
    kinds = {record.kind for record in ledger.requirements}
    out_of_scope = [record for record in ledger.requirements if record.kind == "out_of_scope"]

    assert {"entity", "role", "workflow", "interface", "authorization", "acceptance"} <= kinds
    assert out_of_scope and out_of_scope[0].scope == "out"
    assert all(not record.milestone_id for record in out_of_scope)
    milestone_ids = {milestone.id for milestone in ledger.milestones}
    assert all(
        dependency in milestone_ids
        for milestone in ledger.milestones
        for dependency in milestone.dependencies
    )


def test_oversized_milestones_are_chained_into_small_model_sized_capsules():
    contract = extract_contract(
        parse_prd_text(
            "# Workflow Suite\n\n## Features\n"
            + "\n".join(f"- Implement workflow {index}." for index in range(1, 30))
            + "\n\n## Acceptance\n- The production build passes.\n",
            markdown=True,
        )
    )

    ledger = compile_requirement_ledger(contract)
    workflow_milestones = [
        milestone
        for milestone in ledger.milestones
        if milestone.id == "M-002" or milestone.id.startswith("M-2")
    ]

    assert len(workflow_milestones) == 8
    assert all(
        len(milestone.requirement_ids) <= MAX_REQUIREMENTS_PER_MILESTONE
        for milestone in ledger.milestones
    )
    assert workflow_milestones[1].dependencies == [workflow_milestones[0].id]
    assert workflow_milestones[2].dependencies == [workflow_milestones[1].id]


def test_compiled_milestones_have_binding_conditions_without_generic_react_file_guesses():
    contract = extract_contract(
        parse_prd_text(
            "# Canvas Lite\n\n"
            "## Tech Stack\n- Django\n- React\n- SQLite\n\n"
            "## Data Model\nCourse\n- id, title\n\n"
            "## Features\n- A teacher creates a course.\n- A student submits an assignment.\n",
            markdown=True,
        )
    )

    ledger = compile_requirement_ledger(contract)

    assert ledger.milestones
    assert all(milestone.acceptance_conditions for milestone in ledger.milestones)
    assert all(not record.implementing_files for record in ledger.requirements)
    foundation = next(milestone for milestone in ledger.milestones if milestone.id == "M-001")
    product = next(milestone for milestone in ledger.milestones if milestone.id == "M-002")
    assert "backend/manage.py" in foundation.expected_files
    assert "backend/core/models.py" in foundation.expected_files
    assert "frontend/package.json" in product.expected_files
    assert "frontend/src/App.jsx" in product.expected_files


def test_architecture_artifact_declares_component_ownership_and_react_authoring():
    contract = extract_contract(
        parse_prd_text(
            "# Canvas Lite\n\n## Tech Stack\n- Django\n- React\n- SQLite\n",
            markdown=True,
        )
    )

    architecture = compile_prd_execution_artifacts(contract).architecture

    assert architecture["source_authoring"] == "react_tool_loop"
    assert {item["id"] for item in architecture["components"]} == {
        "frontend",
        "backend",
        "database",
    }


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


def test_milestone_skills_do_not_invent_react_or_sqlite_for_unspecified_stack():
    contract = extract_contract(
        parse_prd_text(
            "# Studio Ops\n\n"
            "## Overview\nA full-stack web application for managing orders.\n\n"
            "## Data Model\nOrder\n- id, status\n\n"
            "## Features\n- Staff update an order status from a browser screen.\n\n"
            "## Persistence Requirements\n- Orders are stored durably.\n\n"
            "## Acceptance Criteria\n- Staff can see the updated status after reload.\n",
            markdown=True,
        )
    )

    ledger = compile_requirement_ledger(contract)
    all_skills = {
        skill
        for milestone in ledger.milestones
        for skill in milestone.active_skills
    }

    assert "react-vite" not in all_skills
    assert "sqlite-persistence" not in all_skills


def test_postgres_milestones_use_sql_databases_not_sqlite_persistence():
    contract = extract_contract(
        parse_prd_text(
            "# Market API\n\n"
            "## Tech Stack\n- Node with Express\n- React with Vite\n- PostgreSQL 16\n\n"
            "## Data Model\nListing\n- id, title, price\n\n"
            "## Features\n- Buyers browse listings.\n\n"
            "## Persistence Requirements\n- All data lives in PostgreSQL.\n"
            "- Seed demo listings with a script.\n\n"
            "## Acceptance Criteria\n- `npm test` exits 0.\n",
            markdown=True,
        )
    )

    ledger = compile_requirement_ledger(contract)
    all_skills = {
        skill
        for milestone in ledger.milestones
        for skill in milestone.active_skills
    }

    assert "sql-databases" in all_skills
    assert "sqlite-persistence" not in all_skills
    assert "react-vite" in all_skills


def test_dockerized_postgres_full_stack_files_are_foundation_targets():
    contract = extract_contract(
        parse_prd_text(
            "# OpenBazaar\n\n"
            "## Tech Stack\n"
            "- Django backend\n"
            "- React and Vite frontend\n"
            "- PostgreSQL 16\n"
            "- Docker Compose\n\n"
            "## Data Model\n"
            "Item\n"
            "- id, title\n\n"
            "## Features\n"
            "- Buyers browse marketplace listings.\n\n"
            "## Acceptance\n"
            "- `docker compose config -q` exits 0.\n",
            markdown=True,
        )
    )

    artifacts = compile_prd_execution_artifacts(contract)
    foundation = next(
        milestone for milestone in artifacts.requirement_ledger.milestones
        if milestone.id == "M-001"
    )
    runtime = artifacts.architecture["runtime"]

    assert runtime["compose"]["services"] == ["postgres", "backend", "frontend"]
    assert {
        "docker-compose.yml",
        ".env.example",
        "backend/Dockerfile",
        "backend/.env.example",
        "backend/requirements.txt",
        "frontend/Dockerfile",
        "frontend/.env.example",
        "frontend/package.json",
        "frontend/vite.config.ts",
        "frontend/index.html",
        "frontend/src/main.jsx",
        "frontend/src/App.jsx",
        "frontend/src/styles.css",
    }.issubset(set(foundation.expected_files))
    assert "sqlite-persistence" not in foundation.active_skills
    assert "sql-databases" in foundation.active_skills


def test_openbazaar_web_only_stack_gets_frontend_shell_even_without_react_word():
    contract = extract_contract(
        parse_prd_text(
            "# OpenBazaar: Web-Only Cash on Delivery Marketplace\n\n"
            "## Overview\n"
            "A web-only marketplace for buyers and sellers.\n\n"
            "## Features\n"
            "- Buyers browse listings from a responsive browser UI.\n\n"
            "## Responsive Design Requirements\n"
            "- The marketplace must work on mobile and desktop screens.\n\n"
            "## Technical Architecture & Database Design\n"
            "- Node.js backend API.\n"
            "- PostgreSQL 16 database.\n"
            "- Docker Compose runs postgres, backend, and frontend services.\n\n"
            "## Data Model\n"
            "Listing\n"
            "- id, title, price\n\n"
            "## Acceptance Criteria\n"
            "- `npm test` exits 0.\n",
            markdown=True,
        )
    )

    artifacts = compile_prd_execution_artifacts(contract)
    components = {component["id"] for component in artifacts.architecture["components"]}
    foundation = next(
        milestone for milestone in artifacts.requirement_ledger.milestones
        if milestone.id == "M-001"
    )
    product = next(
        milestone for milestone in artifacts.requirement_ledger.milestones
        if milestone.id == "M-002"
    )

    assert {"backend", "frontend", "database"} <= components
    assert {
        "docker-compose.yml",
        "backend/Dockerfile",
        "backend/package.json",
        "backend/server.js",
        "frontend/Dockerfile",
        "frontend/package.json",
        "frontend/index.html",
        "frontend/vite.config.ts",
        "frontend/src/main.jsx",
        "frontend/src/App.jsx",
        "frontend/src/styles.css",
    }.issubset(set(foundation.expected_files))
    assert "react-vite" in product.active_skills


def test_selected_node_backend_blueprint_declares_backend_source_files_with_react_present():
    contract = extract_contract(
        parse_prd_text(
            "# OpenBazaar\n\n"
            "## Tech Stack\n"
            "- Node.js with Express backend\n"
            "- React and Vite frontend\n"
            "- PostgreSQL 16\n\n"
            "## Data Model\n"
            "Listing\n"
            "- id, title, price\n\n"
            "## Features\n"
            "- Buyers browse listings.\n\n"
            "## Acceptance\n"
            "- `docker compose config -q` exits 0.\n",
            markdown=True,
        )
    )

    artifacts = compile_prd_execution_artifacts(contract)
    foundation = next(
        milestone for milestone in artifacts.requirement_ledger.milestones
        if milestone.id == "M-001"
    )

    assert "backend/server.js" in foundation.expected_files
    assert "backend/src/app.js" in foundation.expected_files
    assert "backend/src/db.js" in foundation.expected_files
    assert "backend/src/schema.sql" in foundation.expected_files


def test_node_react_postgres_stack_infers_node_backend_for_data_prd():
    contract = extract_contract(
        parse_prd_text(
            "# OpenBazaar\n\n"
            "## Tech Stack\n"
            "- Node.js\n"
            "- React and Vite\n"
            "- PostgreSQL 16\n\n"
            "## Data Model\n"
            "Listing\n"
            "- id, title, price\n\n"
            "## Features\n"
            "- Buyers browse listings.\n\n"
            "## Acceptance\n"
            "- `docker compose config -q` exits 0.\n",
            markdown=True,
        )
    )

    artifacts = compile_prd_execution_artifacts(contract)
    foundation = next(
        milestone for milestone in artifacts.requirement_ledger.milestones
        if milestone.id == "M-001"
    )

    assert "backend/server.js" in foundation.expected_files
    assert "backend/src/app.js" in foundation.expected_files
    assert "backend/src/schema.sql" in foundation.expected_files
    assert "frontend/package.json" in foundation.expected_files


def test_django_product_milestone_declares_server_rendered_ui_files():
    contract = extract_contract(
        parse_prd_text(
            "# Course Desk\n\n"
            "## Tech Stack\n- Django\n- SQLite\n\n"
            "## Data Model\nCourse\n- id, title\n\n"
            "## Features\n- Teachers create courses from the browser.\n"
            "- Students view assigned courses.\n\n"
            "## Acceptance Criteria\n- A teacher can create a course.\n",
            markdown=True,
        )
    )

    ledger = compile_requirement_ledger(contract)
    product = next(milestone for milestone in ledger.milestones if milestone.id == "M-002")

    assert "backend/core/views.py" in product.expected_files
    assert "backend/core/urls.py" in product.expected_files
    assert "backend/core/templates/dashboard.html" in product.expected_files
    assert "react-vite" not in product.active_skills
