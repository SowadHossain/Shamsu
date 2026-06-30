"""Renderer for fixed Django templates."""
from __future__ import annotations

import re
import secrets

from shamsu.templates.django.constants import (
    ASGI_TEMPLATE,
    BASE_HTML_TEMPLATE,
    ENV_EXAMPLE_TEMPLATE,
    LOGIN_HTML_TEMPLATE,
    MANAGE_TEMPLATE,
    PROJECT_URLS_TEMPLATE,
    REGISTER_HTML_TEMPLATE,
    REQUIREMENTS_TEMPLATE,
    SETTINGS_TEMPLATE,
    WSGI_TEMPLATE,
)
from shamsu.types import PageSpec, ProjectSpec

PLACEHOLDER_RE = re.compile(r"\{\{\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_template(template: str, values: dict[str, object]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in values:
            return match.group(0)
        return str(values[name])

    return PLACEHOLDER_RE.sub(replace, template)


def render_fixed_django_files(project: ProjectSpec, secret_key: str | None = None) -> dict[str, str]:
    secret = secret_key or secrets.token_urlsafe(48)
    values = {
        "project_name": project.project_name,
        "display_name": _display_name(project.project_name),
        "app_name": project.app_name,
        "theme": project.theme,
        "secret_key": secret,
        "nav_links": _render_nav_links(project.pages),
    }

    return {
        "manage.py": render_template(MANAGE_TEMPLATE, values),
        f"{project.project_name}/settings.py": render_template(SETTINGS_TEMPLATE, values),
        f"{project.project_name}/urls.py": render_template(PROJECT_URLS_TEMPLATE, values),
        f"{project.project_name}/wsgi.py": render_template(WSGI_TEMPLATE, values),
        f"{project.project_name}/asgi.py": render_template(ASGI_TEMPLATE, values),
        f"{project.app_name}/templates/base.html": render_template(BASE_HTML_TEMPLATE, values),
        f"{project.app_name}/templates/login.html": render_template(LOGIN_HTML_TEMPLATE, values),
        f"{project.app_name}/templates/register.html": render_template(REGISTER_HTML_TEMPLATE, values),
        "requirements.txt": render_template(REQUIREMENTS_TEMPLATE, values),
        ".env.example": render_template(ENV_EXAMPLE_TEMPLATE, values),
    }


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


def _to_kebab_case(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
