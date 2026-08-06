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
        blocks.extend(_test_case_block(entity, entities))
    blocks.extend(_authentication_test_block())
    if not entities:
        blocks.extend(["class GeneratedProjectSmokeTests(TestCase):", "    def test_smoke(self):", "        self.assertTrue(True)", ""])
    return "\n".join(blocks).rstrip() + "\n"


def _test_case_block(entity: EntitySpec, entities: list[EntitySpec]) -> list[str]:
    class_name = f"{entity.name}ApiTests"
    object_name = _to_snake_case(entity.name)
    factory_kwargs = _factory_kwargs(entity)
    other_factory_kwargs = _factory_kwargs(entity, user_expression="self.other_user")
    create_data = _api_payload(entity, prefix="Created")
    blocks = [
        f"class {class_name}(TestCase):",
        "    def setUp(self):",
        "        self.user = get_user_model().objects.create_user(username='tester', password='pass12345')",
        "        self.other_user = get_user_model().objects.create_user(username='other', password='pass12345')",
        "        self.client = APIClient()",
        "        self.client.force_authenticate(self.user)",
        f"        self.{object_name} = {entity.name}.objects.create({factory_kwargs})",
        f"        self.other_{object_name} = {entity.name}.objects.create({other_factory_kwargs})",
        "",
        "    def test_list(self):",
        f"        response = self.client.get(reverse('api:{_resource_url_name(entity.name)}-list'))",
        "        self.assertEqual(response.status_code, 200)",
        "        records = response.data.get('results', response.data)",
        f"        self.assertEqual([record['id'] for record in records], [self.{object_name}.id])",
        "",
        "    def test_create(self):",
        f"        response = self.client.post(reverse('api:{_resource_url_name(entity.name)}-list'), data={create_data!r}, format='json')",
        "        self.assertEqual(response.status_code, 201)",
        "        self.assertEqual(response.data.get('user'), None)",
        "",
        "    def test_retrieve(self):",
        f"        response = self.client.get(reverse('api:{_resource_url_name(entity.name)}-detail', args=[self.{object_name}.pk]))",
        "        self.assertEqual(response.status_code, 200)",
        "",
        "    def test_cannot_retrieve_another_users_record(self):",
        f"        response = self.client.get(reverse('api:{_resource_url_name(entity.name)}-detail', args=[self.other_{object_name}.pk]))",
        "        self.assertEqual(response.status_code, 404)",
        "",
        "    def test_update(self):",
        f"        response = self.client.put(reverse('api:{_resource_url_name(entity.name)}-detail', args=[self.{object_name}.pk]), data={create_data!r}, format='json')",
        "        self.assertEqual(response.status_code, 200)",
        "",
        "    def test_delete(self):",
        f"        response = self.client.delete(reverse('api:{_resource_url_name(entity.name)}-detail', args=[self.{object_name}.pk]))",
        "        self.assertEqual(response.status_code, 204)",
        "",
        "    def test_html_delete_rejects_another_users_record(self):",
        "        self.client.force_login(self.user)",
        f"        response = self.client.post(reverse('{_resource_url_name(entity.name)}-delete', args=[self.other_{object_name}.pk]))",
        "        self.assertEqual(response.status_code, 404)",
        "",
    ]
    if entity.name == "Task":
        blocks.extend(
            [
                "    def test_complete_and_reopen(self):",
                "        complete = self.client.post(reverse('api:task-complete', args=[self.task.pk]))",
                "        self.assertEqual(complete.status_code, 200)",
                "        self.task.refresh_from_db()",
                "        self.assertEqual(self.task.status, 'completed')",
                "        self.assertIsNotNone(self.task.completed_at)",
                "        reopen = self.client.post(reverse('api:task-reopen', args=[self.task.pk]))",
                "        self.assertEqual(reopen.status_code, 200)",
                "        self.task.refresh_from_db()",
                "        self.assertEqual(self.task.status, 'pending')",
                "        self.assertIsNone(self.task.completed_at)",
                "",
            ]
        )
        if any(item.name == "Category" for item in entities):
            blocks.extend(
                [
                    "    def test_rejects_another_users_category(self):",
                    "        category = Category.objects.create(user=self.other_user, name='Private')",
                    f"        data = {create_data!r}",
                    "        data['category'] = category.pk",
                    "        response = self.client.post(reverse('api:task-list'), data=data, format='json')",
                    "        self.assertEqual(response.status_code, 400)",
                    "",
                    "    def test_deleting_category_keeps_task_and_sets_null(self):",
                    "        category = Category.objects.create(user=self.user, name='Temporary')",
                    "        self.task.category = category",
                    "        self.task.save(update_fields=['category'])",
                    "        response = self.client.delete(reverse('api:category-detail', args=[category.pk]))",
                    "        self.assertEqual(response.status_code, 204)",
                    "        self.task.refresh_from_db()",
                    "        self.assertIsNone(self.task.category)",
                    "",
                ]
            )
    return blocks


