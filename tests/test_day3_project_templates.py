from __future__ import annotations

import ast

from shamsu.prd.parser import MarkdownPRDParser
from shamsu.prd.project import build_project_spec
from shamsu.templates.django.renderer import render_fixed_django_files, render_template
from shamsu.types import ParsedPRD


def test_build_project_spec_extracts_entities_pages_endpoints_and_order(tmp_path):
    prd_path = tmp_path / "todo.md"
    prd_path.write_text(
        "# Todo App\n\n"
        "## Entities\n"
        "- **Task**: title (text), done (boolean), user (FK to User)\n\n"
        "## API Endpoints\n"
        "- GET /api/tasks/ - list tasks\n"
        "- POST /api/tasks/ - create task\n\n"
        "## Pages\n"
        "- Dashboard: task stats and recent tasks\n"
        "- Tasks: full task list\n",
        encoding="utf-8",
    )

    spec = build_project_spec(MarkdownPRDParser().parse(prd_path))

    assert spec.project_name == "todo_app"
    assert spec.app_name == "app"
    assert spec.entities[0].name == "Task"
    assert [endpoint.method for endpoint in spec.endpoints] == ["GET", "POST"]
    assert [page.name for page in spec.pages] == [
        "Dashboard",
        "Tasks",
        "Task Form",
        "Task Detail",
    ]
    assert spec.generation_order[0].path == "manage.py"
    assert spec.generation_order[0].specialist is None
    assert "app/templates/task/list.html" in [file.path for file in spec.generation_order]
    assert "app/templates/task/_item.html" in [file.path for file in spec.generation_order]


def test_render_template_replaces_known_placeholders_only():
    rendered = render_template("Hello {{ name }} {{ unknown }}", {"name": "SHAMSU"})

    assert rendered == "Hello SHAMSU {{ unknown }}"


def test_render_fixed_django_files_are_deterministic_and_python_valid(tmp_path):
    prd_path = tmp_path / "todo.md"
    prd_path.write_text(
        "# Todo App\n\n"
        "## Entities\n"
        "- **Task**: title (text)\n",
        encoding="utf-8",
    )
    spec = build_project_spec(MarkdownPRDParser().parse(prd_path))

    files = render_fixed_django_files(spec, secret_key="test-secret")

    expected_paths = {
        "manage.py",
        "todo_app/__init__.py",
        "todo_app/settings.py",
        "todo_app/urls.py",
        "todo_app/wsgi.py",
        "todo_app/asgi.py",
            "app/__init__.py",
            "app/migrations/__init__.py",
            "app/apps.py",
        "app/templates/base.html",
        "app/templates/login.html",
        "app/templates/register.html",
        "app/templates/task/list.html",
        "app/templates/task/_item.html",
        "requirements.txt",
        ".env.example",
    }
    assert set(files) == expected_paths
    assert 'SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "test-secret")' in files["todo_app/settings.py"]
    assert "Django==5.0.6" in files["requirements.txt"]
    assert "btn btn-primary" in files["app/templates/base.html"]

    for path, content in files.items():
        if path.endswith(".py"):
            ast.parse(content)


def test_project_theme_selection_covers_common_domains():
    cases = [
        ("Expense Manager", "Track finance, expenses, budgets, and business reports.", "corporate"),
        ("Writing Desk", "A creative blog for long-form writing and publishing.", "nord"),
        ("Dev Portal", "A technical developer dashboard for code review.", "dark"),
        ("Inventory", "Track stock counts and warehouse transfers.", "corporate"),
    ]

    for title, raw_text, expected_theme in cases:
        spec = build_project_spec(ParsedPRD(title=title, sections={}, raw_text=raw_text))

        assert spec.theme == expected_theme


def test_resource_list_templates_use_consistent_urls_fields_and_htmx(tmp_path):
    prd_path = tmp_path / "todo.md"
    prd_path.write_text(
        "# Todo App\n\n"
        "## Entities\n"
        "- **Task**: title (text), done (boolean)\n\n"
        "## Pages\n"
        "- Dashboard: task stats\n"
        "- Tasks: full task list\n",
        encoding="utf-8",
    )
    spec = build_project_spec(MarkdownPRDParser().parse(prd_path))

    files = render_fixed_django_files(spec, secret_key="test-secret")
    list_html = files["app/templates/task/list.html"]
    item_html = files["app/templates/task/_item.html"]

    assert "{% load crispy_forms_tags %}" in list_html
    assert "{{ form|crispy }}" in list_html
    assert 'hx-post="{% url \'task-list\' %}"' in list_html
    assert 'hx-target="#task-rows"' in list_html
    assert 'hx-swap="beforeend"' in list_html
    assert 'class="btn btn-primary"' in list_html
    assert 'class="modal"' in list_html
    assert 'class="table table-zebra"' in list_html
    assert "{% include \"task/_item.html\" with object=object %}" in list_html
    assert "<th>Title</th>" in list_html
    assert "<th>Done</th>" in list_html

    assert '<tr id="task-{{ object.id }}">' in item_html
    assert "{{ object.title }}" in item_html
    assert "{{ object.done }}" in item_html
    assert 'hx-delete="{% url \'task-delete\' object.id %}"' in item_html
    assert 'hx-target="#task-{{ object.id }}"' in item_html
    assert 'hx-swap="outerHTML"' in item_html
    assert 'class="btn btn-error btn-sm"' in item_html
