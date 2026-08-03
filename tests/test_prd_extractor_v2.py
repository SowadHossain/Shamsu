from __future__ import annotations

from pathlib import Path

from shamsu.prd.classifier import classify_archetype
from shamsu.prd.extractor import extract_entities
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.templates.registry import get_template_provider
from shamsu.types import Archetype


def test_todo_prd_infers_crud_endpoints_and_pages():
    parsed = parse_prd_text(
        "# Todo App\n\n"
        "## Entities\n"
        "- Task: title (text max_length=120), done (boolean default=false), user (FK to User)\n"
    )

    spec = build_project_spec(parsed)

    assert spec.entities[0].name == "Task"
    assert spec.entities[0].fields[0].kwargs["max_length"] == 120
    assert spec.entities[0].fields[1].kwargs["default"] is False
    assert {endpoint.method for endpoint in spec.endpoints} == {"GET", "POST", "PUT", "DELETE"}
    assert {page.page_type for page in spec.pages} >= {"dashboard", "list", "detail", "form"}
    assert spec.archetype == Archetype.WEB_CRUD
    assert spec.archetype_confidence >= 0.65


def test_expense_prd_extracts_decimal_foreign_key_and_optional_fields():
    parsed = parse_prd_text(
        "# Expense Tracker\n\n"
        "## Data Models\n"
        "- Budget: name (string), amount (decimal max_digits=12 decimal_places=2), user (auth user)\n"
        "- Expense: title (text), amount (decimal), budget (belongs to Budget), notes (long text optional)\n"
    )

    spec = build_project_spec(parsed)
    budget = spec.entities[0]
    expense = spec.entities[1]

    assert budget.fields[1].django_type == "DecimalField"
    assert budget.fields[1].kwargs["max_digits"] == 12
    assert budget.fields[2].kwargs["to"] == "User"
    assert expense.fields[2].django_type == "ForeignKey"
    assert expense.fields[2].kwargs["to"] == "Budget"
    assert expense.fields[3].kwargs["blank"] is True
    assert spec.theme == "corporate"


def test_blog_prd_extracts_public_pages_many_to_many_and_choices():
    parsed = parse_prd_text(
        "# Blog Platform\n\n"
        "## Entities\n"
        "- Post: title (string), body (markdown), status (choices: draft/published), tags (many to many Tag), author (auth user)\n"
        "- Tag: name (string)\n\n"
        "## Pages\n"
        "- Public Blog List: public list of published posts\n"
        "- Post Detail: public detail page\n"
        "- Dashboard: private author overview\n"
    )

    spec = build_project_spec(parsed)
    post = spec.entities[0]

    assert post.fields[1].django_type == "TextField"
    assert post.fields[2].kwargs["choices"] == ["draft", "published"]
    assert post.fields[3].django_type == "ManyToManyField"
    assert "many_to_many:Tag" in post.relationships
    public_pages = [page for page in spec.pages if "Public" in page.name or "Post Detail" in page.name]
    assert public_pages
    assert all(page.requires_login is False for page in public_pages)
    assert spec.theme == "nord"


def test_classifier_detects_game_archetype():
    parsed = parse_prd_text(
        "# Cube Runner 3D\n\n"
        "Build a realtime 3D game with player movement, physics, scoring, and levels."
    )

    decision = classify_archetype(parsed)

    assert decision.archetype == Archetype.REALTIME_3D_GAME
    assert decision.confidence >= 0.65


def test_non_django_archetype_uses_generic_generation_order_until_provider_exists():
    parsed = parse_prd_text(
        "# Cube Runner 3D\n\n"
        "Build a realtime 3D game with player movement, physics, scoring, and levels."
    )

    spec = build_project_spec(parsed)

    assert spec.archetype == Archetype.REALTIME_3D_GAME
    assert [file.path for file in spec.generation_order] == ["index.html", "README.md"]


def test_classifier_falls_back_to_generic_web_for_vague_prd():
    parsed = parse_prd_text("# Something\n\nMake a nice thing for people.")

    decision = classify_archetype(parsed)

    assert decision.archetype == Archetype.GENERIC_WEB


def test_template_registry_wraps_django_for_web_crud():
    parsed = parse_prd_text(
        "# Todo App\n\n"
        "## Tech Stack\n"
        "- Django\n"
        "- SQLite\n\n"
        "## Entities\n"
        "- Task: title (text), done (boolean)\n"
    )
    spec = build_project_spec(parsed)
    provider = get_template_provider(spec.archetype)

    assert provider.smoke_test() is True
    assert provider.render_all(spec)["manage.py"].startswith("#!/usr/bin/env python")
    assert provider.build_manifest(spec).holes


