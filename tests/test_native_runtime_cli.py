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


def test_runtime_errors_point_to_models_repair():
    assert repl._looks_like_runtime_error("Could not connect to localhost:11434")
    assert repl._looks_like_runtime_error("model not found")
    assert not repl._looks_like_runtime_error("ordinary validation issue")
