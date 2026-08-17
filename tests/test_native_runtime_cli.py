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


def test_models_pull_downloads_without_a_second_approval(monkeypatch, tmp_path):
    # Typing `/models pull` is itself the consent — it must download directly
    # and never sit on a fragile input() approval that can auto-cancel.
    console, output = _console_output()
    before = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama.exe"),
        server_running=True,
        missing_models=["qwen3:8b"],
    )
    after = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama.exe"),
        server_running=True,
        installed_models=["qwen3:8b"],
        missing_models=[],
    )
    statuses = iter([before, after])
    monkeypatch.setattr(repl, "collect_status", lambda *a, **k: next(statuses, after))
    calls = []
    monkeypatch.setattr(
        repl,
        "_pull_models_with_progress",
        lambda path, models, console: calls.append(list(models)) or {"qwen3:8b": 0},
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no approval prompt for explicit /models pull")),
    )

    repl._handle_models("models pull", console)

    rendered = output.getvalue()
    assert calls == [["qwen3:8b"]]
    assert "Downloading missing local model" in rendered
    assert "cancelled" not in rendered.lower()


def test_models_repair_downloads_without_a_second_approval(monkeypatch, tmp_path):
    console, output = _console_output()
    status = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama.exe"),
        server_running=True,
        missing_models=["qwen3:8b"],
    )
    monkeypatch.setattr(repl, "repair_runtime", lambda *a, **k: status)
    monkeypatch.setattr(repl, "collect_status", lambda *a, **k: status)
    calls = []
    monkeypatch.setattr(
        repl,
        "_pull_models_with_progress",
        lambda path, models, console: calls.append(list(models)) or {"qwen3:8b": 0},
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no approval prompt for explicit /models repair")),
    )

    repl._handle_models("models repair", console)

    rendered = output.getvalue()
    assert calls == [["qwen3:8b"]]
    assert "cancelled" not in rendered.lower()


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

    repl._handle_models("models pull", console)

    rendered = output.getvalue()
    assert calls[0][1] == ["qwen3:8b"]
    assert "qwen3:8b: installed" in rendered
    assert "Local Runtime" in rendered


def test_runtime_errors_point_to_models_repair():
    assert repl._looks_like_runtime_error("Could not connect to localhost:11434")
    assert repl._looks_like_runtime_error("model not found")


def test_models_tier_with_no_argument_shows_active_tier_and_options(tmp_path):
    console, output = _console_output()

    repl._handle_models("models tier", console, tmp_path)

    rendered = output.getvalue()
    assert "Active tier: default" in rendered
    assert "light" in rendered
    assert "heavy" in rendered


def test_models_tier_switches_and_persists_choice(monkeypatch, tmp_path):
    console, output = _console_output()
    monkeypatch.setattr(
        repl,
        "collect_status",
        lambda *args, **kwargs: RuntimeStatus(
            ollama_path=str(tmp_path / "ollama.exe"),
            server_running=True,
            missing_models=[],
        ),
    )

    repl._handle_models("models tier light", console, tmp_path)

    rendered = output.getvalue()
    assert "Switched to light tier" in rendered
    assert (tmp_path / ".shamsu" / "model_tier.json").exists()
    from shamsu.runtime.models import active_tier, ModelTier

    assert active_tier() is ModelTier.LIGHT


def test_models_tier_rejects_unknown_tier_name(tmp_path):
    console, output = _console_output()

    repl._handle_models("models tier ultra-mega", console, tmp_path)

    assert "Unknown tier" in output.getvalue()


def test_models_tier_without_workspace_reports_error():
    console, output = _console_output()

    repl._handle_models("models tier heavy", console, None)

    assert "No workspace available" in output.getvalue()
    assert not repl._looks_like_runtime_error("ordinary validation issue")


def test_models_use_switches_to_installed_model(monkeypatch, tmp_path):
    console, output = _console_output()
    monkeypatch.setattr(
        repl,
        "collect_status",
        lambda *args, **kwargs: RuntimeStatus(
            ollama_path=str(tmp_path / "ollama.exe"),
            server_running=True,
            installed_models=["llama3.1:8b"],
            missing_models=[],
        ),
    )

    repl._handle_models("models use llama3.1:8b", console, tmp_path)

    rendered = output.getvalue()
    assert "Using installed model for all roles" in rendered
    from shamsu.runtime.models import active_model_override, model_for_role

    assert active_model_override() == "llama3.1:8b"
    assert model_for_role("coder") == "llama3.1:8b"


def test_models_use_rejects_uninstalled_model(monkeypatch, tmp_path):
    console, output = _console_output()
    monkeypatch.setattr(
        repl,
        "collect_status",
        lambda *args, **kwargs: RuntimeStatus(
            ollama_path=str(tmp_path / "ollama.exe"),
            server_running=True,
            installed_models=["qwen3:8b"],
            missing_models=[],
        ),
    )

    repl._handle_models("models use llama3.1:8b", console, tmp_path)

    rendered = output.getvalue()
    assert "Model is not installed" in rendered
    from shamsu.runtime.models import active_model_override

    assert active_model_override() == ""


def test_models_use_tier_clears_model_override(monkeypatch, tmp_path):
    from shamsu.runtime.models import active_model_override, set_model_override

    set_model_override(tmp_path, "llama3.1:8b")
    console, output = _console_output()
    monkeypatch.setattr(
        repl,
        "collect_status",
        lambda *args, **kwargs: RuntimeStatus(
            ollama_path=str(tmp_path / "ollama.exe"),
            server_running=True,
            installed_models=["qwen3:8b"],
            missing_models=[],
        ),
    )

    repl._handle_models("models use tier", console, tmp_path)

    assert "Using default tier model selection" in output.getvalue()
    assert active_model_override() == ""


def test_models_use_completion_suggests_installed_models(monkeypatch):
    monkeypatch.setattr(repl, "_MODEL_COMPLETION_CACHE", (0.0, ()))
    monkeypatch.setattr(
        repl,
        "collect_status",
        lambda *args, **kwargs: RuntimeStatus(
            ollama_path="ollama",
            server_running=True,
            installed_models=["llama3.1:8b", "qwen3:8b"],
        ),
    )
    from prompt_toolkit.document import Document

    completions = list(repl.SlashCommandCompleter().get_completions(Document("/models use l"), None))

    assert [item.text for item in completions] == ["llama3.1:8b"]
