from __future__ import annotations

from shamsu.templates.django.validator import validate_generated_templates
from shamsu.types import EntityFieldSpec, EntitySpec, PageSpec, ProjectSpec


def test_frontend_consistency_checker_accepts_valid_templates():
    project = _project()
    files = {
        "app/urls.py": """
from django.urls import path
urlpatterns = [
    path("tasks/", views.task_list, name="task-list"),
]
""",
        "app/templates/tasks/list.html": """
{% load crispy_forms_tags %}
<section id="task-list">
  <a href="{% url 'task-list' %}">Tasks</a>
  <form method="post" hx-post="{% url 'task-list' %}" hx-target="#task-list">
    {{ form|crispy }}
  </form>
  {% for item in tasks %}
    <div id="task-{{ item.id }}">{{ item.title }} {{ item.done }}</div>
  {% endfor %}
</section>
""",
    }

    result = validate_generated_templates(project, files)

    assert result.ok is True
    assert result.errors == []


def test_frontend_consistency_checker_reports_unknown_url_name():
    files = _valid_files()
    files["app/templates/tasks/list.html"] = """<a href="{% url 'missing-route' %}">Bad</a>"""

    result = validate_generated_templates(_project(), files)

    assert result.ok is False
    assert result.errors[0].rule == "url_name"
    assert "missing-route" in result.errors[0].message


def test_frontend_consistency_checker_reports_unknown_model_field():
    files = _valid_files()
    files["app/templates/tasks/list.html"] = "{{ item.not_a_task_field }}"

    result = validate_generated_templates(_project(), files)

    assert result.ok is False
    assert result.errors[0].rule == "model_field"
    assert "not_a_task_field" in result.errors[0].message


def test_frontend_consistency_checker_reports_missing_htmx_target_id():
    files = _valid_files()
    files["app/templates/tasks/list.html"] = """
<section id="task-list">
  <button hx-delete="{% url 'task-list' %}" hx-target="#missing-panel">Delete</button>
</section>
"""

    result = validate_generated_templates(_project(), files)

    assert result.ok is False
    assert result.errors[0].rule == "htmx_target"
    assert "#missing-panel" in result.errors[0].message


def test_frontend_consistency_checker_reports_raw_input_tags():
    files = _valid_files()
    files["app/templates/tasks/list.html"] = """
<form method="post">
  <input name="title">
</form>
"""

    result = validate_generated_templates(_project(), files)

    assert result.ok is False
    assert result.errors[0].rule == "raw_input"


def test_frontend_consistency_checker_can_report_multiple_failures():
    files = _valid_files()
    files["app/templates/tasks/list.html"] = """
<form hx-post="{% url 'missing-route' %}" hx-target="#missing">
  <input name="title">
  {{ item.unknown_field }}
</form>
"""

    result = validate_generated_templates(_project(), files)

    assert {error.rule for error in result.errors} == {
        "url_name",
        "model_field",
        "htmx_target",
        "raw_input",
    }


def _valid_files() -> dict[str, str]:
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


def _project() -> ProjectSpec:
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
