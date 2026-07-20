"""Deterministic Django backend generators from ProjectSpec."""
from __future__ import annotations

import ast
import re
from typing import Any

from shamsu.types import EntityFieldSpec, EntitySpec, PageSpec, ProjectSpec


def render_backend_django_files(project: ProjectSpec) -> dict[str, str]:
    files = {
        f"{project.app_name}/models.py": render_models(project),
        f"{project.app_name}/serializers.py": render_serializers(project),
        f"{project.app_name}/forms.py": render_forms(project),
        f"{project.app_name}/views.py": render_views(project),
        f"{project.app_name}/urls.py": render_app_urls(project),
        f"{project.app_name}/admin.py": render_admin(project),
    }
    for path, content in files.items():
        ast.parse(content, filename=path)
    return files


def render_models(project: ProjectSpec) -> str:
    imports = ["from django.conf import settings", "from django.db import models"]
    blocks = [*imports, ""]
    for entity in _business_entities(project):
        blocks.append(f"class {entity.name}(models.Model):")
        for field in entity.fields:
            blocks.append(f"    {field.name} = {_render_model_field(field)}")
        blocks.extend(_render_model_meta(entity))
        display_field = _display_field(entity)
        blocks.extend(
            [
                "",
                "    def __str__(self):",
                f"        return str(self.{display_field})",
                "",
            ]
        )
    return "\n".join(blocks).rstrip() + "\n"


def render_serializers(project: ProjectSpec) -> str:
    names = [entity.name for entity in _business_entities(project)]
    imports = [
        "from rest_framework import serializers",
        "",
        f"from .models import {', '.join(names)}" if names else "from . import models",
        "",
    ]
    blocks = imports
    for entity in _business_entities(project):
        fields = ["id", *[field.name for field in entity.fields if field.name != _user_field(entity)]]
        blocks.extend(
            [
                f"class {entity.name}Serializer(serializers.ModelSerializer):",
                "    class Meta:",
                f"        model = {entity.name}",
                f"        fields = {fields!r}",
                "",
            ]
        )
    return "\n".join(blocks).rstrip() + "\n"


def render_forms(project: ProjectSpec) -> str:
    names = [entity.name for entity in _business_entities(project)]
    blocks = [
        "from django import forms",
        "from django.contrib.auth import get_user_model",
        "from django.contrib.auth.forms import UserCreationForm",
        "",
        f"from .models import {', '.join(names)}" if names else "from . import models",
        "",
        "",
        "class RegistrationForm(UserCreationForm):",
        "    full_name = forms.CharField(max_length=100)",
        "    email = forms.EmailField()",
        "",
        "    class Meta(UserCreationForm.Meta):",
        "        model = get_user_model()",
        "        fields = ('full_name', 'email', 'username')",
        "",
        "    def clean_email(self):",
        "        email = self.cleaned_data['email'].strip().lower()",
        "        if get_user_model().objects.filter(email__iexact=email).exists():",
        "            raise forms.ValidationError('An account with this email already exists.')",
        "        return email",
        "",
        "    def save(self, commit=True):",
        "        user = super().save(commit=False)",
        "        user.first_name = self.cleaned_data['full_name'].strip()",
        "        user.email = self.cleaned_data['email']",
        "        if commit:",
        "            user.save()",
        "        return user",
        "",
        "",
        "class ProfileForm(forms.ModelForm):",
        "    class Meta:",
        "        model = get_user_model()",
        "        fields = ('first_name', 'email')",
        "",
        "    def clean_email(self):",
        "        email = self.cleaned_data['email'].strip().lower()",
        "        existing = get_user_model().objects.filter(email__iexact=email).exclude(pk=self.instance.pk)",
        "        if existing.exists():",
        "            raise forms.ValidationError('An account with this email already exists.')",
        "        return email",
        "",
    ]
    for entity in _business_entities(project):
        fields = [
            field.name
            for field in entity.fields
            if field.name != _user_field(entity)
            and not field.kwargs.get("auto_now")
            and not field.kwargs.get("auto_now_add")
        ]
        blocks.extend(
            [
                f"class {entity.name}Form(forms.ModelForm):",
                "    class Meta:",
                f"        model = {entity.name}",
                f"        fields = {fields!r}",
            ]
        )
        category_field = next(
            (
                field
                for field in entity.fields
                if field.django_type == "ForeignKey" and field.kwargs.get("to") == "Category"
            ),
            None,
        )
        if _user_field(entity) or category_field:
            blocks.extend(
                [
                    "",
                    "    def __init__(self, *args, user=None, **kwargs):",
                    "        super().__init__(*args, **kwargs)",
                ]
            )
            if category_field:
                blocks.extend(
                    [
                        "        if user is not None:",
                        f'            self.fields["{category_field.name}"].queryset = Category.objects.filter(user=user)',
                    ]
                )
        blocks.append("")
    return "\n".join(blocks).rstrip() + "\n"


