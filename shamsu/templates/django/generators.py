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
        fields = ["id", *[field.name for field in entity.fields]]
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
        "",
        f"from .models import {', '.join(names)}" if names else "from . import models",
        "",
    ]
    for entity in _business_entities(project):
        fields = [field.name for field in entity.fields]
        blocks.extend(
            [
                f"class {entity.name}Form(forms.ModelForm):",
                "    class Meta:",
                f"        model = {entity.name}",
                f"        fields = {fields!r}",
                "",
            ]
        )
    return "\n".join(blocks).rstrip() + "\n"


def render_views(project: ProjectSpec) -> str:
    entities = _business_entities(project)
    names = [entity.name for entity in entities]
    serializer_names = [f"{entity.name}Serializer" for entity in entities]
    form_names = [f"{entity.name}Form" for entity in entities]
    blocks = [
        "from django.contrib.auth.forms import UserCreationForm",
        "from django.contrib.auth.decorators import login_required",
        "from django.shortcuts import redirect, render",
        "from rest_framework.permissions import IsAuthenticated",
        "from rest_framework.viewsets import ModelViewSet",
        "",
        f"from .forms import {', '.join(form_names)}" if form_names else "from . import forms",
        f"from .models import {', '.join(names)}" if names else "from . import models",
        (
            f"from .serializers import {', '.join(serializer_names)}"
            if serializer_names
            else "from . import serializers"
        ),
        "",
    ]
    for entity in entities:
        blocks.extend(_render_viewset(entity))
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
    blocks.extend(["", "urlpatterns = [", '    path("api/", include(router.urls)),'])
    seen: set[str] = set()
    for page in project.pages:
        name = _page_url_name(page)
        view = _page_function_name(page)
        route = _page_route(page)
        if name in seen:
            continue
        seen.add(name)
        blocks.append(f'    path("{route}", views.{view}, name="{name}"),')
    if "dashboard" not in seen:
        blocks.append('    path("dashboard/", views.dashboard, name="dashboard"),')
    blocks.extend(
        [
            '    path("login/", auth_views.LoginView.as_view(template_name="login.html"), name="login"),',
            '    path("logout/", auth_views.LogoutView.as_view(), name="logout"),',
            '    path("register/", views.register, name="register"),',
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


def _render_kwargs(kwargs: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in kwargs.items():
        if key == "choices" and isinstance(value, list):
            choices = [(choice, _display_name(choice)) for choice in value]
            parts.append(f"{key}={choices!r}")
        elif isinstance(value, str) and value == "CASCADE":
            parts.append(f"{key}=models.CASCADE")
        else:
            parts.append(f"{key}={value!r}")
    return ", ".join(parts)


def _render_viewset(entity: EntitySpec) -> list[str]:
    blocks = [
        f"class {entity.name}ViewSet(ModelViewSet):",
        f"    serializer_class = {entity.name}Serializer",
        "    permission_classes = [IsAuthenticated]",
    ]
    if _user_field(entity):
        blocks.extend(
            [
                "",
                "    def get_queryset(self):",
                f"        return {entity.name}.objects.filter({_user_field(entity)}=self.request.user)",
                "",
                "    def perform_create(self, serializer):",
                f"        serializer.save({_user_field(entity)}=self.request.user)",
                "",
            ]
        )
    else:
        blocks.extend([f"    queryset = {entity.name}.objects.all()", ""])
    return blocks


def _render_page_view(page: PageSpec, entities: list[EntitySpec]) -> list[str]:
    function_name = _page_function_name(page)
    decorator = ["@login_required"] if page.requires_login else []
    template = _page_template(page)
    resource = _find_entity(page.resource, entities)
    blocks = [*decorator, f"def {function_name}(request):"]
    if resource:
        object_name = _to_snake_case(resource.name)
        plural = _plural_name(object_name)
        if page.page_type == "detail":
            blocks[1 if decorator else 0] = f"def {function_name}(request, pk):"
            blocks.append(f"    {object_name} = {resource.name}.objects.get(pk=pk)")
            blocks.append(f'    return render(request, "{template}", {{"{object_name}": {object_name}}})')
        elif page.page_type == "form":
            form_name = f"{resource.name}Form"
            blocks.extend(
                [
                    f"    form = {form_name}(request.POST or None)",
                    "    if request.method == \"POST\" and form.is_valid():",
                    "        form.save()",
                    f'        return redirect("{_resource_url_name(resource.name)}-list")',
                    f'    return render(request, "{template}", {{"form": form}})',
                ]
            )
        else:
            blocks.append(f"    {plural} = {resource.name}.objects.all()")
            blocks.append(f'    return render(request, "{template}", {{"{plural}": {plural}}})')
    else:
        blocks.append(f'    return render(request, "{template}")')
    blocks.append("")
    return blocks


def _render_register_view() -> list[str]:
    return [
        "def register(request):",
        "    form = UserCreationForm(request.POST or None)",
        "    if request.method == \"POST\" and form.is_valid():",
        "        form.save()",
        "        return redirect(\"login\")",
        '    return render(request, "register.html", {"form": form})',
        "",
    ]


def _business_entities(project: ProjectSpec) -> list[EntitySpec]:
    return [entity for entity in project.entities if entity.name.lower() != "user"]


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
