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
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

AUTH_PASSWORD_VALIDATORS = []
{{ auth_user_model }}
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
'''

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
    if not is_django_project(expected_files):
        return None
    parts = Path(_posix(relative_path)).parts
    if not parts:
        return None
    name = parts[-1]
    parent = parts[-2] if len(parts) >= 2 else ""
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
    return None