def render_views(project: ProjectSpec) -> str:
    entities = _business_entities(project)
    names = [entity.name for entity in entities]
    serializer_names = [f"{entity.name}Serializer" for entity in entities]
    form_names = [f"{entity.name}Form" for entity in entities]
    blocks = [
        "from django.contrib.auth import authenticate, get_user_model, login as auth_login, logout as auth_logout, update_session_auth_hash",
        "from django.contrib.auth.decorators import login_required",
        "from django.contrib.auth.forms import PasswordChangeForm",
        "from django.contrib.auth.password_validation import validate_password",
        "from django.core.exceptions import ValidationError as DjangoValidationError",
        "from django.db.models import Q",
        "from django.shortcuts import get_object_or_404, redirect, render",
        "from django.utils import timezone",
        "from django.views.decorators.http import require_POST",
        "from rest_framework.exceptions import ValidationError",
        "from rest_framework.decorators import action, api_view, permission_classes",
        "from rest_framework.permissions import AllowAny, IsAuthenticated",
        "from rest_framework.pagination import PageNumberPagination",
        "from rest_framework.response import Response",
        "from rest_framework import status as http_status",
        "from rest_framework_simplejwt.tokens import RefreshToken",
        "from rest_framework.viewsets import ModelViewSet",
        "",
        (
            f"from .forms import ProfileForm, RegistrationForm, {', '.join(form_names)}"
            if form_names
            else "from .forms import ProfileForm, RegistrationForm"
        ),
        f"from .models import {', '.join(names)}" if names else "from . import models",
        (
            f"from .serializers import {', '.join(serializer_names)}"
            if serializer_names
            else "from . import serializers"
        ),
        "",
    ]
    if any(entity.name == "Task" for entity in entities):
        blocks.extend(
            [
                "class TaskPagination(PageNumberPagination):",
                '    page_size_query_param = "limit"',
                "    max_page_size = 100",
                "",
            ]
        )
    for entity in entities:
        blocks.extend(_render_viewset(entity))
        blocks.extend(_render_delete_view(entity))
    rendered_pages: set[str] = set()
    for page in project.pages:
        function_name = _page_function_name(page)
        if function_name in rendered_pages:
            continue
        rendered_pages.add(function_name)
        blocks.extend(_render_page_view(page, entities))
    if "dashboard" not in rendered_pages:
        blocks.extend(_render_page_view(PageSpec("Dashboard", "dashboard", "Overview"), entities))
    blocks.extend(_render_register_view())
    blocks.extend(_render_account_views())
    blocks.extend(_render_api_account_views(entities))
    return "\n".join(blocks).rstrip() + "\n"


