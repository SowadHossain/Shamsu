from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.full_pipeline import FullDjangoPipeline
from shamsu.prd.contract import extract_contract
from shamsu.prd.input import parse_prd_file
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.templates.django.frontend import render_django_tests
from shamsu.templates.django.generators import render_backend_django_files
from shamsu.templates.django.renderer import render_fixed_django_files
from shamsu.types import Archetype

FIXTURE = Path(__file__).parent / "fixtures" / "prds" / "taskflow.txt"


def test_taskflow_contract_captures_full_stack_requirements():
    contract = extract_contract(parse_prd_file(FIXTURE))

    assert contract.title == "TaskFlow Todo App"
    assert contract.project_kind == "web_app"
    assert contract.requires_full_stack is True
    assert contract.stack_hint == ""
    assert "sqlite" in contract.required_stack
    assert {entity["name"] for entity in contract.entities} >= {"User", "Category", "Task"}
    assert {item["path"] for item in contract.api_endpoints} >= {
        "/api/auth/register",
        "/api/tasks",
        "/api/tasks/:taskId",
    }
    assert contract.query_capabilities == ["search", "filter", "sort", "pagination"]
    assert contract.authentication_rules
    assert contract.authorization_rules
    assert contract.security_requirements
    assert contract.required_tests
    assert contract.acceptance_criteria


def test_taskflow_routes_to_real_full_stack_generation_plan():
    spec = build_project_spec(parse_prd_file(FIXTURE))

    assert spec.archetype is Archetype.WEB_CRUD
    assert spec.category == "web-crud"
    assert spec.generation_ready is True
    assert spec.needs_input is False
    assert {entity.name for entity in spec.entities} >= {"User", "Category", "Task"}
    assert len(spec.generation_order) > 20
    assert [item.path for item in spec.generation_order[:2]] != ["index.html", "README.md"]
    assert any("Django" in item for item in spec.assumptions)
    assert spec.definition_of_done


def test_full_stack_prd_without_domain_entities_stops_for_input(tmp_path):
    prd = tmp_path / "ambiguous.txt"
    prd.write_text(
        "Ambiguous App\n\n1 Product Overview\n"
        "A full-stack web application with authentication and a SQLite database.\n\n"
        "10 Authentication and Authorization\nUsers must log in.\n\n"
        "24 Database Requirements\nPersistent records are stored in SQLite.\n",
        encoding="utf-8",
    )

    spec = build_project_spec(parse_prd_file(prd))

    assert spec.needs_input is True
    assert spec.generation_ready is False
    assert spec.generation_order == []
    assert "entities" in spec.clarification_question.lower()


def test_security_word_trust_does_not_select_rust():
    contract = extract_contract(parse_prd_file(FIXTURE))

    assert contract.stack_hint != "rust"
    assert "rust" not in contract.required_stack


@pytest.mark.asyncio
async def test_full_pipeline_writes_nothing_when_contract_needs_input(tmp_path):
    prd = tmp_path / "ambiguous.txt"
    prd.write_text(
        "Ambiguous App\n\n1 Product Overview\n"
        "A full-stack web application with authentication and a SQLite database.\n\n"
        "10 Authentication and Authorization\nUsers must log in.\n\n"
        "24 Database Requirements\nPersistent records are stored in SQLite.\n",
        encoding="utf-8",
    )
    target = tmp_path / "output"

    class EmptySearch:
        def search(self, *_args, **_kwargs):
            return []

    result = await FullDjangoPipeline(
        tmp_path,
        search=EmptySearch(),
        approval_func=lambda _request: True,
    ).run(prd, target)

    assert result.success is False
    assert result.written_files == []
    assert "needs input" in result.error.lower()
    assert not target.exists()


def test_taskflow_django_output_contains_contract_critical_behavior():
    spec = build_project_spec(parse_prd_file(FIXTURE))
    backend = render_backend_django_files(spec)
    fixed = render_fixed_django_files(spec, secret_key="test-secret")
    generated_tests = render_django_tests(spec)

    assert "TaskPagination" in backend["app/views.py"]
    assert 'page_size_query_param = "limit"' in backend["app/views.py"]
    assert "Q(title__icontains=search)" in backend["app/views.py"]
    assert 'due == "overdue"' in backend["app/views.py"]
    assert "def complete(" in backend["app/views.py"]
    assert "def reopen(" in backend["app/views.py"]
    assert "get_object_or_404(Task, pk=pk, user=request.user)" in backend["app/views.py"]
    assert 'namespace="api"' in backend["app/urls.py"]
    assert "def api_register(" in backend["app/views.py"]
    assert "def api_dashboard_statistics(" in backend["app/views.py"]
    assert "app/migrations/__init__.py" in fixed
    assert "Pillow" not in fixed["requirements.txt"]
    assert "test_cannot_retrieve_another_users_record" in generated_tests
    assert "test_api_registration_and_email_login" in generated_tests


def test_pdf_schema_tables_map_to_entities_in_document_order():
    parsed = parse_prd_text(
        "25.2 Categories Table\nFields:\n25.3 Tasks Table\nFields:",
        line_pages=[19, 19, 19, 20],
    )
    parsed.tables = [
        {
            "page": 19,
            "rows": [
                ["Field Type Requirements"],
                ["user_id Integer Required, foreign key to users"],
                ["name Text Required, maximum 50 characters"],
            ],
        },
        {
            "page": 20,
            "rows": [
                ["Field Type Requirements"],
                ["user_id Integer Required, foreign key to users"],
                ["category_id Integer Optional, foreign key to categories"],
                ["title Text Required, maximum 200 characters"],
            ],
        },
    ]

    contract = extract_contract(parsed)

    assert [entity["name"] for entity in contract.entities] == ["Category", "Task"]
    category_field = next(
        field for field in contract.entities[1]["fields"] if field["name"] == "category"
    )
    assert category_field["kwargs"]["on_delete"] == "SET_NULL"
