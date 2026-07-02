from __future__ import annotations

from io import StringIO

from rich.console import Console

from shamsu.cli.repl import (
    _handle_log,
    _handle_parse_prd,
    _handle_status,
    _resolve_workspace_file,
    parse_args,
    resolve_workspace,
)
from shamsu.safety.sandbox import SecurityError


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


def test_handle_status_prints_workspace_index_model_and_task(monkeypatch, tmp_path):
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    class FakeRuntimeStatus:
        ready = True
        base_url = "http://localhost:11434"
        ollama_path = "ollama"
        server_running = True
        missing_models = []
        message = "ready"

    monkeypatch.setattr("shamsu.cli.repl.collect_status", lambda: FakeRuntimeStatus())
    monkeypatch.setattr("shamsu.cli.repl.status_text", lambda _status: "Ollama is ready.")

    _handle_status(tmp_path, console)

    rendered = output.getvalue()
    assert "SHAMSU Status" in rendered
    assert "Workspace" in rendered
    assert str(tmp_path) in rendered
    assert "Index" in rendered
    assert "missing" in rendered
    assert "Files" in rendered
    assert "Symbols" in rendered
    assert "Task status" in rendered
    assert "Current model" in rendered
    assert "Pending approvals" in rendered


def test_handle_log_tails_and_redacts_structured_logs(tmp_path):
    log_dir = tmp_path / ".shamsu"
    log_dir.mkdir()
    (log_dir / "shamsu.log").write_text(
        '{"level":"info","message":"first"}\n'
        '{"level":"error","password":"abc123","message":"secret"}\n',
        encoding="utf-8",
    )
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    _handle_log("log 1", tmp_path, console)

    rendered = output.getvalue()
    assert "SHAMSU Log" in rendered
    assert "secret" in rendered
    assert "[REDACTED]" in rendered
    assert "abc123" not in rendered
    assert "first" not in rendered


def test_handle_log_reports_missing_log_file(tmp_path):
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    _handle_log("log", tmp_path, console)

    assert "No log file found" in output.getvalue()