def render_app_urls(project: ProjectSpec) -> str:
    entities = _business_entities(project)
    blocks = [
        "from django.contrib.auth import views as auth_views",
        "from django.urls import include, path",
        "from rest_framework.routers import DefaultRouter",
        "",
        "from . import views",
        "",
        "router = DefaultRouter()",
    ]
    for entity in entities:
        blocks.append(
            f'router.register("{_resource_slug(entity.name)}", views.{entity.name}ViewSet, '
            f'basename="{_resource_url_name(entity.name)}")'
        )
    blocks.extend(
        [
            "",
            "urlpatterns = [",
            '    path("api/", include((router.urls, "api"), namespace="api")),',
        ]
    )
    seen: set[str] = set()
    for page in project.pages:
        name = _page_url_name(page)
        view = _page_function_name(page)
        route = _page_route(page)
        if name in seen:
            continue
        seen.add(name)
        blocks.append(f'    path("{route}", views.{view}, name="{name}"),')
    for entity in entities:
        name = f"{_resource_url_name(entity.name)}-delete"
        if name in seen:
            continue
        seen.add(name)
        route = f"{_resource_slug(entity.name)}/<int:pk>/delete/"
        view = f"{_to_snake_case(entity.name)}_delete"
        blocks.append(f'    path("{route}", views.{view}, name="{name}"),')
    if "dashboard" not in seen:
        blocks.append('    path("dashboard/", views.dashboard, name="dashboard"),')
    blocks.extend(
        [
            '    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),',
            '    path("logout/", auth_views.LogoutView.as_view(), name="logout"),',
            '    path("register/", views.register, name="register"),',
            '    path("account/delete/", views.delete_account, name="delete-account"),',
            '    path("api/auth/register/", views.api_register, name="api-register"),',
            '    path("api/auth/login/", views.api_login, name="api-login"),',
            '    path("api/auth/logout/", views.api_logout, name="api-logout"),',
            '    path("api/auth/me/", views.api_current_user, name="api-current-user"),',
            '    path("api/users/me/", views.api_profile, name="api-profile"),',
            '    path("api/users/me/password/", views.api_change_password, name="api-change-password"),',
            '    path("api/dashboard/statistics/", views.api_dashboard_statistics, name="api-dashboard-statistics"),',
            "]",
        ]
    )
    return "\n".join(blocks) + "\n"


def render_admin(project: ProjectSpec) -> str:
    names = [entity.name for entity in _business_entities(project)]
    blocks = ["from django.contrib import admin", ""]
    if names:
        blocks.extend([f"from .models import {', '.join(names)}", ""])
        blocks.extend(f"admin.site.register({name})" for name in names)
    return "\n".join(blocks).rstrip() + "\n"


def _render_model_field(field: EntityFieldSpec) -> str:
    kwargs = dict(field.kwargs)
    if field.django_type == "ForeignKey":
        target = kwargs.pop("to", "User")
        if target == "User":
            target = "settings.AUTH_USER_MODEL"
        else:
            target = repr(target)
        return f"models.ForeignKey({target}, {_render_kwargs(kwargs)})"
    if field.django_type == "ManyToManyField":
        target = kwargs.pop("to", "User")
        target_text = "settings.AUTH_USER_MODEL" if target == "User" else repr(target)
        rendered = _render_kwargs(kwargs)
        return f"models.ManyToManyField({target_text}{', ' if rendered else ''}{rendered})"
    return f"models.{field.django_type}({_render_kwargs(kwargs)})"


def _render_model_meta(entity: EntitySpec) -> list[str]:
    field_names = {field.name for field in entity.fields}
    lines: list[str] = []
    if entity.name == "Category" and {"user", "name"} <= field_names:
        lines.extend(
            [
                "",
                "    class Meta:",
                "        ordering = ['name']",
                "        constraints = [",
                "            models.UniqueConstraint(fields=['user', 'name'], name='unique_category_name_per_user'),",
                "        ]",
            ]
        )
    elif entity.name == "Task":
        indexed = [name for name in ("status", "priority", "due_at") if name in field_names]
        if indexed:
            lines.extend(["", "    class Meta:", "        indexes = ["])
            lines.extend(
                f"            models.Index(fields=['user', '{name}']),"
                for name in indexed
            )
            lines.append("        ]")
    return lines


def _render_kwargs(kwargs: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in kwargs.items():
        if key == "choices" and isinstance(value, list):
            choices = [(choice, _display_name(choice)) for choice in value]
            parts.append(f"{key}={choices!r}")
        elif isinstance(value, str) and value in {
            "CASCADE", "PROTECT", "RESTRICT", "SET_DEFAULT", "SET_NULL",
        }:
            parts.append(f"{key}=models.{value}")
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts)


