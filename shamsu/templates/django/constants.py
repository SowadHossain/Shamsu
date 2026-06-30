"""
Fixed Django project templates.

These files should be generated with substitution only. Keeping them out of
the LLM path saves tokens and removes a large class of avoidable mistakes.
"""
from __future__ import annotations

MANAGE_TEMPLATE = """#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{{ project_name }}.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
"""

SETTINGS_TEMPLATE = """from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = "{{ secret_key }}"
DEBUG = True
ALLOWED_HOSTS = []

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt",
    "crispy_forms",
    "crispy_tailwind",
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
        "DIRS": [],
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

WSGI_APPLICATION = "{{ project_name }}.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

CRISPY_ALLOWED_TEMPLATE_PACKS = "tailwind"
CRISPY_TEMPLATE_PACK = "tailwind"

LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/dashboard/"
LOGOUT_REDIRECT_URL = "/login/"
"""

PROJECT_URLS_TEMPLATE = """from django.contrib import admin
from django.urls import include, path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("{{ app_name }}.urls")),
    path("api/auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
]
"""

WSGI_TEMPLATE = """import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{{ project_name }}.settings")

application = get_wsgi_application()
"""

ASGI_TEMPLATE = """import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "{{ project_name }}.settings")

application = get_asgi_application()
"""

BASE_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en" data-theme="{{ theme }}">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{% block title %}{{ display_name }}{% endblock %}</title>
  <link href="https://cdn.jsdelivr.net/npm/daisyui@4/dist/full.min.css" rel="stylesheet">
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://unpkg.com/htmx.org@1.9/dist/htmx.min.js"></script>
</head>
<body class="min-h-screen bg-base-200">
  <div class="navbar bg-base-100 shadow">
    <div class="navbar-start">
      <a href="{% url 'dashboard' %}" class="btn btn-ghost text-xl">{{ display_name }}</a>
    </div>
    <div class="navbar-center hidden lg:flex">
      <ul class="menu menu-horizontal px-1">
        {{ nav_links }}
      </ul>
    </div>
    <div class="navbar-end">
      {% if user.is_authenticated %}
        <span class="text-sm mr-4 opacity-70">{{ user.username }}</span>
        <a href="{% url 'logout' %}" class="btn btn-ghost btn-sm">Logout</a>
      {% else %}
        <a href="{% url 'login' %}" class="btn btn-primary btn-sm">Login</a>
      {% endif %}
    </div>
  </div>

  {% if messages %}
    <div class="container mx-auto px-4 mt-4">
      {% for message in messages %}
        <div class="alert alert-info mb-2 shadow">
          <span>{{ message }}</span>
        </div>
      {% endfor %}
    </div>
  {% endif %}

  <main class="container mx-auto px-4 py-8">
    {% block content %}{% endblock %}
  </main>
</body>
</html>
"""

LOGIN_HTML_TEMPLATE = """{% extends "base.html" %}
{% load crispy_forms_tags %}

{% block title %}Login{% endblock %}

{% block content %}
<div class="max-w-md mx-auto">
  <div class="card bg-base-100 shadow">
    <div class="card-body">
      <h1 class="card-title">Login</h1>
      <form method="post">
        {% csrf_token %}
        {{ form|crispy }}
        <button type="submit" class="btn btn-primary w-full">Login</button>
      </form>
    </div>
  </div>
</div>
{% endblock %}
"""

REGISTER_HTML_TEMPLATE = """{% extends "base.html" %}
{% load crispy_forms_tags %}

{% block title %}Register{% endblock %}

{% block content %}
<div class="max-w-md mx-auto">
  <div class="card bg-base-100 shadow">
    <div class="card-body">
      <h1 class="card-title">Create Account</h1>
      <form method="post">
        {% csrf_token %}
        {{ form|crispy }}
        <button type="submit" class="btn btn-primary w-full">Register</button>
      </form>
    </div>
  </div>
</div>
{% endblock %}
"""

REQUIREMENTS_TEMPLATE = """Django==5.0.6
djangorestframework==3.15.2
djangorestframework-simplejwt==5.3.1
django-crispy-forms==2.3
crispy-tailwind==1.0.3
Pillow==10.3.0
pytest-django==4.8.0
"""

ENV_EXAMPLE_TEMPLATE = """DJANGO_SECRET_KEY={{ secret_key }}
DJANGO_DEBUG=True
"""
