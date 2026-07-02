from __future__ import annotations

from pathlib import Path

from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.templates.django.frontend_checker import FrontendConsistencyChecker
from shamsu.templates.django.writer import DjangoProjectWriter


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


def _generated_project(tmp_path: Path):
    project = _todo_spec()
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).write_project(project, prd)
    return project
