from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

from rich.console import Console

from shamsu.cli.repl import (
    _handle_log,
    _print_help,
    _resolve_workspace_file,
)
from shamsu.patch.engine import PatchEngine
from shamsu.safety.audit import AuditLogger
from shamsu.safety.sandbox import SecurityError
from shamsu.session.manager import SessionManager
from shamsu.tools.django import DjangoSetupRunner
from shamsu.tools.executor import (
    BLOCKED_EXIT_CODE,
    WORKSPACE_EXIT_CODE,
    CommandRunner,
)


class RecordingCommandRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Path]] = []

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        self.calls.append((command, cwd))
        return 0, "", ""

    def run_tests(self, cwd: Path):  # pragma: no cover - not used here
        raise NotImplementedError


def test_prd_parse_blocks_path_traversal(tmp_path: Path):
    outside = tmp_path.parent / "outside_prd.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    try:
        try:
            _resolve_workspace_file("../outside_prd.md", tmp_path)
        except SecurityError as exc:
            assert "outside workspace" in str(exc)
        else:  # pragma: no cover - explicit failure path
            raise AssertionError("Expected PRD path traversal to be blocked")
    finally:
        outside.unlink(missing_ok=True)


def test_project_setup_blocks_outside_project_without_running_commands(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside_project"
    workspace.mkdir()
    outside.mkdir()
    (outside / "requirements.txt").write_text("Django==5.0.6\n", encoding="utf-8")
    (outside / "manage.py").write_text("#!/usr/bin/env python\n", encoding="utf-8")
    runner = RecordingCommandRunner()

    result = DjangoSetupRunner(workspace, command_runner=runner).run(outside)

    assert result.ok is False
    assert "outside workspace" in result.failures[0].stderr
    assert runner.calls == []


def test_patch_apply_blocks_path_traversal_before_approval(tmp_path: Path):
    approvals = []
    diff = """--- a/../outside.py
+++ b/../outside.py
@@ -1 +1 @@
-old
+new
"""

    engine = PatchEngine(
        tmp_path,
        approval_func=lambda request: approvals.append(request) or True,
    )

    assert engine.apply(diff, tmp_path) is False
    assert approvals == []
    assert not (tmp_path.parent / "outside.py").exists()


def test_session_log_paths_stay_inside_workspace_and_redact_secrets(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("safety-test")
    logger.log("test.event", {"message": 'password = "abc123"'}, 'password = "abc123"')
    assert logger.events_path.is_relative_to(tmp_path / ".shamsu" / "sessions")

    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    _handle_log("log tail 5", logger, console)

    rendered = output.getvalue()
    assert "[REDACTED]" in rendered
    assert "abc123" not in rendered


def test_audit_logger_keeps_log_and_affected_paths_inside_workspace(tmp_path: Path):
    outside = tmp_path.parent / "outside.py"
    outside.write_text("secret = 'abc123'\n", encoding="utf-8")
    try:
        log_path = AuditLogger(tmp_path).log(
            "file_write",
            "success",
            affected_paths=["inside.py", outside],
            details={"stdout": "SECRET_KEY = 'django-insecure-secret'"},
        )
    finally:
        outside.unlink(missing_ok=True)

    assert log_path == tmp_path / ".shamsu" / "audit.jsonl"
    event = json.loads(log_path.read_text(encoding="utf-8"))
    assert event["affected_paths"] == ["inside.py"]
    assert "[REDACTED]" in event["details"]["stdout"]
    assert "django-insecure-secret" not in log_path.read_text(encoding="utf-8")


def test_command_cwd_traversal_and_dangerous_commands_are_blocked(tmp_path: Path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    runner = CommandRunner(workspace, approval_func=lambda _request: True)

    cwd_code, _stdout, cwd_stderr = runner.run("python -m pytest --version", outside)
    blocked_code, blocked_stdout, blocked_stderr = runner.run("rm -rf /", workspace)

    assert cwd_code == WORKSPACE_EXIT_CODE
    assert "outside workspace" in cwd_stderr
    assert blocked_code == BLOCKED_EXIT_CODE
    assert blocked_stdout == ""
    assert "Blocked command" in blocked_stderr


def test_command_output_secrets_are_redacted(tmp_path: Path):
    runner = CommandRunner(tmp_path, approval_func=lambda _request: True)
    command = (
        "python -c \"import sys; "
        "print('SECRET_KEY = \\\"django-insecure-secret\\\"'); "
        "print('password = \\\"abc123\\\"', file=sys.stderr)\""
    )

    _code, stdout, stderr = runner.run(command, tmp_path)

    assert "[REDACTED]" in stdout
    assert "[REDACTED]" in stderr
    assert "django-insecure-secret" not in stdout
    assert "abc123" not in stderr


def test_repl_help_does_not_expose_arbitrary_shell_execution():
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    _print_help(console)

    rendered = output.getvalue().lower()
    assert "django setup" in rendered
    assert "run <command>" not in rendered
    assert "shell" not in rendered
    assert "exec" not in rendered
