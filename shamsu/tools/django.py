"""Django project setup helpers."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from shamsu.interfaces import ICommandRunner
from shamsu.safety.commands import redact
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.tools.executor import CommandRunner

INSTALL_REQUIREMENTS_COMMAND = "pip install -r requirements.txt"
MAKE_MIGRATIONS_COMMAND = "python manage.py makemigrations"
MIGRATE_COMMAND = "python manage.py migrate"


@dataclass(frozen=True)
class DjangoCommandResult:
    step: str
    command: str
    cwd: Path
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part)


@dataclass(frozen=True)
class DjangoSetupFailure:
    step: str
    command: str
    cwd: Path
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    def to_bug_report(self) -> str:
        output = "\n".join(part for part in (self.stdout, self.stderr) if part)
        return (
            f"Django setup failed during {self.step}.\n"
            f"Command: {self.command}\n"
            f"Working directory: {self.cwd}\n"
            f"Exit code: {self.exit_code}\n\n"
            f"{output}"
        ).strip()


@dataclass(frozen=True)
class DjangoSetupResult:
    project_cwd: Path
    commands: list[DjangoCommandResult] = field(default_factory=list)
    failures: list[DjangoSetupFailure] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures and all(command.ok for command in self.commands)

    @property
    def bugfix_context(self) -> str:
        return "\n\n".join(failure.to_bug_report() for failure in self.failures)


class DjangoSetupRunner:
    def __init__(
        self,
        workspace_root: Path,
        command_runner: ICommandRunner | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.command_runner = command_runner or CommandRunner(self.workspace_root)

    def run(self, project_cwd: Path | str = ".") -> DjangoSetupResult:
        try:
            cwd = self._validate_project_cwd(project_cwd)
        except (SecurityError, ValueError) as exc:
            return self._failed_without_command(Path(project_cwd), str(exc))

        commands: list[DjangoCommandResult] = []
        failures: list[DjangoSetupFailure] = []
        for step, command in (
            ("install_requirements", INSTALL_REQUIREMENTS_COMMAND),
            ("makemigrations", MAKE_MIGRATIONS_COMMAND),
            ("migrate", MIGRATE_COMMAND),
        ):
            result = self._run_step(step, command, cwd)
            commands.append(result)
            if not result.ok:
                failures.append(_failure_from_result(result))
                break
        return DjangoSetupResult(project_cwd=cwd, commands=commands, failures=failures)

    def _validate_project_cwd(self, project_cwd: Path | str) -> Path:
        cwd = self.sandbox.validate(project_cwd)
        if not cwd.is_dir():
            raise ValueError(f"Generated project directory does not exist: {cwd}")
        if not (cwd / "requirements.txt").is_file():
            raise ValueError(f"requirements.txt not found in generated project: {cwd}")
        if not (cwd / "manage.py").is_file():
            raise ValueError(f"manage.py not found in generated project: {cwd}")
        return cwd

    def _run_step(self, step: str, command: str, cwd: Path) -> DjangoCommandResult:
        exit_code, stdout, stderr = self.command_runner.run(command, cwd)
        return DjangoCommandResult(
            step=step,
            command=command,
            cwd=cwd,
            exit_code=exit_code,
            stdout=redact(stdout),
            stderr=redact(stderr),
        )

    def _failed_without_command(self, cwd: Path, error: str) -> DjangoSetupResult:
        failure = DjangoSetupFailure(
            step="validate_project",
            command="",
            cwd=cwd,
            exit_code=1,
            stderr=redact(error),
        )
        return DjangoSetupResult(project_cwd=cwd, failures=[failure])


def _failure_from_result(result: DjangoCommandResult) -> DjangoSetupFailure:
    return DjangoSetupFailure(
        step=result.step,
        command=result.command,
        cwd=result.cwd,
        exit_code=result.exit_code,
        stdout=result.stdout,
        stderr=result.stderr,
    )
