from __future__ import annotations

import ast

from shamsu.prd.parser import MarkdownPRDParser
from shamsu.prd.project import build_project_spec
from shamsu.templates.django.renderer import render_fixed_django_files, render_template


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
    assert [page.name for page in spec.pages] == ["Dashboard", "Tasks"]
    assert spec.generation_order[0].path == "manage.py"
    assert spec.generation_order[0].specialist is None


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
        "app/apps.py",
        "app/templates/base.html",
        "app/templates/login.html",
        "app/templates/register.html",
        "requirements.txt",
        ".env.example",
    }
    assert set(files) == expected_paths
    assert "SECRET_KEY = \"test-secret\"" in files["todo_app/settings.py"]
    assert "Django==5.0.6" in files["requirements.txt"]
    assert "btn btn-primary" in files["app/templates/base.html"]

    for path, content in files.items():
        if path.endswith(".py"):
            ast.parse(content)
