"""Harness-owned verification planning, execution, and bounded repair.

The model may write code, but it does not decide whether that code is done.
This module discovers deterministic project checks, executes them in a stable
order, and reports an honest verified / failed / unverifiable verdict.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.repair.loop import RepairLoop, VerifyRun
from shamsu.repair.proposer_llm import GenerateJSON, LLMProposer
from shamsu.repair.types import RepairResult
from shamsu.session.manager import SessionLogger
from shamsu.verify.wiring import WIRING_COMMAND, has_wiring_surface, verify_wiring


class CommandRunnerLike(Protocol):
    def run(self, command: str, cwd: Path) -> tuple[int, str, str]: ...


@dataclass(frozen=True)
class AcceptanceCheck:
    """An executable PRD criterion with optional stdout evidence."""

    command: str
    stdout_contains: tuple[str, ...] = ()
    criterion: str = ""


@dataclass(frozen=True)
class VerificationStep:
    """One deterministic check in a verification plan."""

    stage: str
    command: str
    cwd: Path
    required: bool = True
    reason: str = ""
    stdout_contains: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationStepResult:
    """Ground-truth result for one executed verification step."""

    step: VerificationStep
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def passed(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True)
class VerificationPlan:
    """Ordered checks discovered from project-owned metadata."""

    workspace: Path
    steps: tuple[VerificationStep, ...] = ()

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(step.command for step in self.steps)


@dataclass(frozen=True)
class VerifyOutcome:
    """The harness verdict on a set of changes."""

    verified: bool
    unverifiable: bool = False
    exit_code: int | None = None
    command: str = ""
    summary: str = ""
    repair_result: RepairResult | None = None
    steps: tuple[VerificationStepResult, ...] = field(default_factory=tuple)
    failed_step: VerificationStep | None = None

    @property
    def failed(self) -> bool:
        return not self.verified and not self.unverifiable

    def status(self) -> str:
        if self.verified:
            return "verified"
        if self.unverifiable:
            return "unverifiable"
        return "failed"


def acceptance_checks_from_criteria(criteria: Sequence[str]) -> tuple[AcceptanceCheck, ...]:
    """Extract explicit executable commands from backticked acceptance text.

    Only common tool/runtime prefixes are accepted. Backticked values after a
    ``prints`` criterion become required stdout evidence; unrelated file names
    and prose are ignored.
    """
    checks: list[AcceptanceCheck] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    allowed_prefix = re.compile(
        r"^(?:python3?|pytest|npm|pnpm|yarn|node|npx|uv|poetry|"
        r"cargo|go|dotnet|java|mvn|gradle|bash|sh|pwsh|powershell)\b",
        re.IGNORECASE,
    )
    for criterion in criteria:
        matches = re.findall(r"`([^`\r\n]+)`", str(criterion))
        if not matches:
            continue
        candidate = matches[0].strip()
        if not allowed_prefix.match(candidate):
            continue
        expected = ()
        if re.search(r"\bprints?\b", str(criterion), re.IGNORECASE):
            expected = tuple(value.strip() for value in matches[1:] if value.strip())
        key = (candidate, expected)
        if key not in seen:
            checks.append(
                AcceptanceCheck(
                    command=candidate,
                    stdout_contains=expected,
                    criterion=str(criterion).strip(),
                )
            )
            seen.add(key)
    return tuple(checks)


def acceptance_commands_from_criteria(criteria: Sequence[str]) -> tuple[str, ...]:
    """Compatibility view of extracted acceptance checks as command strings."""
    return tuple(check.command for check in acceptance_checks_from_criteria(criteria))


def default_verify_command(
    changed_files: list[str],
    *,
    stack: str = "",
    stack_hint: str = "",
    python_bin: str | None = None,
    lightweight: bool = False,
) -> str:
    """Return the legacy single-command verifier.

    This compatibility wrapper intentionally retains its historical output.
    New verification flows should use :func:`build_verification_plan`.
    """
    stack_l = (stack or "").lower()
    hint = (stack_hint or "").lower()
    py_files = [f for f in changed_files if f.endswith(".py")]
    has_package_json = any(f.endswith("package.json") for f in changed_files)
    has_requirements = any(f.endswith("requirements.txt") for f in changed_files)

    is_node = (
        has_package_json
        or "node" in stack_l
        or "node" in hint
        or "vite" in stack_l
    )
    if is_node:
        return "" if lightweight else "npm install && npm run build"

    if py_files or "python" in stack_l or "django" in stack_l or hint in {"python", "django"}:
        pybin = python_bin or _default_python_bin()
        if has_requirements:
            prefix = "" if lightweight else "pip install -r requirements.txt && "
            return f"{prefix}{pybin} -m py_compile " + " ".join(py_files)
        if py_files:
            return f"{pybin} -m py_compile " + " ".join(py_files)
    return ""


def build_verification_plan(
    workspace: Path | str,
    changed_files: Sequence[str],
    *,
    stack: str = "",
    stack_hint: str = "",
    python_bin: str | None = None,
    lightweight: bool = True,
    acceptance_commands: Sequence[str | AcceptanceCheck] = (),
) -> VerificationPlan:
    """Discover deterministic setup, build, test, lint, and acceptance checks."""
    workspace_path = Path(workspace).resolve()
    changed = [_normalized_relative_path(workspace_path, value) for value in changed_files]
    changed = [value for value in changed if value]
    stack_text = f"{stack} {stack_hint}".lower()
    is_node = _is_node_project(workspace_path, changed, stack_text)
    is_python = _is_python_project(workspace_path, changed, stack_text)

    steps: list[VerificationStep] = []
    project_roots: list[Path] = []
    if is_node:
        node_root = _project_root(workspace_path, changed, ("package.json",))
        project_roots.append(node_root)
        steps.extend(_node_steps(node_root, lightweight=lightweight))
    if is_python:
        python_root = _project_root(
            workspace_path,
            changed,
            ("manage.py", "pyproject.toml", "requirements.txt", "setup.cfg"),
        )
        project_roots.append(python_root)
        steps.extend(
            _python_steps(
                python_root,
                changed,
                workspace_path=workspace_path,
                python_bin=python_bin or _default_python_bin(),
                lightweight=lightweight,
            )
        )
    unique_roots = list(dict.fromkeys(project_roots))
    project_root = unique_roots[0] if len(unique_roots) == 1 else workspace_path
    if has_wiring_surface(project_root):
        steps.insert(
            0,
            VerificationStep(
                "wiring",
                WIRING_COMMAND,
                project_root,
                reason="frontend/backend routes and database queries must match declarations",
            ),
        )

    existing = {step.command.strip() for step in steps}
    for item in acceptance_commands:
        if isinstance(item, AcceptanceCheck):
            normalized = item.command.strip()
            expected = item.stdout_contains
            reason = item.criterion or "explicit acceptance check"
        else:
            normalized = str(item).strip()
            expected = ()
            reason = "explicit acceptance check"
        if not normalized or (normalized in existing and not expected):
            continue
        steps.append(
            VerificationStep(
                stage="acceptance",
                command=normalized,
                cwd=project_root,
                required=True,
                reason=reason,
                stdout_contains=expected,
            )
        )
        existing.add(normalized)
    return VerificationPlan(workspace=workspace_path, steps=tuple(steps))


def stack_of(changed_files: list[str]) -> str:
    """Infer a coarse stack label from changed file names."""
    if any(f.endswith("package.json") for f in changed_files) or any(
        f.endswith((".ts", ".tsx", ".jsx")) for f in changed_files
    ):
        return "node"
    if any(f.endswith("manage.py") or f.endswith("settings.py") for f in changed_files):
        return "django"
    if any(f.endswith((".py", "requirements.txt")) for f in changed_files):
        return "python"
    return ""


def verify_only(
    workspace: Path | str,
    changed_files: list[str],
    *,
    command_runner: CommandRunnerLike | None = None,
    stack: str = "",
    stack_hint: str = "",
    lightweight: bool = True,
    session_logger: SessionLogger | None = None,
    acceptance_commands: Sequence[str | AcceptanceCheck] = (),
) -> VerifyOutcome:
    """Execute the discovered plan once without asking the model to repair it."""
    workspace_path = Path(workspace).resolve()
    plan = build_verification_plan(
        workspace_path,
        changed_files,
        stack=stack,
        stack_hint=stack_hint,
        lightweight=lightweight,
        acceptance_commands=acceptance_commands,
    )
    if not plan.steps:
        return _unverifiable_outcome()

    runner = command_runner or _default_runner(workspace_path, session_logger)
    results, failed_step = _execute_plan(plan, runner, session_logger)
    return _outcome_from_results(plan, results, failed_step)


def verify_and_repair(
    workspace: Path | str,
    changed_files: list[str],
    *,
    generate: GenerateJSON,
    command_runner: CommandRunnerLike | None = None,
    max_attempts: int = 2,
    stack: str = "",
    stack_hint: str = "",
    lightweight: bool = False,
    session_logger: SessionLogger | None = None,
    acceptance_commands: Sequence[str | AcceptanceCheck] = (),
) -> VerifyOutcome:
    """Execute a plan, repair its first required failure, then rerun the plan."""
    workspace_path = Path(workspace).resolve()
    plan = build_verification_plan(
        workspace_path,
        changed_files,
        stack=stack,
        stack_hint=stack_hint,
        lightweight=lightweight,
        acceptance_commands=acceptance_commands,
    )
    if not plan.steps:
        return _unverifiable_outcome()

    runner = command_runner or _default_runner(workspace_path, session_logger)
    initial_results, failed_step = _execute_plan(plan, runner, session_logger)
    if failed_step is None:
        return _outcome_from_results(plan, initial_results, None)

    repair_result = RepairLoop(
        failed_step.cwd,
        _VerificationStepVerifier(failed_step, runner),
        LLMProposer(generate),
        max_attempts=max_attempts,
        session_logger=session_logger,
        digest=DiagnosticDigest(failed_step.cwd),
    ).run()
    if not repair_result.success or repair_result.exit_code != 0:
        return _outcome_from_results(
            plan,
            initial_results,
            failed_step,
            repair_result=repair_result,
            summary=repair_result.final_message,
        )

    final_results, final_failed_step = _execute_plan(plan, runner, session_logger)
    return _outcome_from_results(
        plan,
        final_results,
        final_failed_step,
        repair_result=repair_result,
    )


def _node_steps(project_root: Path, *, lightweight: bool) -> list[VerificationStep]:
    package_path = project_root / "package.json"
    if not package_path.is_file():
        return []
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return [
            VerificationStep(
                "syntax",
                'node -e "JSON.parse(require(\'fs\').readFileSync(\'package.json\', \'utf8\'))"',
                project_root,
                reason="package.json must be valid JSON",
            )
        ]

    scripts = package.get("scripts") if isinstance(package, dict) else {}
    scripts = scripts if isinstance(scripts, dict) else {}
    checks: list[VerificationStep] = []
    if not lightweight:
        checks.append(
            VerificationStep(
                "setup",
                "npm install",
                project_root,
                reason="install project-declared dependencies",
            )
        )
    elif not (project_root / "node_modules").is_dir():
        return []

    if _real_npm_script(scripts.get("build")):
        checks.append(
            VerificationStep("build", "npm run build", project_root, reason="project build script")
        )
    test_script = scripts.get("test")
    if _real_npm_script(test_script):
        checks.append(
            VerificationStep(
                "test",
                _node_test_command(str(test_script)),
                project_root,
                reason="project test script",
            )
        )
    if _real_npm_script(scripts.get("lint")):
        checks.append(
            VerificationStep(
                "lint",
                "npm run lint",
                project_root,
                required=False,
                reason="project lint script",
            )
        )

    # Setup alone proves dependency resolution, not application correctness.
    if not any(step.stage != "setup" for step in checks):
        return []
    return checks


def _python_steps(
    project_root: Path,
    changed: Sequence[str],
    *,
    workspace_path: Path,
    python_bin: str,
    lightweight: bool,
) -> list[VerificationStep]:
    checks: list[VerificationStep] = []
    requirements = project_root / "requirements.txt"
    if requirements.is_file() and not lightweight:
        checks.append(
            VerificationStep(
                "setup",
                f"{python_bin} -m pip install -r requirements.txt",
                project_root,
                reason="install project-declared dependencies",
            )
        )

    py_files = _python_files_for_root(project_root, changed, workspace_path)
    if py_files:
        checks.append(
            VerificationStep(
                "syntax",
                f"{python_bin} -m py_compile "
                + " ".join(_quote_command_arg(path) for path in py_files),
                project_root,
                reason="compile changed Python files",
            )
        )

    if (project_root / "manage.py").is_file():
        checks.append(
            VerificationStep(
                "test",
                f"{python_bin} manage.py test",
                project_root,
                reason="Django project test suite",
            )
        )
    elif _has_pytest(project_root):
        checks.append(
            VerificationStep(
                "test",
                f"{python_bin} -m pytest",
                project_root,
                reason="Python project test suite",
            )
        )

    lint_command = _python_lint_command(project_root, python_bin)
    if lint_command:
        checks.append(
            VerificationStep(
                "lint",
                lint_command,
                project_root,
                required=False,
                reason="project lint configuration",
            )
        )
    return checks


def _execute_plan(
    plan: VerificationPlan,
    runner: CommandRunnerLike,
    session_logger: SessionLogger | None,
) -> tuple[tuple[VerificationStepResult, ...], VerificationStep | None]:
    results: list[VerificationStepResult] = []
    failed_step: VerificationStep | None = None
    for step in plan.steps:
        run = _VerificationStepVerifier(step, runner).run()
        result = VerificationStepResult(step, run.exit_code, run.stdout, run.stderr)
        results.append(result)
        _log_verification_step(result, session_logger)
        if result.exit_code != 0 and step.required:
            failed_step = step
            break
    return tuple(results), failed_step


class _VerificationStepVerifier:
    """Apply both process-exit and acceptance-output truth to one command."""

    def __init__(self, step: VerificationStep, runner: CommandRunnerLike) -> None:
        self.step = step
        self.runner = runner

    @property
    def command(self) -> str:
        return self.step.command

    def run(self) -> VerifyRun:
        if self.step.command == WIRING_COMMAND:
            result = verify_wiring(self.step.cwd)
            return VerifyRun(
                command=self.step.command,
                exit_code=0 if result.ok else 1,
                stdout=(
                    "Wiring check passed: "
                    f"{result.frontend_calls} frontend call(s), "
                    f"{result.backend_routes} backend route(s), "
                    f"{result.query_tables} database query reference(s)."
                    if result.ok
                    else ""
                ),
                stderr=result.stderr(),
            )
        exit_code, stdout, stderr = self.runner.run(self.step.command, self.step.cwd)
        stdout = stdout or ""
        stderr = stderr or ""
        if exit_code == 0 and self.step.stdout_contains:
            missing = [value for value in self.step.stdout_contains if value not in stdout]
            if missing:
                exit_code = 1
                detail = "Acceptance output missing: " + ", ".join(repr(value) for value in missing)
                stderr = f"{stderr.rstrip()}\n{detail}".strip()
        return VerifyRun(
            command=self.step.command,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )


def _outcome_from_results(
    plan: VerificationPlan,
    results: tuple[VerificationStepResult, ...],
    failed_step: VerificationStep | None,
    *,
    repair_result: RepairResult | None = None,
    summary: str = "",
) -> VerifyOutcome:
    command = failed_step.command if failed_step else " && ".join(plan.commands)
    if failed_step is not None:
        failed_result = next(
            (result for result in reversed(results) if result.step == failed_step),
            None,
        )
        exit_code = failed_result.exit_code if failed_result else 1
        message = summary or (
            f"Verification FAILED at required {failed_step.stage} stage: "
            f"`{failed_step.command}` (exit {exit_code})."
        )
        return VerifyOutcome(
            verified=False,
            exit_code=exit_code,
            command=command,
            summary=message,
            repair_result=repair_result,
            steps=results,
            failed_step=failed_step,
        )

    optional_failures = [
        result for result in results if not result.passed and not result.step.required
    ]
    required_stages = [result.step.stage for result in results if result.step.required]
    message = (
        f"Verification passed {len(required_stages)} required stage(s): "
        f"{', '.join(required_stages)}."
    )
    if optional_failures:
        names = ", ".join(result.step.stage for result in optional_failures)
        message += f" Optional stage warning(s): {names}."
    return VerifyOutcome(
        verified=True,
        exit_code=0,
        command=command,
        summary=message,
        repair_result=repair_result,
        steps=results,
    )


def _unverifiable_outcome() -> VerifyOutcome:
    return VerifyOutcome(
        verified=False,
        unverifiable=True,
        summary="No deterministic verifier is available for these changes (UNVERIFIED).",
    )


def _is_node_project(_workspace: Path, changed: Sequence[str], stack_text: str) -> bool:
    return (
        "node" in stack_text
        or "vite" in stack_text
        or "react" in stack_text
        or any(path.endswith(("package.json", ".ts", ".tsx", ".jsx")) for path in changed)
    )


def _is_python_project(_workspace: Path, changed: Sequence[str], stack_text: str) -> bool:
    return (
        "python" in stack_text
        or "django" in stack_text
        or any(path.endswith((".py", "requirements.txt", "pyproject.toml")) for path in changed)
    )


def _project_root(workspace: Path, changed: Sequence[str], markers: Sequence[str]) -> Path:
    marker_set = set(markers)
    for relative in changed:
        candidate = workspace / relative
        if candidate.name in marker_set:
            return candidate.parent.resolve()
    for relative in changed:
        candidate = (workspace / relative).parent
        while candidate == workspace or workspace in candidate.parents:
            if any((candidate / marker).is_file() for marker in markers):
                return candidate.resolve()
            if candidate == workspace:
                break
            candidate = candidate.parent
    return workspace


def _normalized_relative_path(workspace: Path, value: str) -> str:
    try:
        path = Path(value)
        if path.is_absolute():
            path = path.resolve().relative_to(workspace)
        return path.as_posix().lstrip("./")
    except (OSError, ValueError):
        return ""


def _python_files_for_root(
    project_root: Path,
    changed: Sequence[str],
    workspace: Path,
) -> list[str]:
    files: list[str] = []
    for relative in changed:
        if not relative.endswith(".py"):
            continue
        absolute = (workspace / relative).resolve()
        try:
            project_relative = absolute.relative_to(project_root)
        except ValueError:
            continue
        files.append(project_relative.as_posix())
    return list(dict.fromkeys(files))


def _real_npm_script(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    lowered = value.lower()
    return "no test specified" not in lowered and "exit 1" not in lowered


def _node_test_command(script: str) -> str:
    lowered = script.lower()
    if re.search(r"\bvitest\b", lowered) and not re.search(r"\bvitest\s+run\b", lowered):
        return "npm test -- --run"
    if re.search(r"\bjest\b", lowered) and "--runinband" not in lowered:
        return "npm test -- --runInBand"
    return "npm test"


def _has_pytest(project_root: Path) -> bool:
    if any(
        (project_root / name).is_file()
        for name in ("pytest.ini", "tox.ini", "conftest.py")
    ):
        return True
    tests_dir = project_root / "tests"
    if tests_dir.is_dir() and any(tests_dir.rglob("test_*.py")):
        return True
    for name in ("requirements.txt", "pyproject.toml", "setup.cfg"):
        path = project_root / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            continue
        if "pytest" in text:
            return True
    return False


def _python_lint_command(project_root: Path, python_bin: str) -> str:
    pyproject = project_root / "pyproject.toml"
    setup_cfg = project_root / "setup.cfg"
    try:
        config = "\n".join(
            path.read_text(encoding="utf-8", errors="replace").lower()
            for path in (pyproject, setup_cfg)
            if path.is_file()
        )
    except OSError:
        return ""
    if "[tool.ruff" in config or "[ruff]" in config:
        return f"{python_bin} -m ruff check ."
    return ""


def _quote_command_arg(value: str) -> str:
    if not re.search(r"[\s\"']", value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def _log_verification_step(
    result: VerificationStepResult,
    session_logger: SessionLogger | None,
) -> None:
    payload = {
        "stage": result.step.stage,
        "command": result.step.command,
        "cwd": str(result.step.cwd),
        "required": result.step.required,
        "exit_code": result.exit_code,
        "passed": result.passed,
    }
    if session_logger:
        session_logger.log(
            "verification.step",
            payload,
            f"Verification {result.step.stage}: {'passed' if result.passed else 'failed'}",
            workflow_id="verification",
        )
    from shamsu.action_ledger.context import get_current_run

    ledger = get_current_run()
    if ledger:
        ledger.log_event("verification_step", **payload)


def _default_python_bin() -> str:
    return "python" if os.name == "nt" else "python3"


def _default_runner(workspace: Path, session_logger: SessionLogger | None) -> CommandRunnerLike:
    from shamsu.action_ledger.context import get_current_run
    from shamsu.tools.executor import CommandRunner

    return CommandRunner(
        workspace,
        approval_func=lambda _request: True,
        session_logger=session_logger,
        action_ledger=get_current_run(),
    )