def _authentication_test_block() -> list[str]:
    return [
        "class AuthenticationTests(TestCase):",
        "    def test_root_route_is_available(self):",
        "        response = self.client.get(reverse('home'))",
        "        self.assertIn(response.status_code, (200, 302))",
        "",
        "    def test_registration_hashes_password_and_normalizes_email(self):",
        "        response = self.client.post(reverse('register'), data={",
        "            'full_name': 'Example User',",
        "            'email': 'USER@Example.COM',",
        "            'username': 'example',",
        "            'password1': 'StrongPass123!',",
        "            'password2': 'StrongPass123!',",
        "        })",
        "        self.assertEqual(response.status_code, 302)",
        "        user = get_user_model().objects.get(username='example')",
        "        self.assertEqual(user.email, 'user@example.com')",
        "        self.assertTrue(user.check_password('StrongPass123!'))",
        "",
        "    def test_duplicate_email_is_rejected(self):",
        "        get_user_model().objects.create_user(username='existing', email='user@example.com')",
        "        response = self.client.post(reverse('register'), data={",
        "            'full_name': 'Example User',",
        "            'email': 'USER@example.com',",
        "            'username': 'example',",
        "            'password1': 'StrongPass123!',",
        "            'password2': 'StrongPass123!',",
        "        })",
        "        self.assertEqual(response.status_code, 200)",
        "        self.assertContains(response, 'already exists')",
        "",
        "    def test_dashboard_requires_login(self):",
        "        response = self.client.get(reverse('dashboard'))",
        "        self.assertEqual(response.status_code, 302)",
        "",
        "    def test_api_registration_and_email_login(self):",
        "        client = APIClient()",
        "        registration = client.post(reverse('api-register'), data={",
        "            'fullName': 'API User',",
        "            'email': 'api@example.com',",
        "            'password': 'StrongPass123!',",
        "            'confirmPassword': 'StrongPass123!',",
        "        }, format='json')",
        "        self.assertEqual(registration.status_code, 201)",
        "        self.assertNotIn('password', registration.data)",
        "        login = client.post(reverse('api-login'), data={",
        "            'email': 'api@example.com',",
        "            'password': 'StrongPass123!',",
        "        }, format='json')",
        "        self.assertEqual(login.status_code, 200)",
        "        self.assertIn('access', login.data)",
        "",
        "    def test_current_user_api_requires_authentication(self):",
        "        response = APIClient().get(reverse('api-current-user'))",
        "        self.assertIn(response.status_code, {401, 403})",
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


def _factory_kwargs(entity: EntitySpec, user_expression: str = "self.user") -> str:
    values = []
    for field in entity.fields:
        if field.django_type == "ManyToManyField":
            continue
        if field.kwargs.get("auto_now") or field.kwargs.get("auto_now_add"):
            continue
        if field.django_type == "ForeignKey" and field.kwargs.get("to") == "User":
            values.append(f"{field.name}={user_expression}")
        elif field.django_type != "ForeignKey":
            values.append(f"{field.name}={_sample_value(field)!r}")
    return ", ".join(values)


def _api_payload(entity: EntitySpec, prefix: str = "Sample") -> dict[str, object]:
    payload: dict[str, object] = {}
    for field in entity.fields:
        if field.django_type in {"ForeignKey", "ManyToManyField"}:
            continue
        if field.kwargs.get("auto_now") or field.kwargs.get("auto_now_add"):
            continue
        payload[field.name] = _sample_value(field, prefix=prefix)
    return payload


def _sample_value(field: EntityFieldSpec, prefix: str = "Sample") -> object:
    choices = field.kwargs.get("choices")
    if isinstance(choices, list) and choices:
        return choices[0]
    if field.django_type == "BooleanField":
        return True
    if field.django_type in {"IntegerField", "PositiveIntegerField"}:
        return 1
    if field.django_type == "DecimalField":
        return "12.50"
    if field.django_type in {"DateField", "DateTimeField"}:
        return "2026-01-01T12:00:00Z" if field.django_type == "DateTimeField" else "2026-01-01"
    if field.django_type == "EmailField":
        return "user@example.com"
    return f"{prefix} {_display_name(field.name)}"


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
