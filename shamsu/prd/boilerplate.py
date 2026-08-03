"""Deterministic framework boilerplate for PRD builds.

`templates/django/constants.py` states the rule plainly: these files "should be
generated with substitution only. Keeping them out of the LLM path saves tokens
and removes a large class of avoidable mistakes." The PRD milestone file pass
never honored it - it asked the model to write every expected file freeform.

The cost, measured: in SIX consecutive live 7B runs (2026-08-01/02) the model
wrote a `settings.py` that referenced `BASE_DIR` in DATABASES without ever
defining it. `python manage.py check` failed identically every time, M-001
failed, and all 23 milestones stayed pending. No bundled skill carries any
Django guidance, so the model was writing framework config from memory with no
reference - the one job where a 7B has nothing to add and everything to lose.

Only pure scaffolding is generated here. Product logic (models.py, views.py,
serializers) stays with the model, which handles it well - `models.py` came out
correct and complete in the same runs.

The templates are deliberately dependency-free (django.contrib only). The
milestone verifier runs `python manage.py check` against whatever Django is
installed, so pulling in rest_framework/simplejwt/crispy - as the full
`SETTINGS_TEMPLATE` does - would trade a NameError for a ModuleNotFoundError.
"""
from __future__ import annotations

import secrets
from collections.abc import Sequence
from pathlib import Path

from shamsu.templates.django.constants import (
    APP_CONFIG_TEMPLATE,
    ASGI_TEMPLATE,
    MANAGE_TEMPLATE,
    WSGI_TEMPLATE,
)
from shamsu.templates.django.renderer import render_template

SETTINGS_TEMPLATE = '''"""Django settings - generated deterministically by SHAMSU."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "{{ secret_key }}")
DEBUG = os.environ.get("DJANGO_DEBUG", "True").lower() == "true"
ALLOWED_HOSTS = [
    item
    for item in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if item
]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "{{ app_name }}",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "{{ project_name }}.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

{{ wsgi_application }}
{{ databases }}

AUTH_PASSWORD_VALIDATORS = []
{{ auth_user_model }}
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
'''

POSTGRES_DATABASES_TEMPLATE = '''DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("POSTGRES_DB", "openbazaar"),
        "USER": os.environ.get("POSTGRES_USER", "openbazaar"),
        "PASSWORD": os.environ.get("POSTGRES_PASSWORD", "openbazaar"),
        "HOST": os.environ.get("POSTGRES_HOST", "postgres"),
        "PORT": os.environ.get("POSTGRES_PORT", "5432"),
    }
}
'''

SQLITE_DATABASES_TEMPLATE = '''DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}
'''

DJANGO_REQUIREMENTS_TEMPLATE = """Django>=5.0,<6.0
psycopg[binary]>=3.1
"""

DJANGO_ENV_EXAMPLE = """DJANGO_SECRET_KEY=change-me
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,backend
POSTGRES_DB=openbazaar
POSTGRES_USER=openbazaar
POSTGRES_PASSWORD=openbazaar
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
"""

DJANGO_DOCKERFILE = """FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate && python manage.py runserver 0.0.0.0:8000"]
"""

NODE_PACKAGE_JSON = """{
  "scripts": {
    "dev": "node server.js",
    "start": "node server.js",
    "build": "node --check server.js",
    "test": "node --test"
  },
  "dependencies": {
    "better-sqlite3": "^9.0.0",
    "cors": "^2.8.5",
    "express": "^4.18.0"
  }
}
"""

NODE_ENV_EXAMPLE = """NODE_ENV=development
PORT=8000
DATABASE_URL=sqlite:./db.sqlite3
"""

NODE_DOCKERFILE = """FROM node:22-alpine

WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .

EXPOSE 8000
CMD ["npm", "start"]
"""

REACT_PACKAGE_JSON = """{
  "scripts": {
    "dev": "vite --host 0.0.0.0",
    "build": "vite build",
    "test": "vitest run"
  },
  "dependencies": {
    "@vitejs/plugin-react": "^4.0.0",
    "vite": "^5.0.0",
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "typescript": "^5.0.0"
  },
  "devDependencies": {
    "vitest": "^1.0.0"
  }
}
"""

REACT_INDEX_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>OpenBazaar</title>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/{{ entrypoint }}"></script>
  </body>
</html>
"""

REACT_VITE_CONFIG = """import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://backend:8000",
        changeOrigin: true
      }
    }
  },
  test: {
    environment: "jsdom"
  }
});
"""

REACT_ENV_EXAMPLE = """VITE_API_BASE_URL=/api
"""

REACT_DOCKERFILE = """FROM node:22-alpine

WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .

