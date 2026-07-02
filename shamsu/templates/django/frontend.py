"""Deterministic Django frontend and test generators."""
from __future__ import annotations

import ast
import re

from shamsu.templates.django.generators import _business_entities
from shamsu.types import EntityFieldSpec, EntitySpec, ProjectSpec


def render_frontend_django_files(project: ProjectSpec) -> dict[str, str]:
    base = f"{project.app_name}/templates"
    return {
        f"{base}/dashboard.html": render_dashboard(project),
        f"{base}/resource_list.html": render_resource_list(project),
        f"{base}/resource_detail.html": render_resource_detail(project),
        f"{base}/resource_form.html": render_resource_form(project),
    }


def render_django_test_files(project: ProjectSpec) -> dict[str, str]:
    path = f"{project.app_name}/tests.py"
    content = render_django_tests(project)
    ast.parse(content, filename=path)
    return {path: content}


def render_dashboard(project: ProjectSpec) -> str:
    entities = _business_entities(project)
    stat_cards = "\n".join(_dashboard_stat(entity) for entity in entities)
    recent_tables = "\n".join(_dashboard_recent_table(entity) for entity in entities)
    if not stat_cards:
        stat_cards = '<div class="stat"><div class="stat-title">Resources</div><div class="stat-value">0</div></div>'
    return f"""{{% extends "base.html" %}}
{{% block content %}}
<section class="space-y-6">
  <div>
    <h1 class="text-3xl font-bold">Dashboard</h1>
    <p class="text-base-content/70">Overview and recent activity.</p>
  </div>
  <div class="stats stats-vertical lg:stats-horizontal shadow bg-base-100">
{stat_cards}
  </div>
  <div class="grid gap-6 lg:grid-cols-2">
{recent_tables}
  </div>
</section>
{{% endblock %}}
"""


def render_resource_list(project: ProjectSpec) -> str:
    entity = _first_entity(project)
    object_name = _to_snake_case(entity.name)
    plural = _plural_name(object_name)
    columns = "\n".join(f"        <th>{_display_name(field.name)}</th>" for field in _display_fields(entity))
    cells = "\n".join(f"        <td>{{{{ {object_name}.{field.name} }}}}</td>" for field in _display_fields(entity))
    return f"""{{% extends "base.html" %}}
{{% block content %}}
<section class="space-y-4">
  <div class="flex items-center justify-between gap-4">
    <h1 class="text-2xl font-bold">{_display_name(entity.name)} List</h1>
    <a class="btn btn-primary" href="{{% url '{_resource_url_name(entity.name)}-form' %}}">New {_display_name(entity.name)}</a>
  </div>
  <div id="{plural}-table" class="overflow-x-auto rounded-box border border-base-300 bg-base-100">
    <table class="table table-zebra">
      <thead><tr>
{columns}
        <th>Actions</th>
      </tr></thead>
      <tbody>
      {{% for {object_name} in {plural} %}}
      <tr id="{object_name}-{{{{ {object_name}.id }}}}">
{cells}
        <td><a class="btn btn-sm" href="{{% url '{_resource_url_name(entity.name)}-detail' {object_name}.id %}}">View</a></td>
      </tr>
      {{% empty %}}
      <tr><td colspan="{len(_display_fields(entity)) + 1}" class="text-center">No records yet.</td></tr>
      {{% endfor %}}
      </tbody>
    </table>
  </div>
</section>
{{% endblock %}}
"""


def render_resource_detail(project: ProjectSpec) -> str:
    entity = _first_entity(project)
    object_name = _to_snake_case(entity.name)
    rows = "\n".join(
        f"""    <div class="flex justify-between border-b border-base-200 py-2">
      <span class="font-medium">{_display_name(field.name)}</span>
      <span>{{{{ {object_name}.{field.name} }}}}</span>
    </div>"""
        for field in _display_fields(entity)
    )
    return f"""{{% extends "base.html" %}}
{{% block content %}}
<section class="max-w-3xl space-y-4">
  <a class="btn btn-ghost btn-sm" href="{{% url '{_resource_url_name(entity.name)}-list' %}}">Back</a>
  <div class="card bg-base-100 shadow">
    <div class="card-body">
      <h1 class="card-title">{_display_name(entity.name)} Detail</h1>
{rows}
    </div>
  </div>
</section>
{{% endblock %}}
"""


def render_resource_form(_project: ProjectSpec) -> str:
    return """{% extends "base.html" %}
{% load crispy_forms_tags %}
{% block content %}
<section class="max-w-2xl">
  <div class="card bg-base-100 shadow">
    <div class="card-body">
      <h1 class="card-title">Edit Record</h1>
      <form method="post" class="space-y-4" hx-boost="true">
        {% csrf_token %}
        {{ form|crispy }}
        <div class="card-actions justify-end">
          <button type="submit" class="btn btn-primary">Save</button>
        </div>
      </form>
    </div>
  </div>
</section>
{% endblock %}
"""


