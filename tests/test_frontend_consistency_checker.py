from __future__ import annotations

from pathlib import Path

from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.templates.django.frontend_checker import FrontendConsistencyChecker
from shamsu.templates.django.validator import validate_generated_templates
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.types import EntityFieldSpec, EntitySpec, PageSpec, ProjectSpec


def test_frontend_checker_passes_generated_templates(tmp_path: Path):
    project = _todo_spec()
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).write_project(project, prd)

    diagnostics = FrontendConsistencyChecker(tmp_path).check(project)

    assert diagnostics == []


def test_frontend_checker_catches_missing_url_name(tmp_path: Path):
    project = _generated_project(tmp_path)
    template = tmp_path / "app" / "templates" / "dashboard.html"
    template.write_text("{% url 'missing-url' %}\n", encoding="utf-8")

    diagnostics = FrontendConsistencyChecker(tmp_path).check(project)

    assert any("missing URL name missing-url" in item.message for item in diagnostics)


def test_frontend_checker_catches_invalid_model_field_reference(tmp_path: Path):
    project = _generated_project(tmp_path)
    template = tmp_path / "app" / "templates" / "resource_list.html"
    template.write_text("{{ task.not_a_field }}\n", encoding="utf-8")

    diagnostics = FrontendConsistencyChecker(tmp_path).check(project)

    assert any("missing field not_a_field on Task" in item.message for item in diagnostics)


def test_frontend_checker_catches_missing_htmx_target(tmp_path: Path):
    project = _generated_project(tmp_path)
    template = tmp_path / "app" / "templates" / "resource_form.html"
    template.write_text(
        "{% load crispy_forms_tags %}<form hx-post='.' hx-target='#missing'>{{ form|crispy }}</form>",
        encoding="utf-8",
    )

    diagnostics = FrontendConsistencyChecker(tmp_path).check(project)

    assert any("HTMX target #missing" in item.message for item in diagnostics)


def test_frontend_checker_catches_raw_form_inputs(tmp_path: Path):
    project = _generated_project(tmp_path)
    template = tmp_path / "app" / "templates" / "resource_form.html"
    template.write_text("<form><input name='title'></form>", encoding="utf-8")

    diagnostics = FrontendConsistencyChecker(tmp_path).check(project)

    assert any("crispy forms" in item.message for item in diagnostics)


def test_writer_check_project_includes_frontend_diagnostics(tmp_path: Path):
    project = _generated_project(tmp_path)
    template = tmp_path / "app" / "templates" / "resource_form.html"
    template.write_text("<form><textarea name='title'></textarea></form>", encoding="utf-8")

    diagnostics = DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).check_project(project)

    assert any(item.file_path.endswith("resource_form.html") for item in diagnostics)


def test_template_validator_accepts_valid_templates():
    result = validate_generated_templates(_validator_project(), _valid_template_files())

    assert result.ok is True
    assert result.errors == []


def test_template_validator_reports_unknown_url_name():
    files = _valid_template_files()
    files["app/templates/tasks/list.html"] = """<a href="{% url 'missing-route' %}">Bad</a>"""

    result = validate_generated_templates(_validator_project(), files)

    assert result.ok is False
    assert result.errors[0].rule == "url_name"
    assert "missing-route" in result.errors[0].message


def test_template_validator_reports_unknown_model_field():
    files = _valid_template_files()
    files["app/templates/tasks/list.html"] = "{{ item.not_a_task_field }}"

    result = validate_generated_templates(_validator_project(), files)

    assert result.ok is False
    assert result.errors[0].rule == "model_field"
    assert "not_a_task_field" in result.errors[0].message


def test_template_validator_reports_missing_htmx_target_id():
    files = _valid_template_files()
    files["app/templates/tasks/list.html"] = """
<section id="task-list">
  <button hx-delete="{% url 'task-list' %}" hx-target="#missing-panel">Delete</button>
</section>
"""

    result = validate_generated_templates(_validator_project(), files)

    assert result.ok is False
    assert result.errors[0].rule == "htmx_target"
    assert "#missing-panel" in result.errors[0].message


def test_template_validator_reports_raw_input_tags():
    files = _valid_template_files()
    files["app/templates/tasks/list.html"] = """
<form method="post">
  <input name="title">
</form>
"""

    result = validate_generated_templates(_validator_project(), files)

    assert result.ok is False
    assert result.errors[0].rule == "raw_input"


def test_template_validator_can_report_multiple_failures():
    files = _valid_template_files()
    files["app/templates/tasks/list.html"] = """
<form hx-post="{% url 'missing-route' %}" hx-target="#missing">
  <input name="title">
  {{ item.unknown_field }}
</form>
"""

    result = validate_generated_templates(_validator_project(), files)

    assert {error.rule for error in result.errors} == {
        "url_name",
        "model_field",
        "htmx_target",
        "raw_input",
    }


def _todo_spec():
    return build_project_spec(
        parse_prd_text(
            "# Todo App\n\n"
            "## Entities\n"
            "- Task: title (text), done (boolean), user (FK to User)\n",
            fallback_title="PRD",
            markdown=True,
        )
    )


def _generated_project(tmp_path: Path):
    project = _todo_spec()
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).write_project(project, prd)
    return project


def _valid_template_files() -> dict[str, str]:
    return {
        "app/urls.py": """
from django.urls import path
urlpatterns = [
    path("tasks/", views.task_list, name="task-list"),
]
""",
        "app/templates/tasks/list.html": """
{% load crispy_forms_tags %}
<section id="task-list">
  <form method="post" hx-post="{% url 'task-list' %}" hx-target="#task-list">
    {{ form|crispy }}
  </form>
  {{ item.title }}
</section>
""",
    }


def _validator_project() -> ProjectSpec:
    return ProjectSpec(
        project_name="todo_app",
        app_name="app",
        entities=[
            EntitySpec(
                name="Task",
                fields=[
                    EntityFieldSpec("title", "CharField"),
                    EntityFieldSpec("done", "BooleanField"),
                ],
            )
        ],
        endpoints=[],
        pages=[
            PageSpec(
                name="Tasks",
                page_type="list",
                purpose="Manage tasks",
                resource="Task",
                fields_shown=["title", "done"],
            )
        ],
    )
