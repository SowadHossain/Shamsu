from __future__ import annotations

from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec


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