def _render_viewset(entity: EntitySpec) -> list[str]:
    user_field = _user_field(entity)
    blocks = [
        f"class {entity.name}ViewSet(ModelViewSet):",
        f"    serializer_class = {entity.name}Serializer",
        "    permission_classes = [IsAuthenticated]",
    ]
    if entity.name == "Task":
        blocks.append("    pagination_class = TaskPagination")
    if user_field:
        queryset_lines = (
            _render_task_queryset(entity, user_field)
            if entity.name == "Task"
            else [
                "    def get_queryset(self):",
                f"        return {entity.name}.objects.filter({user_field}=self.request.user)",
            ]
        )
        blocks.extend(
            [
                "",
                *queryset_lines,
                "",
                "    def perform_create(self, serializer):",
                "        self._validate_owned_relations(serializer)",
                f"        serializer.save({user_field}=self.request.user)",
                "",
                "    def perform_update(self, serializer):",
                "        self._validate_owned_relations(serializer)",
                "        serializer.save()",
                "",
                "    def _validate_owned_relations(self, serializer):",
            ]
        )
        owned_relations = [
            field
            for field in entity.fields
            if field.django_type == "ForeignKey" and field.kwargs.get("to") == "Category"
        ]
        if owned_relations:
            for field in owned_relations:
                blocks.extend(
                    [
                        f'        {field.name} = serializer.validated_data.get("{field.name}")',
                        f"        if {field.name} is not None and {field.name}.user_id != self.request.user.id:",
                        f'            raise ValidationError({{"{field.name}": "Select one of your own categories."}})',
                    ]
                )
        else:
            blocks.append("        return None")
        blocks.append("")
        if entity.name == "Task":
            blocks.extend(_render_task_actions())
    else:
        blocks.extend([f"    queryset = {entity.name}.objects.all()", ""])
    return blocks


def _render_task_queryset(entity: EntitySpec, user_field: str) -> list[str]:
    field_names = {field.name for field in entity.fields}
    lines = [
        "    def get_queryset(self):",
        f"        queryset = {entity.name}.objects.filter({user_field}=self.request.user)",
    ]
    if {"title", "description"} <= field_names:
        lines.extend(
            [
                '        search = self.request.query_params.get("search", "").strip()',
                "        if search:",
                "            queryset = queryset.filter(Q(title__icontains=search) | Q(description__icontains=search))",
            ]
        )
    for field_name in ("status", "priority"):
        if field_name in field_names:
            lines.extend(
                [
                    f'        {field_name} = self.request.query_params.get("{field_name}")',
                    f"        if {field_name}:",
                    f"            queryset = queryset.filter({field_name}={field_name})",
                ]
            )
    if "category" in field_names:
        lines.extend(
            [
                '        category_id = self.request.query_params.get("categoryId")',
                "        if category_id:",
                "            queryset = queryset.filter(category_id=category_id)",
            ]
        )
    if "due_at" in field_names:
        lines.extend(
            [
                '        due = self.request.query_params.get("due")',
                "        now = timezone.now()",
                '        if due == "overdue":',
                '            queryset = queryset.filter(due_at__lt=now).exclude(status="completed")',
                '        elif due == "today":',
                "            queryset = queryset.filter(due_at__date=now.date())",
                '        elif due == "upcoming":',
                "            queryset = queryset.filter(due_at__gt=now)",
            ]
        )
    lines.extend(
        [
            '        sort_by = self.request.query_params.get("sortBy", "created_at")',
            '        sort_order = self.request.query_params.get("sortOrder", "desc")',
            f"        allowed_sort_fields = {sorted(field_names & {'title', 'status', 'priority', 'due_at', 'created_at', 'updated_at'})!r}",
            '        sort_by = sort_by if sort_by in allowed_sort_fields else "created_at"',
            '        prefix = "-" if sort_order.lower() == "desc" else ""',
            "        return queryset.order_by(f\"{prefix}{sort_by}\")",
        ]
    )
    return lines


def _render_task_actions() -> list[str]:
    return [
        '    @action(detail=True, methods=["post"])',
        "    def complete(self, request, pk=None):",
        "        task = self.get_object()",
        '        task.status = "completed"',
        "        task.completed_at = timezone.now()",
        '        task.save(update_fields=["status", "completed_at", "updated_at"])',
        "        return Response(self.get_serializer(task).data)",
        "",
        '    @action(detail=True, methods=["post"])',
        "    def reopen(self, request, pk=None):",
        "        task = self.get_object()",
        '        task.status = "pending"',
        "        task.completed_at = None",
        '        task.save(update_fields=["status", "completed_at", "updated_at"])',
        "        return Response(self.get_serializer(task).data)",
        "",
    ]


