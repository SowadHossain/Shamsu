"""A half-built project is not a broken one.

An incremental build writes manage.py, then settings, then the app, then urls,
then views. Django refuses to start at every one of those steps until the last,
because each references something the next step will write. The gate reported
each refusal as a FAILED verification, so every foundation step came back
UNCONFIRMED on a file that was perfectly correct - which teaches the reader to
ignore the verdict, the one thing a verifier must never do.

Found by actually running the OpenBazaar build (2026-08-03): five of the first
ten steps "failed" this way while producing exactly the right file.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.verify.gate import (
    VerificationStep,
    VerificationStepResult,
    _django_is_checkable,
    _incomplete_project_reason,
    build_verification_plan,
)


def _result(stderr: str) -> VerificationStepResult:
    step = VerificationStep("framework", "python manage.py check", Path("."))
    return VerificationStepResult(step, 1, "", stderr)


# ── classifying a failure as "unfinished" vs "broken" ─────────────────────


def test_a_missing_local_module_means_unfinished():
    reason = _incomplete_project_reason(_result("ModuleNotFoundError: No module named 'core.urls'"))

    assert reason == "module core.urls does not exist yet"


def test_an_unwritten_sibling_module_means_unfinished():
    """`from . import views` before views.py exists."""
    reason = _incomplete_project_reason(
        _result("ImportError: cannot import name 'views' from 'core' (core/__init__.py)")
    )

    assert reason == "module views does not exist yet"


def test_an_uninstalled_user_model_means_unfinished():
    reason = _incomplete_project_reason(
        _result(
            "django.core.exceptions.ImproperlyConfigured: AUTH_USER_MODEL refers to "
            "model 'core.User' that has not been installed"
        )
    )

    assert "has not been installed" in reason


def test_a_missing_third_party_package_is_a_real_failure():
    """A broken environment must not be excused as a half-built project."""
    assert _incomplete_project_reason(_result("ModuleNotFoundError: No module named 'django'")) == ""


def test_a_genuine_system_check_error_is_a_real_failure():
    reason = _incomplete_project_reason(
        _result(
            "SystemCheckError: System check identified some issues:\nERRORS:\n"
            "core.Item.seller: (fields.E300) Field defines a relation with model 'Foo'"
        )
    )

    assert reason == ""


def test_no_result_is_not_excused():
    assert _incomplete_project_reason(None) == ""


# ── not planning a check that cannot say anything yet ─────────────────────


def _manage_py(root: Path, settings_module: str = "config.settings") -> None:
    (root / "manage.py").write_text(
        "import os, sys\n"
        f"os.environ.setdefault('DJANGO_SETTINGS_MODULE', '{settings_module}')\n",
        encoding="utf-8",
    )


def _settings(root: Path, body: str, package: str = "config") -> None:
    (root / package).mkdir(exist_ok=True)
    (root / package / "__init__.py").write_text("", encoding="utf-8")
    (root / package / "settings.py").write_text(body, encoding="utf-8")


def test_a_project_without_its_settings_yet_is_not_checkable(tmp_path: Path):
    _manage_py(tmp_path)

    assert _django_is_checkable(tmp_path) is False


def test_a_project_whose_local_app_is_missing_is_not_checkable(tmp_path: Path):
    _manage_py(tmp_path)
    _settings(tmp_path, "INSTALLED_APPS = ['django.contrib.auth', 'core']\n")

    assert _django_is_checkable(tmp_path) is False


def test_a_project_whose_user_model_is_missing_is_not_checkable(tmp_path: Path):
    _manage_py(tmp_path)
    _settings(
        tmp_path,
        "INSTALLED_APPS = ['core']\nAUTH_USER_MODEL = 'core.User'\n",
    )
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("", encoding="utf-8")

    assert _django_is_checkable(tmp_path) is False


def test_a_fully_assembled_project_is_checkable(tmp_path: Path):
    _manage_py(tmp_path)
    _settings(
        tmp_path,
        "INSTALLED_APPS = ['django.contrib.auth', 'core']\nAUTH_USER_MODEL = 'core.User'\n",
    )
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "core" / "models.py").write_text(
        "from django.db import models\n\n\nclass User(models.Model):\n    pass\n",
        encoding="utf-8",
    )

    assert _django_is_checkable(tmp_path) is True


def test_the_framework_stage_is_skipped_until_the_project_assembles(tmp_path: Path):
    _manage_py(tmp_path)
    (tmp_path / "config").mkdir()

    stages = [
        step.stage
        for step in build_verification_plan(
            tmp_path, ["manage.py"], stack_hint="django"
        ).steps
    ]

    assert "framework" not in stages
    assert "syntax" in stages   # the file written IS still checked


def test_manage_py_without_a_declared_settings_module_is_left_to_django(tmp_path: Path):
    """No readable declaration: Django is the better judge, as before."""
    (tmp_path / "manage.py").write_text("import sys\n", encoding="utf-8")

    assert _django_is_checkable(tmp_path) is True
