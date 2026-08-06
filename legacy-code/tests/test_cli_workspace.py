from __future__ import annotations

from io import StringIO

from rich.console import Console

from shamsu.cli.repl import (
    _handle_log,
    _handle_parse_prd,
    _resolve_workspace_file,
    parse_args,
    resolve_workspace,
)
from shamsu.safety.sandbox import SecurityError
from shamsu.session.manager import SessionManager


def test_parse_args_accepts_workspace():
    args = parse_args(["--workspace", "sample-project"])

    assert args.workspace == "sample-project"


def test_resolve_workspace_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    assert resolve_workspace(None) == tmp_path.resolve()


def test_resolve_workspace_uses_explicit_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert resolve_workspace(str(workspace)) == workspace.resolve()


def test_resolve_workspace_does_not_auto_redirect_to_ancestor_workspace(tmp_path):
    (tmp_path / ".shamsu").mkdir()
    child = tmp_path / "scripts"
    child.mkdir()

    assert resolve_workspace(str(child)) == child.resolve()


def test_parse_prd_path_accepts_file_inside_workspace(tmp_path):
    prd = tmp_path / "PROJECT.md"
    prd.write_text("# Project\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")

    resolved = _resolve_workspace_file("PROJECT.md", tmp_path)

    assert resolved == prd.resolve()


def test_parse_prd_path_rejects_file_outside_workspace(tmp_path):
    outside = tmp_path.parent / "OUTSIDE_PRD.md"
    outside.write_text("# Outside\n", encoding="utf-8")

    try:
        try:
            _resolve_workspace_file(str(outside), tmp_path)
        except SecurityError as exc:
            assert "outside workspace" in str(exc)
        else:  # pragma: no cover - explicit failure path
            raise AssertionError("Expected SecurityError")
    finally:
        outside.unlink(missing_ok=True)


def test_handle_parse_prd_prints_parsed_title_inside_workspace(tmp_path):
    prd = tmp_path / "PROJECT.md"
    prd.write_text("# Project\n\n## Pages\n- Dashboard: overview\n", encoding="utf-8")
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)

    _handle_parse_prd("parse-prd PROJECT.md", tmp_path, console)

    assert "Title: Project" in output.getvalue()


def test_handle_parse_prd_reports_outside_workspace(tmp_path):
    outside = tmp_path.parent / "OUTSIDE_PRD.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)

    try:
        _handle_parse_prd(f'parse-prd "{outside}"', tmp_path, console)
        assert "outside workspace" in output.getvalue()
    finally:
        outside.unlink(missing_ok=True)


def test_handle_log_tails_and_redacts_session_events(tmp_path):
    logger = SessionManager(tmp_path).create_session("test")
    logger.log("test.first", {"message": "first"}, "first")
    logger.log("test.secret", {"password": "abc123"}, 'password = "abc123"')
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    _handle_log("log tail 1", logger, console)

    rendered = output.getvalue()
    assert "Last 1 Events" in rendered
    assert "[REDACTED]" in rendered
    assert "abc123" not in rendered
    assert "first" not in rendered


def test_handle_log_reports_empty_session_events(tmp_path):
    logger = SessionManager(tmp_path).create_session("test")
    logger.events_path.unlink()
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    _handle_log("log", logger, console)

    assert "No session events yet" in output.getvalue()