def _render_page_view(page: PageSpec, entities: list[EntitySpec]) -> list[str]:
    function_name = _page_function_name(page)
    decorator = ["@login_required"] if page.requires_login else []
    template = _page_template(page)
    resource = _find_entity(page.resource, entities)
    blocks = [*decorator, f"def {function_name}(request):"]
    lowered_name = page.name.lower()
    if "user profile" in lowered_name:
        blocks.extend(
            [
                "    form = ProfileForm(request.POST or None, instance=request.user)",
                '    if request.method == "POST" and form.is_valid():',
                "        form.save()",
                '        return redirect("user-profile-page")',
                f'    return render(request, "{template}", {{"form": form}})',
                "",
            ]
        )
        return blocks
    if "change password" in lowered_name:
        blocks.extend(
            [
                "    form = PasswordChangeForm(request.user, request.POST or None)",
                '    if request.method == "POST" and form.is_valid():',
                "        user = form.save()",
                "        update_session_auth_hash(request, user)",
                '        return redirect("dashboard")',
                '    return render(request, "resource_form.html", {"form": form})',
                "",
            ]
        )
        return blocks
    if page.page_type == "dashboard":
        context_items: list[str] = []
        for entity in entities:
            plural = _plural_name(_to_snake_case(entity.name))
            user_field = _user_field(entity)
            query = (
                f"{entity.name}.objects.filter({user_field}=request.user)"
                if user_field
                else f"{entity.name}.objects.all()"
            )
            blocks.append(f"    {plural} = {query}")
            context_items.append(f'"{plural}": {plural}')
        context = "{" + ", ".join(context_items) + "}"
        blocks.append(f'    return render(request, "{template}", {context})')
        blocks.append("")
        return blocks
    if resource:
        object_name = _to_snake_case(resource.name)
        plural = _plural_name(object_name)
        user_field = _user_field(resource)
        owner_filter = f", {user_field}=request.user" if user_field else ""
        if page.page_type == "detail":
            blocks[1 if decorator else 0] = f"def {function_name}(request, pk):"
            blocks.append(
                f"    {object_name} = get_object_or_404({resource.name}, pk=pk{owner_filter})"
            )
            blocks.append(f'    return render(request, "{template}", {{"{object_name}": {object_name}}})')
        elif page.page_type == "form":
            form_name = f"{resource.name}Form"
            form_user = ", user=request.user" if user_field else ""
            blocks.extend(
                [
                    f"    form = {form_name}(request.POST or None{form_user})",
                    "    if request.method == \"POST\" and form.is_valid():",
                    "        instance = form.save(commit=False)",
                    *([f"        instance.{user_field} = request.user"] if user_field else []),
                    "        instance.save()",
                    f'        return redirect("{_resource_url_name(resource.name)}-list")',
                    f'    return render(request, "{template}", {{"form": form}})',
                ]
            )
        else:
            query = (
                f"{resource.name}.objects.filter({user_field}=request.user)"
                if user_field
                else f"{resource.name}.objects.all()"
            )
            blocks.append(f"    {plural} = {query}")
            form_name = f"{resource.name}Form"
            form_user = ", user=request.user" if user_field else ""
            blocks.extend(
                [
                    f"    form = {form_name}(request.POST or None{form_user})",
                    '    if request.method == "POST" and form.is_valid():',
                    "        instance = form.save(commit=False)",
                    *([f"        instance.{user_field} = request.user"] if user_field else []),
                    "        instance.save()",
                    f'        return redirect("{_resource_url_name(resource.name)}-list")',
                    f'    return render(request, "{template}", '
                    f'{{"{plural}": {plural}, "objects": {plural}, "form": form}})',
                ]
            )
    else:
        blocks.append(f'    return render(request, "{template}")')
    blocks.append("")
    return blocks


def _render_delete_view(entity: EntitySpec) -> list[str]:
    object_name = _to_snake_case(entity.name)
    list_url = f"{_resource_url_name(entity.name)}-list"
    user_field = _user_field(entity)
    owner_filter = f", {user_field}=request.user" if user_field else ""
    return [
        "@login_required",
        f"def {_to_snake_case(entity.name)}_delete(request, pk):",
        f"    {object_name} = get_object_or_404({entity.name}, pk=pk{owner_filter})",
        "    if request.method == \"POST\" or request.method == \"DELETE\":",
        f"        {object_name}.delete()",
        f"    return redirect(\"{list_url}\")",
        "",
    ]


