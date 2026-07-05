"""Renderer for fixed Django templates."""
from __future__ import annotations

import re
import secrets

from shamsu.templates.django.constants import (
    APP_CONFIG_TEMPLATE,
    ASGI_TEMPLATE,
    BASE_HTML_TEMPLATE,
    ENV_EXAMPLE_TEMPLATE,
    LOGIN_HTML_TEMPLATE,
    MANAGE_TEMPLATE,
    PROJECT_URLS_TEMPLATE,
    REGISTER_HTML_TEMPLATE,
    REQUIREMENTS_TEMPLATE,
    RESOURCE_ITEM_HTML_TEMPLATE,
    RESOURCE_LIST_HTML_TEMPLATE,
    SETTINGS_TEMPLATE,
    WSGI_TEMPLATE,
)
from shamsu.types import EntitySpec, PageSpec, ProjectSpec

PLACEHOLDER_RE = re.compile(r"\{\{\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_template(template: str, values: dict[str, object]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in values:
            return match.group(0)
        return str(values[name])

    return PLACEHOLDER_RE.sub(replace, template)


def render_fixed_django_files(
    project: ProjectSpec,
    secret_key: str | None = None,
) -> dict[str, str]:
    secret = secret_key or secrets.token_urlsafe(48)
    values = {
        "project_name": project.project_name,
        "display_name": _display_name(project.project_name),
        "app_name": project.app_name,
        "theme": project.theme,
        "secret_key": secret,
        "nav_links": _render_nav_links(project.pages),
        "app_config_class": f"{_to_pascal_case(project.app_name)}Config",
    }

    files = {
        "manage.py": render_template(MANAGE_TEMPLATE, values),
        f"{project.project_name}/__init__.py": "",
        f"{project.project_name}/settings.py": render_template(SETTINGS_TEMPLATE, values),
        f"{project.project_name}/urls.py": render_template(PROJECT_URLS_TEMPLATE, values),
        f"{project.project_name}/wsgi.py": render_template(WSGI_TEMPLATE, values),
        f"{project.project_name}/asgi.py": render_template(ASGI_TEMPLATE, values),
        f"{project.app_name}/__init__.py": "",
        f"{project.app_name}/apps.py": render_template(APP_CONFIG_TEMPLATE, values),
        f"{project.app_name}/templates/base.html": render_template(BASE_HTML_TEMPLATE, values),
        f"{project.app_name}/templates/login.html": render_template(LOGIN_HTML_TEMPLATE, values),
        f"{project.app_name}/templates/register.html": render_template(
            REGISTER_HTML_TEMPLATE,
            values,
        ),
        "requirements.txt": render_template(REQUIREMENTS_TEMPLATE, values),
        ".env.example": render_template(ENV_EXAMPLE_TEMPLATE, values),
    }
    files.update(_render_resource_template_files(project))
    return files


def _render_resource_template_files(project: ProjectSpec) -> dict[str, str]:
    files: dict[str, str] = {}
    entities_by_name = {entity.name.lower(): entity for entity in project.entities}
    for page in project.pages:
        if page.page_type != "list" or not page.resource:
            continue
        resource_key = _to_kebab_case(page.resource)
        if not resource_key:
            continue
        entity = entities_by_name.get(page.resource.lower())
        if entity is None:
            continue
        field_names = _resource_field_names(page, entity)
        resource_label = _display_name(page.resource)
        values = {
            "resource_label": resource_label,
            "resource_label_lower": resource_label.lower(),
            "resource_label_plural": _pluralize_label(resource_label),
            "resource_label_plural_lower": _pluralize_label(resource_label).lower(),
            "resource_url_name": f"{resource_key}-list",
            "resource_delete_url_name": f"{resource_key}-delete",
            "resource_template_dir": resource_key,
            "partial_template_path": f"{resource_key}/_item.html",
            "modal_id": f"{resource_key}-modal",
            "table_body_id": f"{resource_key}-rows",
            "row_id_prefix": resource_key,
            "table_headers": _render_table_headers(field_names),
            "table_cells": _render_table_cells(field_names),
            "table_colspan": len(field_names) + 1,
        }
        files[f"{project.app_name}/templates/{resource_key}/list.html"] = render_template(
            RESOURCE_LIST_HTML_TEMPLATE,
            values,
        )
        files[f"{project.app_name}/templates/{resource_key}/_item.html"] = render_template(
            RESOURCE_ITEM_HTML_TEMPLATE,
            values,
        )
    return files


def _resource_field_names(page: PageSpec, entity: EntitySpec | None) -> list[str]:
    if page.fields_shown:
        return [_to_snake_case(field_name) for field_name in page.fields_shown]
    if entity is not None and entity.fields:
        return [field.name for field in entity.fields]
    return ["id"]


def _render_table_headers(field_names: list[str]) -> str:
    return "\n".join(f"        <th>{_display_name(field_name)}</th>" for field_name in field_names)


def _render_table_cells(field_names: list[str]) -> str:
    return "\n".join(f"  <td>{{{{ object.{field_name} }}}}</td>" for field_name in field_names)


def _render_nav_links(pages: list[PageSpec]) -> str:
    links: list[str] = []
    seen: set[str] = set()
    for page in pages:
        if page.page_type == "auth":
            continue
        url_name = _url_name(page)
        if url_name in seen:
            continue
        seen.add(url_name)
        links.append(f'<li><a href="{{% url \'{url_name}\' %}}">{page.name}</a></li>')
    return "\n        ".join(links)


def _url_name(page: PageSpec) -> str:
    if page.page_type == "dashboard":
        return "dashboard"
    if page.resource:
        return f"{_to_kebab_case(page.resource)}-list"
    return _to_kebab_case(page.name)


def _display_name(project_name: str) -> str:
    return project_name.replace("_", " ").replace("-", " ").title()


def _to_pascal_case(text: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", text) if part)


def _pluralize_label(text: str) -> str:
    if text.endswith("y"):
        return f"{text[:-1]}ies"
    if text.endswith("s"):
        return text
    return f"{text}s"


def _to_kebab_case(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _to_snake_case(text: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "_", text.strip())
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return text.strip("_").lower() or "field"
