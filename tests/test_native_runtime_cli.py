from __future__ import annotations

from io import StringIO

from rich.console import Console

from shamsu.cli import repl
from shamsu.runtime.ollama import RuntimeStatus
from shamsu.runtime.models import required_model_names


def _console_output() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def test_models_status_prints_missing_runtime_message(monkeypatch):
    console, output = _console_output()
    monkeypatch.setattr(
        repl,
        "collect_status",
        lambda: RuntimeStatus(missing_models=required_model_names()),
    )

    repl._handle_models("models status", console)

    rendered = output.getvalue()
    assert "Local Runtime" in rendered
    assert "local-only Ollama" in rendered
    assert "Ollama not found" in rendered


def test_models_pull_asks_approval_before_downloading(monkeypatch, tmp_path):
    console, output = _console_output()
    status = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama.exe"),
        server_running=True,
        missing_models=["qwen3:8b"],
    )
    monkeypatch.setattr(repl, "collect_status", lambda *args, **kwargs: status)
    monkeypatch.setattr(
        repl,
        "_pull_models_with_progress",
        lambda *_args, **_kwargs: {"qwen3:8b": 0},
    )

    approvals = []

    def deny(request):
        approvals.append(request)
        return False

    repl._handle_models("models pull", console, approval_func=deny)

    rendered = output.getvalue()
    assert approvals[0].action_type == "run_command"
    assert "qwen3:8b" in approvals[0].preview
    assert "cancelled" in rendered


def test_models_pull_closed_approval_input_does_not_crash(monkeypatch, tmp_path):
    console, output = _console_output()
    status = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama.exe"),
        server_running=True,
        missing_models=["qwen3:8b"],
    )
    monkeypatch.setattr(repl, "collect_status", lambda *args, **kwargs: status)

    def raise_eof(_prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)

    repl._handle_models("models pull", console)

    rendered = output.getvalue()
    assert "Action cancelled" in rendered
    assert "Model download cancelled" in rendered


def test_models_pull_downloads_with_progress_when_approved(monkeypatch, tmp_path):
    console, output = _console_output()
    calls = []
    status_before = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama.exe"),
        server_running=True,
        missing_models=["qwen3:8b"],
    )
    status_after = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama.exe"),
        server_running=True,
        installed_models=required_model_names(),
        missing_models=[],
    )
    statuses = iter([status_before, status_after])
    monkeypatch.setattr(repl, "collect_status", lambda *args, **kwargs: next(statuses))

    def pull(ollama_path, models, console):
        calls.append((ollama_path, models))
        return {"qwen3:8b": 0}

    monkeypatch.setattr(repl, "_pull_models_with_progress", pull)

    repl._handle_models("models pull", console, approval_func=lambda _request: True)

    rendered = output.getvalue()
    assert calls[0][1] == ["qwen3:8b"]
    assert "qwen3:8b: installed" in rendered
    assert "Local Runtime" in rendered


def test_runtime_errors_point_to_models_repair():
    assert repl._looks_like_runtime_error("Could not connect to localhost:11434")
    assert repl._looks_like_runtime_error("model not found")
    assert not repl._looks_like_runtime_error("ordinary validation issue")