def _render_register_view() -> list[str]:
    return [
        "def register(request):",
        "    form = RegistrationForm(request.POST or None)",
        "    if request.method == \"POST\" and form.is_valid():",
        "        form.save()",
        "        return redirect(\"login\")",
        '    return render(request, "register.html", {"form": form})',
        "",
    ]


def _business_entities(project: ProjectSpec) -> list[EntitySpec]:
    return [
        entity
        for entity in project.entities
        if entity.name.lower() not in {"user", "session"}
    ]


def _render_account_views() -> list[str]:
    return [
        "@login_required",
        "@require_POST",
        "def delete_account(request):",
        "    user = request.user",
        "    auth_logout(request)",
        "    user.delete()",
        '    return redirect("landing-page")',
        "",
    ]


def _render_api_account_views(entities: list[EntitySpec]) -> list[str]:
    has_tasks = any(entity.name == "Task" for entity in entities)
    lines = [
        '@api_view(["POST"])',
        "@permission_classes([AllowAny])",
        "def api_register(request):",
        '    full_name = str(request.data.get("fullName", "")).strip()',
        '    email = str(request.data.get("email", "")).strip().lower()',
        '    password = str(request.data.get("password", ""))',
        '    confirmation = str(request.data.get("confirmPassword", ""))',
        "    errors = {}",
        '    if not full_name or len(full_name) > 100: errors["fullName"] = "Enter a name up to 100 characters."',
        '    if not email or "@" not in email: errors["email"] = "Enter a valid email address."',
        '    elif get_user_model().objects.filter(email__iexact=email).exists(): errors["email"] = "Email is already registered."',
        '    if password != confirmation: errors["confirmPassword"] = "Passwords do not match."',
        "    try:",
        "        validate_password(password)",
        "    except DjangoValidationError as exc:",
        '        errors["password"] = list(exc.messages)',
        "    if errors:",
        "        return Response(errors, status=http_status.HTTP_400_BAD_REQUEST)",
        "    user = get_user_model().objects.create_user(username=email, email=email, password=password, first_name=full_name)",
        "    return Response(_safe_user(user), status=http_status.HTTP_201_CREATED)",
        "",
        '@api_view(["POST"])',
        "@permission_classes([AllowAny])",
        "def api_login(request):",
        '    email = str(request.data.get("email", "")).strip().lower()',
        '    password = str(request.data.get("password", ""))',
        "    candidate = get_user_model().objects.filter(email__iexact=email).first()",
        "    user = authenticate(request, username=candidate.username if candidate else email, password=password)",
        "    if user is None:",
        '        return Response({"detail": "Invalid email or password."}, status=http_status.HTTP_401_UNAUTHORIZED)',
        "    auth_login(request, user)",
        "    refresh = RefreshToken.for_user(user)",
        '    return Response({"user": _safe_user(user), "refresh": str(refresh), "access": str(refresh.access_token)})',
        "",
        '@api_view(["POST"])',
        "@permission_classes([IsAuthenticated])",
        "def api_logout(request):",
        "    auth_logout(request)",
        "    return Response(status=http_status.HTTP_204_NO_CONTENT)",
        "",
        '@api_view(["GET"])',
        "@permission_classes([IsAuthenticated])",
        "def api_current_user(request):",
        "    return Response(_safe_user(request.user))",
        "",
        '@api_view(["GET", "PATCH", "DELETE"])',
        "@permission_classes([IsAuthenticated])",
        "def api_profile(request):",
        '    if request.method == "GET":',
        "        return Response(_safe_user(request.user))",
        '    if request.method == "DELETE":',
        "        user = request.user",
        "        auth_logout(request)",
        "        user.delete()",
        "        return Response(status=http_status.HTTP_204_NO_CONTENT)",
        "    form = ProfileForm(request.data, instance=request.user)",
        "    if not form.is_valid():",
        "        return Response(form.errors, status=http_status.HTTP_400_BAD_REQUEST)",
        "    return Response(_safe_user(form.save()))",
        "",
        '@api_view(["POST"])',
        "@permission_classes([IsAuthenticated])",
        "def api_change_password(request):",
        '    old_password = str(request.data.get("currentPassword", ""))',
        '    new_password = str(request.data.get("newPassword", ""))',
        '    confirmation = str(request.data.get("confirmPassword", ""))',
        "    if not request.user.check_password(old_password):",
        '        return Response({"currentPassword": "Incorrect password."}, status=http_status.HTTP_400_BAD_REQUEST)',
        "    if new_password != confirmation:",
        '        return Response({"confirmPassword": "Passwords do not match."}, status=http_status.HTTP_400_BAD_REQUEST)',
        "    try:",
        "        validate_password(new_password, request.user)",
        "    except DjangoValidationError as exc:",
        '        return Response({"newPassword": list(exc.messages)}, status=http_status.HTTP_400_BAD_REQUEST)',
        "    request.user.set_password(new_password)",
        "    request.user.save(update_fields=['password'])",
        "    update_session_auth_hash(request._request, request.user)",
        "    return Response(status=http_status.HTTP_204_NO_CONTENT)",
        "",
        '@api_view(["GET"])',
        "@permission_classes([IsAuthenticated])",
        "def api_dashboard_statistics(request):",
    ]
    if has_tasks:
        lines.extend(
            [
                "    tasks = Task.objects.filter(user=request.user)",
                "    return Response({",
                '        "total": tasks.count(),',
                '        "pending": tasks.filter(status="pending").count(),',
                '        "inProgress": tasks.filter(status="in_progress").count(),',
                '        "completed": tasks.filter(status="completed").count(),',
                '        "overdue": tasks.filter(due_at__lt=timezone.now()).exclude(status="completed").count(),',
                "    })",
            ]
        )
    else:
        lines.append('    return Response({"total": 0})')
    lines.extend(
        [
            "",
            "def _safe_user(user):",
            "    return {",
            '        "id": user.pk,',
            '        "fullName": user.get_full_name() or user.username,',
            '        "email": user.email,',
            '        "isActive": user.is_active,',
            "    }",
            "",
        ]
    )
    return lines