ATLASOPS_PRD_EXCERPT = """# Product Requirements Document: AtlasOps Command Center

## 1. Overview

AtlasOps Command Center is a local-first operations platform for managing incident
response, field work orders, inventory, vendors, approvals, audit trails, and
executive reporting. The product must include both a browser UI and a terminal CLI.
The application must run locally without cloud services. The default implementation
should use SQLite for persistence.

## 4. Recommended Technical Stack

- TypeScript
- React
- Vite
- Node.js
- SQLite
- Zod
- Vitest
- Playwright

## 5. Roles

### Entity: User

Fields:

- id: string, required, unique
- name: string, required
- email: string, required, unique, valid email
- role: enum, required, values: admin, manager, dispatcher, technician, auditor
- active: boolean, default true

Rules:

- Only active users can be assigned work.

## 6. Core Entities

### Entity: Site

Fields:

- id: string, required, unique
- code: string, required, unique
- name: string, required
- timezone: string, required
- risk_level: enum, values: low, medium, high, critical
- active: boolean, default true

Rules:

- Site code must be uppercase alphanumeric plus hyphen only.

### Entity: Incident

Fields:

- id: string, required, unique
- incident_number: string, required, unique
- title: string, required
- description: text, required
- site_id: string, required, references Site
- severity: enum, values: low, medium, high, critical
- status: enum, values: new, triaged, linked_to_work_order, resolved, canceled
- detected_at: datetime, required
- resolved_at: datetime, optional
- tags: string array
- created_by_user_id: string, required, references User

Rules:

- A canceled incident cannot be converted to a work order.

### Entity: WorkOrder

Fields:

- id: string, required, unique
- work_order_number: string, required, unique
- title: string, required
- site_id: string, required, references Site
- incident_id: string, optional, references Incident
- assigned_user_id: string, optional, references User
- priority: enum, values: low, normal, high, urgent
- status: enum, values: draft, ready, assigned, in_progress, blocked, completed, canceled

## 8. Required CLI Commands

```bash
atlas init
atlas status
atlas incident add --title "Water leak" --site HQ --severity high --reporter "Sam"
```
"""


def test_heading_style_entities_do_not_become_field_name_models():
    parsed = parse_prd_text(ATLASOPS_PRD_EXCERPT, markdown=True)

    entities = extract_entities(parsed)
    names = {entity.name for entity in entities}

    assert {"User", "Site", "Incident", "WorkOrder"} <= names
    assert {"Name", "Email", "SiteId", "IncidentNumber", "WorkOrderNumber"} - names == {
        "Name", "Email", "SiteId", "IncidentNumber", "WorkOrderNumber",
    }

    incident = next(entity for entity in entities if entity.name == "Incident")
    fields = {field.name: field for field in incident.fields}
    assert fields["site"].django_type == "ForeignKey"
    assert fields["site"].kwargs["to"] == "Site"
    assert fields["created_by_user"].kwargs["to"] == "User"
    assert fields["status"].kwargs["choices"] == [
        "new", "triaged", "linked_to_work_order", "resolved", "canceled",
    ]
    assert fields["tags"].django_type == "JSONField"
    assert fields["tags"].kwargs["default"] == {"__callable__": "list"}


def test_complex_node_cli_prd_routes_to_freeform_not_django():
    parsed = parse_prd_text(ATLASOPS_PRD_EXCERPT, markdown=True)

    spec = build_project_spec(parsed)

    assert spec.generation_ready is True
    assert spec.needs_input is False
    assert {entity.name for entity in spec.entities} >= {"User", "Site", "Incident", "WorkOrder"}
    assert "node" in spec.prd_contract.required_stack
    assert getattr(spec.suitability.strategy, "value", spec.suitability.strategy) == "freeform"
    assert "Django generator" in " ".join(spec.suitability.conflicts)


def test_data_model_plain_headings_extract_entities_from_long_prd():
    prd_text = Path("evals/fixtures/prds/atlasdesk_long.md").read_text(encoding="utf-8")
    parsed = parse_prd_text(prd_text, markdown=True)

    entities = extract_entities(parsed)
    names = {entity.name for entity in entities}

    assert {"Incident", "Note", "HealthMetric"} <= names
    incident = next(entity for entity in entities if entity.name == "Incident")
    incident_fields = {field.name: field for field in incident.fields}
    assert {"title", "customer", "severity", "status", "owner", "sla_minutes", "notes"} <= (
        set(incident_fields)
    )
    assert incident_fields["sla_minutes"].django_type == "IntegerField"
    assert incident_fields["notes"].django_type == "JSONField"

    note = next(entity for entity in entities if entity.name == "Note")
    assert {"incident_id", "author", "body", "created_at"} <= {field.name for field in note.fields}


def test_sections_after_an_entity_container_are_not_entities():
    """`parsed.sections` is flat, so a "## Entities" container had no closing
    boundary: every later "##" section inherited it and became an entity whose
    bullets lexed as fields. "## API Endpoints" and "## Pages" - the ordinary PRD
    shape - produced APIEndpoints/Pages entities and Django models to match."""
    parsed = parse_prd_text(
        "# Todo App\n\n"
        "## Entities\n"
        "- Task: title, done\n\n"
        "## API Endpoints\n"
        "- GET /api/tasks/ - list tasks\n"
        "- POST /api/tasks/ - create task\n\n"
        "## Pages\n"
        "- Dashboard: task stats and recent tasks\n"
        "- Tasks: full task list\n",
        markdown=True,
    )

    assert [entity.name for entity in extract_entities(parsed)] == ["Task"]

    spec = build_project_spec(parsed)
    assert [page.name for page in spec.pages] == [
        "Dashboard",
        "Tasks",
        "Task Form",
        "Task Detail",
    ]


def test_entity_container_still_claims_real_sub_entities():
    """The container boundary must not over-correct: genuine entity sub-sections
    under a "Data Model" heading are still extracted."""
    parsed = parse_prd_text(
        "# App\n\n"
        "## Data Model\n\n"
        "### Task\n"
        "- title: str\n"
        "- done: bool\n\n"
        "### Category\n"
        "- name: str\n",
        markdown=True,
    )

    assert {entity.name for entity in extract_entities(parsed)} == {"Task", "Category"}