EXPOSE 5173
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
"""

ROOT_ENV_EXAMPLE = """POSTGRES_DB=openbazaar
POSTGRES_USER=openbazaar
POSTGRES_PASSWORD=openbazaar
DJANGO_SECRET_KEY=change-me
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,backend
VITE_API_BASE_URL=/api
"""

DOCKER_COMPOSE_TEMPLATE = """services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: openbazaar
      POSTGRES_USER: openbazaar
      POSTGRES_PASSWORD: openbazaar
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U openbazaar -d openbazaar"]
      interval: 5s
      timeout: 5s
      retries: 10

  backend:
    build:
      context: ./backend
    environment:
      DJANGO_SECRET_KEY: change-me
      DJANGO_DEBUG: "True"
      DJANGO_ALLOWED_HOSTS: localhost,127.0.0.1,backend
      POSTGRES_DB: openbazaar
      POSTGRES_USER: openbazaar
      POSTGRES_PASSWORD: openbazaar
      POSTGRES_HOST: postgres
      POSTGRES_PORT: "5432"
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy

  frontend:
    build:
      context: ./frontend
    environment:
      VITE_API_BASE_URL: /api
    ports:
      - "5173:5173"
    depends_on:
      - backend

volumes:
  postgres_data:
"""

PROJECT_URLS_TEMPLATE = '''"""Root URL configuration - generated deterministically by SHAMSU."""
from django.contrib import admin
from django.urls import path

urlpatterns = [
    path("admin/", admin.site.urls),
]
'''

PROJECT_URLS_WITH_APP_TEMPLATE = '''"""Root URL configuration - generated deterministically by SHAMSU."""
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("{{ app_name }}.urls")),
]
'''

APP_URLS_TEMPLATE = '''"""App URL configuration - generated deterministically by SHAMSU."""
from django.urls import path

urlpatterns = []
'''


def _posix(path: str) -> str:
    return str(path).replace("\\", "/").strip("/")


def _package_of(expected: Sequence[str], filename: str) -> str:
    """Directory name of the expected file called *filename* (e.g. settings.py
    -> its package, which is Django's project package)."""
    for item in expected:
        parts = Path(_posix(item)).parts
        if parts and parts[-1] == filename and len(parts) >= 2:
            return parts[-2]
    return ""


def _expects(expected: Sequence[str], package: str, filename: str) -> bool:
    """True when ``<package>/<filename>`` is one of the expected files."""
    target = (package, filename)
    for item in expected:
        parts = Path(_posix(item)).parts
        if len(parts) >= 2 and parts[-2:] == target:
            return True
    return False


def django_layout(expected_files: Sequence[str]) -> tuple[str, str]:
    """Return ``(project_package, app_package)`` inferred from expected files.

    The project package is whatever directory holds ``settings.py``; the app
    package is whatever holds ``models.py``. Empty strings when absent.
    """
    return (
        _package_of(expected_files, "settings.py"),
        _package_of(expected_files, "models.py"),
    )


def is_django_project(expected_files: Sequence[str]) -> bool:
    names = {Path(_posix(item)).name for item in expected_files}
    return "manage.py" in names and "settings.py" in names


def is_react_vite_project(expected_files: Sequence[str]) -> bool:
    expected = {_posix(item) for item in expected_files}
    names = {Path(item).name for item in expected}
    return "package.json" in names and "vite.config.ts" in names and any(
        _has_component_path(item, "frontend") for item in expected
    )


def is_node_express_project(expected_files: Sequence[str]) -> bool:
    expected = {_posix(item) for item in expected_files}
    return any(_endswith_component_path(item, "backend/package.json") for item in expected)


def uses_postgres_runtime(expected_files: Sequence[str]) -> bool:
    expected = {_posix(item) for item in expected_files}
    return any(Path(item).name == "docker-compose.yml" for item in expected) and any(
        _endswith_component_path(item, "backend/Dockerfile")
        or _endswith_component_path(item, "backend/.env.example")
        or Path(item).name == ".env.example"
        for item in expected
    )


def _has_component_path(path: str, component: str) -> bool:
    parts = Path(_posix(path)).parts
    return component in parts


def _endswith_component_path(path: str, suffix: str) -> bool:
    parts = Path(_posix(path)).parts
    suffix_parts = Path(_posix(suffix)).parts
    return len(parts) >= len(suffix_parts) and parts[-len(suffix_parts):] == suffix_parts


def _react_entrypoint(expected_files: Sequence[str]) -> str:
    expected = {_posix(item) for item in expected_files}
    for candidate in ("frontend/src/main.tsx", "frontend/src/main.jsx"):
        if any(_endswith_component_path(item, candidate) for item in expected):
            return candidate.removeprefix("frontend/")
    return "src/main.tsx"


def _docker_compose_content(expected_files: Sequence[str]) -> str:
    expected = {_posix(item) for item in expected_files}
    has_backend = any(_endswith_component_path(item, "backend/Dockerfile") for item in expected)
    has_frontend = any(_endswith_component_path(item, "frontend/Dockerfile") for item in expected)
    if has_backend and has_frontend:
        return DOCKER_COMPOSE_TEMPLATE
    sections = [
        """services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: openbazaar
      POSTGRES_USER: openbazaar
      POSTGRES_PASSWORD: openbazaar
    ports:
      - "5432:5432"
    volumes:
      - postgres_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U openbazaar -d openbazaar"]
      interval: 5s
      timeout: 5s
      retries: 10
""",
    ]
    if has_backend:
        sections.append(
            """
  backend:
    build:
      context: ./backend
    environment:
      DJANGO_SECRET_KEY: change-me
      DJANGO_DEBUG: "True"
      DJANGO_ALLOWED_HOSTS: localhost,127.0.0.1,backend
      POSTGRES_DB: openbazaar
      POSTGRES_USER: openbazaar
      POSTGRES_PASSWORD: openbazaar
      POSTGRES_HOST: postgres
      POSTGRES_PORT: "5432"
    ports:
      - "8000:8000"
    depends_on:
      postgres:
        condition: service_healthy
"""
        )
    sections.append(
        """
volumes:
  postgres_data:
"""
    )
    return "".join(sections)


def render_boilerplate(
    relative_path: str,
    expected_files: Sequence[str],
    *,
    custom_user_model: bool = False,
    secret_key: str = "",
) -> str | None:
    """Deterministic content for a Django scaffolding file, or None.

    None means "not boilerplate" - the caller falls back to the model, which is
    correct for models.py, views.py, and anything carrying product logic.
    """
    parts = Path(_posix(relative_path)).parts
    if not parts:
        return None
    normalized = "/".join(parts)
    name = parts[-1]
    parent = parts[-2] if len(parts) >= 2 else ""
    if uses_postgres_runtime(expected_files):
        if name == "docker-compose.yml":
            return _docker_compose_content(expected_files)
        if name == ".env.example" and not (
            _endswith_component_path(normalized, "backend/.env.example")
            or _endswith_component_path(normalized, "frontend/.env.example")
        ):
            return ROOT_ENV_EXAMPLE
    if is_react_vite_project(expected_files):
        if _endswith_component_path(normalized, "frontend/package.json"):
            return REACT_PACKAGE_JSON
        if _endswith_component_path(normalized, "frontend/index.html"):
            return render_template(
                REACT_INDEX_HTML_TEMPLATE,
                {"entrypoint": _react_entrypoint(expected_files)},
            )
        if _endswith_component_path(normalized, "frontend/vite.config.ts"):
            return REACT_VITE_CONFIG
        if _endswith_component_path(normalized, "frontend/.env.example"):
            return REACT_ENV_EXAMPLE
        if _endswith_component_path(normalized, "frontend/Dockerfile"):
            return REACT_DOCKERFILE
    if is_node_express_project(expected_files):
        if _endswith_component_path(normalized, "backend/package.json"):
            return NODE_PACKAGE_JSON
        if _endswith_component_path(normalized, "backend/.env.example"):
            return NODE_ENV_EXAMPLE
        if _endswith_component_path(normalized, "backend/Dockerfile"):
            return NODE_DOCKERFILE
    if not is_django_project(expected_files):
        return None
    project_package, app_package = django_layout(expected_files)
    if not project_package:
        return None
    values: dict[str, object] = {
        "project_name": project_package,
        "app_name": app_package or project_package,
        "secret_key": secret_key or secrets.token_urlsafe(32),
    }

    if name == "manage.py":
        return render_template(MANAGE_TEMPLATE, values)
    if name == "settings.py" and parent == project_package:
        values["auth_user_model"] = (
            f'AUTH_USER_MODEL = "{app_package}.User"\n' if custom_user_model and app_package else ""
        )
        values["databases"] = (
            POSTGRES_DATABASES_TEMPLATE
            if uses_postgres_runtime(expected_files)
            else SQLITE_DATABASES_TEMPLATE
        ).rstrip()
        # Only point at a WSGI module the milestone actually creates: Django's
        # own checks flag a WSGI_APPLICATION whose module does not exist, and
        # `manage.py check` does not need the setting at all.
        values["wsgi_application"] = (
            f'WSGI_APPLICATION = "{project_package}.wsgi.application"\n'
            if _expects(expected_files, project_package, "wsgi.py")
            else ""
        )
        return render_template(SETTINGS_TEMPLATE, values)
    if name == "urls.py" and parent == project_package:
        template = (
            PROJECT_URLS_WITH_APP_TEMPLATE
            if app_package and _expects(expected_files, app_package, "urls.py")
            else PROJECT_URLS_TEMPLATE
        )
        return render_template(template, values)
    if name == "urls.py" and parent == app_package:
        return render_template(APP_URLS_TEMPLATE, values)
    if name == "wsgi.py" and parent == project_package:
        return render_template(WSGI_TEMPLATE, values)
    if name == "asgi.py" and parent == project_package:
        return render_template(ASGI_TEMPLATE, values)
    if name == "apps.py" and parent == app_package and app_package:
        values["app_config_class"] = f"{app_package.replace('_', ' ').title().replace(' ', '')}Config"
        return render_template(APP_CONFIG_TEMPLATE, values)
    if _endswith_component_path(normalized, "backend/requirements.txt"):
        return DJANGO_REQUIREMENTS_TEMPLATE
    if _endswith_component_path(normalized, "backend/.env.example"):
        return DJANGO_ENV_EXAMPLE
    if _endswith_component_path(normalized, "backend/Dockerfile"):
        return DJANGO_DOCKERFILE
    return None