def _display_field(entity: EntitySpec) -> str:
    for candidate in ("name", "title", "username", "email"):
        if any(field.name == candidate for field in entity.fields):
            return candidate
    return entity.fields[0].name if entity.fields else "pk"


def _user_field(entity: EntitySpec) -> str | None:
    for field in entity.fields:
        if field.django_type == "ForeignKey" and field.kwargs.get("to") == "User":
            return field.name
    return None


def _find_entity(name: str | None, entities: list[EntitySpec]) -> EntitySpec | None:
    if not name:
        return None
    for entity in entities:
        if entity.name == name:
            return entity
    return None


def _page_function_name(page: PageSpec) -> str:
    if page.page_type == "dashboard":
        return "dashboard"
    if page.resource:
        return f"{_resource_url_name(page.resource)}_{page.page_type}"
    return _to_snake_case(page.name)


def _page_url_name(page: PageSpec) -> str:
    if page.page_type == "dashboard":
        return "dashboard"
    if page.resource:
        return f"{_resource_url_name(page.resource)}-{page.page_type}"
    return _to_kebab_case(page.name)


def _page_route(page: PageSpec) -> str:
    if page.page_type == "dashboard":
        return "dashboard/"
    if page.resource:
        base = f"{_resource_slug(page.resource)}/"
        if page.page_type == "detail":
            return f"{base}<int:pk>/"
        if page.page_type == "form":
            return f"{base}new/"
        return base
    return f"{_to_kebab_case(page.name)}/"


def _page_template(page: PageSpec) -> str:
    if page.page_type == "dashboard":
        return "dashboard.html"
    if page.page_type == "detail":
        return "resource_detail.html"
    if page.page_type == "form":
        return "resource_form.html"
    if page.page_type == "list" and page.resource:
        return f"{_to_kebab_case(page.resource)}/list.html"
    return "resource_list.html"


def _resource_slug(text: str) -> str:
    return _plural_name(_to_kebab_case(text))


def _resource_url_name(text: str) -> str:
    return _to_kebab_case(text)


def _plural_name(text: str) -> str:
    if text.endswith("y"):
        return f"{text[:-1]}ies"
    if text.endswith("s"):
        return text
    return f"{text}s"


def _display_name(text: str) -> str:
    return text.replace("_", " ").replace("-", " ").title()


def _to_snake_case(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.strip())
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.strip("_").lower()


def _to_kebab_case(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
