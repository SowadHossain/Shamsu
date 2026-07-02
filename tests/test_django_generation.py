from __future__ import annotations

import ast

import pytest

from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.templates.django.checker import BackendConsistencyChecker
from shamsu.templates.django.generators import render_backend_django_files
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.types import ApprovalRequest, TaskStepStatus


def _spec(text: str):
    return build_project_spec(parse_prd_text(text, fallback_title="PRD", markdown=True))


def _todo_spec():
    return _spec(
        "# Todo App\n\n"
        "## Entities\n"
        "- Task: title (text), done (boolean), user (FK to User)\n"
    )


def test_backend_generators_create_valid_todo_backend_files():
    files = render_backend_django_files(_todo_spec())

    expected = {
        "app/models.py",
        "app/serializers.py",
        "app/forms.py",
        "app/views.py",
        "app/urls.py",
        "app/admin.py",
    }
    assert set(files) == expected
    assert "class Task(models.Model):" in files["app/models.py"]
    assert "class TaskSerializer(serializers.ModelSerializer):" in files["app/serializers.py"]
    assert "class TaskForm(forms.ModelForm):" in files["app/forms.py"]
    assert "class TaskViewSet(ModelViewSet):" in files["app/views.py"]
    assert 'router.register("tasks", views.TaskViewSet' in files["app/urls.py"]
    assert "admin.site.register(Task)" in files["app/admin.py"]
    for path, content in files.items():
        ast.parse(content, filename=path)


def test_backend_generators_cover_expense_and_blog_relationships():
    expense = _spec(
        "# Expense Tracker\n\n"
        "## Data Models\n"
        "- Budget: name (string), amount (decimal max_digits=12 decimal_places=2), user (auth user)\n"
        "- Expense: title (text), amount (decimal), budget (belongs to Budget), notes (long text optional)\n"
    )
    blog = _spec(
        "# Blog Platform\n\n"
        "## Entities\n"
        "- Post: title (string), body (markdown), tags (many to many Tag), author (auth user)\n"
        "- Tag: name (string)\n"
    )

    expense_models = render_backend_django_files(expense)["tracker/models.py"]
    blog_models = render_backend_django_files(blog)["platform/models.py"]

    assert "models.DecimalField(max_digits=12, decimal_places=2)" in expense_models
    assert "models.ForeignKey('Budget'" in expense_models
    assert "models.ManyToManyField('Tag')" in blog_models


def test_project_writer_writes_inside_workspace_and_records_state(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")
    approvals: list[ApprovalRequest] = []

    def approve(request: ApprovalRequest) -> bool:
        approvals.append(request)
        return True

    state = DjangoProjectWriter(tmp_path, approval_func=approve).write_project(_todo_spec(), prd)

    assert approvals
    assert (tmp_path / "manage.py").exists()
    assert (tmp_path / "app" / "models.py").exists()
    assert "manage.py" in state.completed_files
    assert any(step.status == TaskStepStatus.SKIPPED for step in state.generation_order)


def test_project_writer_rejects_path_escape(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")

    with pytest.raises(Exception, match="outside workspace"):
        DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).write_project(
            _todo_spec(),
            prd,
            target_dir=tmp_path.parent,
        )


def test_project_writer_refuses_overwrite_when_denied(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    (tmp_path / "manage.py").write_text("old", encoding="utf-8")
    calls = 0

    def approve_generation_only(_request: ApprovalRequest) -> bool:
        nonlocal calls
        calls += 1
        return calls == 1

    with pytest.raises(PermissionError, match="Overwrite denied"):
        DjangoProjectWriter(tmp_path, approval_func=approve_generation_only).write_project(
            _todo_spec(),
            prd,
        )


def test_project_writer_can_resume_partial_state(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    writer = DjangoProjectWriter(tmp_path, approval_func=lambda _request: True)

    first = writer.write_project(_todo_spec(), prd)
    second = writer.write_project(_todo_spec(), prd)

    assert second.task_id == first.task_id
    assert (tmp_path / "app" / "admin.py").exists()


def test_consistency_checker_passes_generated_backend(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    project = _todo_spec()
    DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).write_project(project, prd)

    diagnostics = BackendConsistencyChecker(tmp_path).check(project)

    assert diagnostics == []


def test_consistency_checker_catches_bad_references(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    project = _todo_spec()
    DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).write_project(project, prd)
    (tmp_path / "app" / "serializers.py").write_text(
        "from rest_framework import serializers\n"
        "from .models import Task\n\n"
        "class TaskSerializer(serializers.ModelSerializer):\n"
        "    class Meta:\n"
        "        model = MissingTask\n"
        "        fields = ['id', 'missing']\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "urls.py").write_text(
        "from django.urls import path\nfrom . import views\nurlpatterns = [path('x/', views.nope)]\n",
        encoding="utf-8",
    )

    diagnostics = BackendConsistencyChecker(tmp_path).check(project)

    messages = [diagnostic.message for diagnostic in diagnostics]
    assert any("missing model MissingTask" in message for message in messages)
    assert any("URL references missing view nope" in message for message in messages)


def test_consistency_checker_catches_invalid_form_field_and_missing_view_serializer(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    project = _todo_spec()
    DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).write_project(project, prd)
    (tmp_path / "app" / "forms.py").write_text(
        "from django import forms\n"
        "from .models import Task\n\n"
        "class TaskForm(forms.ModelForm):\n"
        "    class Meta:\n"
        "        model = Task\n"
        "        fields = ['id', 'not_a_field']\n",
        encoding="utf-8",
    )
    (tmp_path / "app" / "views.py").write_text(
        "from rest_framework.viewsets import ModelViewSet\n"
        "from .models import Task\n\n"
        "class TaskViewSet(ModelViewSet):\n"
        "    serializer_class = MissingSerializer\n"
        "    queryset = Task.objects.all()\n",
        encoding="utf-8",
    )

    diagnostics = BackendConsistencyChecker(tmp_path).check(project)

    messages = [diagnostic.message for diagnostic in diagnostics]
    assert any("Field 'not_a_field'" in message for message in messages)
    assert any("missing symbol MissingSerializer" in message for message in messages)


def test_consistency_checker_catches_bad_admin_registration(tmp_path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n", encoding="utf-8")
    project = _todo_spec()
    DjangoProjectWriter(tmp_path, approval_func=lambda _request: True).write_project(project, prd)
    (tmp_path / "app" / "admin.py").write_text(
        "from django.contrib import admin\n"
        "from .models import Task\n\n"
        "admin.site.register(MissingModel)\n",
        encoding="utf-8",
    )

    diagnostics = BackendConsistencyChecker(tmp_path).check(project)

    assert any("Admin registers missing model MissingModel" in item.message for item in diagnostics)
