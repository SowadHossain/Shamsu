from __future__ import annotations

from pathlib import Path

from shamsu.tools.django import (
    INSTALL_REQUIREMENTS_COMMAND,
    MAKE_MIGRATIONS_COMMAND,
    MIGRATE_COMMAND,
    DjangoSetupRunner,
)


class FakeCommandRunner:
    def __init__(self, outputs: dict[str, tuple[int, str, str]] | None = None) -> None:
        self.outputs = outputs or {}
        self.calls: list[tuple[str, Path]] = []

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        self.calls.append((command, cwd))
        return self.outputs.get(command, (0, f"{command} ok", ""))

    def run_tests(self, cwd: Path):  # pragma: no cover - not used by setup runner
        raise NotImplementedError


def test_django_setup_runs_install_and_migrations_in_project_cwd(tmp_path: Path):
    project = _django_project(tmp_path)
    runner = FakeCommandRunner()

    result = DjangoSetupRunner(tmp_path, command_runner=runner).run(project)

    assert result.ok is True
    assert runner.calls == [
        (INSTALL_REQUIREMENTS_COMMAND, project),
        (MAKE_MIGRATIONS_COMMAND, project),
        (MIGRATE_COMMAND, project),
    ]
    assert [command.step for command in result.commands] == [
        "install_requirements",
        "makemigrations",
        "migrate",
    ]


def test_django_setup_install_command_is_approval_backed_by_command_runner(tmp_path: Path):
    project = _django_project(tmp_path)
    runner = FakeCommandRunner()

    DjangoSetupRunner(tmp_path, command_runner=runner).run(project)

    assert runner.calls[0] == (INSTALL_REQUIREMENTS_COMMAND, project)


def test_django_setup_stops_and_structures_failure_for_bugfix(tmp_path: Path):
    project = _django_project(tmp_path)
    runner = FakeCommandRunner(
        {
            MAKE_MIGRATIONS_COMMAND: (
                1,
                "SECRET_KEY = 'django-insecure-secret'",
                "ValueError: bad model",
            )
        }
    )

    result = DjangoSetupRunner(tmp_path, command_runner=runner).run(project)

    assert result.ok is False
    assert [command for command, _cwd in runner.calls] == [
        INSTALL_REQUIREMENTS_COMMAND,
        MAKE_MIGRATIONS_COMMAND,
    ]
    assert result.failures[0].step == "makemigrations"
    assert result.failures[0].command == MAKE_MIGRATIONS_COMMAND
    assert "ValueError: bad model" in result.bugfix_context
    assert "[REDACTED]" in result.bugfix_context
    assert "django-insecure-secret" not in result.bugfix_context


def test_django_setup_rejects_outside_workspace_project_cwd(tmp_path: Path):
    outside = tmp_path.parent / "outside_django_project"
    outside.mkdir(exist_ok=True)
    try:
        result = DjangoSetupRunner(tmp_path, command_runner=FakeCommandRunner()).run(outside)

        assert result.ok is False
        assert result.commands == []
        assert result.failures[0].step == "validate_project"
        assert "outside workspace" in result.failures[0].stderr
    finally:
        (outside / "requirements.txt").unlink(missing_ok=True)
        (outside / "manage.py").unlink(missing_ok=True)
        outside.rmdir()


def test_django_setup_requires_generated_project_files(tmp_path: Path):
    project = tmp_path / "generated"
    project.mkdir()

    result = DjangoSetupRunner(tmp_path, command_runner=FakeCommandRunner()).run(project)

    assert result.ok is False
    assert result.commands == []
    assert "requirements.txt not found" in result.failures[0].stderr


def _django_project(root: Path) -> Path:
    project = root / "generated"
    project.mkdir()
    (project / "requirements.txt").write_text("Django==5.0.6\n", encoding="utf-8")
    (project / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    return project