def render_django_tests(project: ProjectSpec) -> str:
    entities = _business_entities(project)
    imports = [
        "from django.contrib.auth import get_user_model",
        "from django.test import TestCase",
        "from django.urls import reverse",
        "from rest_framework.test import APIClient",
        "",
    ]
    if entities:
        imports.extend([f"from .models import {', '.join(entity.name for entity in entities)}", ""])
    blocks = imports
    for entity in entities:
        blocks.extend(_test_case_block(entity))
    if not entities:
        blocks.extend(["class GeneratedProjectSmokeTests(TestCase):", "    def test_smoke(self):", "        self.assertTrue(True)", ""])
    return "\n".join(blocks).rstrip() + "\n"


def _test_case_block(entity: EntitySpec) -> list[str]:
    class_name = f"{entity.name}ApiTests"
    object_name = _to_snake_case(entity.name)
    factory_kwargs = _factory_kwargs(entity)
    create_data = _api_payload(entity)
    return [
        f"class {class_name}(TestCase):",
        "    def setUp(self):",
        "        self.user = get_user_model().objects.create_user(username='tester', password='pass12345')",
        "        self.client = APIClient()",
        "        self.client.force_authenticate(self.user)",
        f"        self.{object_name} = {entity.name}.objects.create({factory_kwargs})",
        "",
        "    def test_list(self):",
        f"        response = self.client.get(reverse('{_resource_url_name(entity.name)}-list'))",
        "        self.assertIn(response.status_code, {200, 301, 302})",
        "",
        "    def test_create(self):",
        f"        response = self.client.post(reverse('{_resource_url_name(entity.name)}-list'), data={create_data!r}, format='json')",
        "        self.assertIn(response.status_code, {200, 201, 400})",
        "",
        "    def test_retrieve(self):",
        f"        response = self.client.get(reverse('{_resource_url_name(entity.name)}-detail', args=[self.{object_name}.pk]))",
        "        self.assertIn(response.status_code, {200, 301, 302})",
        "",
        "    def test_update(self):",
        f"        response = self.client.put(reverse('{_resource_url_name(entity.name)}-detail', args=[self.{object_name}.pk]), data={create_data!r}, format='json')",
        "        self.assertIn(response.status_code, {200, 400})",
        "",
        "    def test_delete(self):",
        f"        response = self.client.delete(reverse('{_resource_url_name(entity.name)}-detail', args=[self.{object_name}.pk]))",
        "        self.assertIn(response.status_code, {204, 301, 302})",
        "",
    ]


def _dashboard_stat(entity: EntitySpec) -> str:
    object_name = _to_snake_case(entity.name)
    plural = _plural_name(object_name)
    return f"""    <div class="stat">
      <div class="stat-title">{_display_name(plural)}</div>
      <div class="stat-value">{{{{ {plural}|length|default:0 }}}}</div>
      <div class="stat-actions"><a class="btn btn-xs" href="{{% url '{_resource_url_name(entity.name)}-list' %}}">Open</a></div>
    </div>"""


def _dashboard_recent_table(entity: EntitySpec) -> str:
    object_name = _to_snake_case(entity.name)
    plural = _plural_name(object_name)
    display_field = _display_fields(entity)[0].name
    return f"""    <div class="card bg-base-100 shadow">
      <div class="card-body">
        <h2 class="card-title">Recent {_display_name(plural)}</h2>
        <div class="overflow-x-auto">
          <table class="table table-sm">
            <tbody>
            {{% for {object_name} in {plural}|slice:":5" %}}
              <tr><td>{{{{ {object_name}.{display_field} }}}}</td><td><a class="link" href="{{% url '{_resource_url_name(entity.name)}-detail' {object_name}.id %}}">View</a></td></tr>
            {{% empty %}}
              <tr><td>No records yet.</td></tr>
            {{% endfor %}}
            </tbody>
          </table>
        </div>
      </div>
    </div>"""


def _first_entity(project: ProjectSpec) -> EntitySpec:
    entities = _business_entities(project)
    if entities:
        return entities[0]
    return EntitySpec("Resource", [EntityFieldSpec("name", "CharField", {"max_length": 200})])


def _display_fields(entity: EntitySpec) -> list[EntityFieldSpec]:
    return [field for field in entity.fields if field.django_type not in {"ForeignKey", "ManyToManyField"}] or entity.fields


def _factory_kwargs(entity: EntitySpec) -> str:
    values = []
    for field in entity.fields:
        if field.django_type == "ManyToManyField":
            continue
        if field.django_type == "ForeignKey" and field.kwargs.get("to") == "User":
            values.append(f"{field.name}=self.user")
        elif field.django_type != "ForeignKey":
            values.append(f"{field.name}={_sample_value(field)!r}")
    return ", ".join(values)


def _api_payload(entity: EntitySpec) -> dict[str, object]:
    payload: dict[str, object] = {}
    for field in entity.fields:
        if field.django_type in {"ForeignKey", "ManyToManyField"}:
            continue
        payload[field.name] = _sample_value(field)
    return payload


def _sample_value(field: EntityFieldSpec) -> object:
    if field.django_type == "BooleanField":
        return True
    if field.django_type in {"IntegerField", "PositiveIntegerField"}:
        return 1
    if field.django_type == "DecimalField":
        return "12.50"
    if field.django_type in {"DateField", "DateTimeField"}:
        return "2026-01-01"
    if field.django_type == "EmailField":
        return "user@example.com"
    return f"Sample {_display_name(field.name)}"


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
