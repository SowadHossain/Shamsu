from __future__ import annotations

from shamsu.prd.contract import PRDContract
from shamsu.prd.requirements import _architecture_expected_files_for_milestone
from shamsu.registry.blueprints import (
    blueprint_by_id,
    resolve_blueprints,
    runtime_file_paths_for_contract,
    runtime_plan_for_contract,
)


def test_a_prohibited_blueprint_is_unavailable_even_when_it_is_the_suggestion():
    contract = PRDContract(title="No Postgres", prohibitions=["postgres"])

    resolution = resolve_blueprints(contract)

    assert "postgres" in resolution.unavailable
    assert "database" not in resolution.suggestions
    assert any("postgres" in conflict for conflict in resolution.conflicts)


def test_unspecified_slots_are_assumptions_not_required_stack_defaults():
    contract = PRDContract(title="API", stack_hint="node", required_stack=["express"])

    resolution = resolve_blueprints(contract)

    assert resolution.selected["backend"].id == "node-express"
    assert "frontend" in resolution.suggestions
    assert "database" in resolution.suggestions
    assert any("No frontend stack was specified" in item for item in resolution.assumptions)
    assert any("No database stack was specified" in item for item in resolution.assumptions)
    assert contract.required_stack == ["express"]


def test_named_but_unsupported_stack_yields_no_blueprint_instead_of_a_default():
    contract = PRDContract(title="Rails App", stack_hint="rails", required_stack=["rails"])

    resolution = resolve_blueprints(contract)

    assert "backend" not in resolution.selected
    assert "backend" in resolution.suggestions
    assert "rails" in resolution.unsupported
    assert any("rails" in error for error in resolution.errors)


def test_react_vite_node_tooling_does_not_select_a_backend_blueprint():
    contract = PRDContract(
        title="Frontend",
        stack_hint="node",
        required_stack=["react", "vite"],
    )

    resolution = resolve_blueprints(contract)

    assert "backend" not in resolution.selected
    assert resolution.selected["frontend"].id == "react-vite"


def test_node_react_postgres_prd_selects_a_backend_service():
    contract = PRDContract(
        title="OpenBazaar",
        stack_hint="node",
        required_stack=["react", "vite", "postgres"],
        entities=[{"name": "Listing"}],
        persistence_requirements=["Listings are stored in PostgreSQL."],
    )

    resolution = resolve_blueprints(contract)

    assert resolution.selected["backend"].id == "node-express"
    assert resolution.selected["frontend"].id == "react-vite"
    assert resolution.selected["database"].id == "postgres"


def test_selected_blueprint_paths_match_the_compiled_expected_file_path():
    contract = PRDContract(title="Course Desk", stack_hint="django", required_stack=["django"])
    django = resolve_blueprints(contract).selected["backend"]

    expected_model_path = django.path_for("models")
    milestone_paths = _architecture_expected_files_for_milestone("M-001", contract)

    assert expected_model_path == "backend/core/models.py"
    assert expected_model_path in milestone_paths


def test_folder_map_is_not_a_manifest():
    react = blueprint_by_id("react-vite")
    assert react is not None

    assert "app" in react.folder_map
    assert react.path_for("app") == "frontend/src/App.tsx"
    assert "frontend/src/App.tsx" not in react.config_paths()


def test_selecting_postgres_produces_compose_and_env_without_sqlite():
    contract = PRDContract(title="Market", stack_hint="postgres", required_stack=["postgres"])

    resolution = resolve_blueprints(contract)
    postgres = resolution.selected["database"]
    payload = postgres.to_dict()
    rendered = str(payload).lower()

    assert postgres.id == "postgres"
    assert "docker-compose.yml" in postgres.config_paths()
    assert ".env.example" in postgres.config_paths()
    assert "sqlite" not in rendered


def test_full_stack_runtime_plan_ties_backend_frontend_and_postgres_together():
    contract = PRDContract(
        title="OpenBazaar",
        stack_hint="django",
        required_stack=["django", "react", "vite", "postgres"],
    )

    plan = runtime_plan_for_contract(contract)
    paths = runtime_file_paths_for_contract(contract)

    assert [service["name"] for service in plan["services"]] == [
        "postgres",
        "backend",
        "frontend",
    ]
    assert plan["compose"] == {
        "path": "docker-compose.yml",
        "services": ["postgres", "backend", "frontend"],
        "database": "postgres",
    }
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
    }.issubset(set(paths))
