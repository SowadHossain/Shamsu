from __future__ import annotations

from io import StringIO

from rich.console import Console

from shamsu.cli import repl
from shamsu.runtime.models import DEFAULT_TIER, ModelTier, active_tier, tier_ever_configured
from shamsu.runtime.ollama import RuntimeStatus
from shamsu.safety.approval import ask_tier_choice


def _console_output() -> tuple[Console, StringIO]:
    output = StringIO()
    return Console(file=output, force_terminal=False, width=120), output


def test_ask_tier_choice_returns_none_on_non_interactive_console():
    console, _ = _console_output()

    assert ask_tier_choice(console) is None


def test_tier_ever_configured_is_false_for_a_fresh_workspace(tmp_path):
    assert tier_ever_configured(tmp_path) is False


def test_tier_ever_configured_is_true_after_set_model_tier(tmp_path):
    from shamsu.runtime.models import set_model_tier

    set_model_tier(tmp_path, ModelTier.LIGHT)

    assert tier_ever_configured(tmp_path) is True


def test_first_run_prompts_and_pulls_when_no_tier_configured(monkeypatch, tmp_path):
    console, output = _console_output()
    monkeypatch.setattr(repl, "ask_tier_choice", lambda console: "light")
    monkeypatch.setattr(
        repl,
        "collect_status",
        lambda *args, **kwargs: RuntimeStatus(
            ollama_path=str(tmp_path / "ollama.exe"),
            server_running=True,
            missing_models=[],
        ),
    )

    repl._maybe_prompt_first_run_tier(tmp_path, console)

    assert active_tier() is ModelTier.LIGHT
    assert tier_ever_configured(tmp_path) is True
    assert "Using light tier" in output.getvalue()


def test_first_run_falls_back_to_default_tier_when_not_interactive(monkeypatch, tmp_path):
    console, output = _console_output()
    monkeypatch.setattr(repl, "ask_tier_choice", lambda console: None)
    monkeypatch.setattr(
        repl,
        "collect_status",
        lambda *args, **kwargs: RuntimeStatus(missing_models=[]),
    )

    repl._maybe_prompt_first_run_tier(tmp_path, console)

    assert active_tier() is DEFAULT_TIER
    assert "Using default tier" in output.getvalue()


def test_first_run_is_skipped_when_tier_already_configured(monkeypatch, tmp_path):
    from shamsu.runtime.models import set_model_tier

    set_model_tier(tmp_path, ModelTier.HEAVY)
    console, output = _console_output()

    def _fail(console):
        raise AssertionError("should not prompt when a tier was already configured")

    monkeypatch.setattr(repl, "ask_tier_choice", _fail)

    repl._maybe_prompt_first_run_tier(tmp_path, console)

    assert active_tier() is ModelTier.HEAVY
    assert output.getvalue() == ""


def test_first_run_is_skipped_when_env_var_pins_a_tier(monkeypatch, tmp_path):
    monkeypatch.setenv("SHAMSU_MODEL_TIER", "heavy")
    console, output = _console_output()

    def _fail(console):
        raise AssertionError("should not prompt when SHAMSU_MODEL_TIER is set")

    monkeypatch.setattr(repl, "ask_tier_choice", _fail)

    repl._maybe_prompt_first_run_tier(tmp_path, console)

    assert tier_ever_configured(tmp_path) is False
    assert output.getvalue() == ""


def test_pull_missing_models_reports_when_ollama_not_found(monkeypatch):
    console, output = _console_output()
    monkeypatch.setattr(repl, "collect_status", lambda *args, **kwargs: RuntimeStatus())

    repl._pull_missing_models_for_active_tier(console)

    assert "Ollama was not found" in output.getvalue()


def test_pull_missing_models_downloads_with_progress(monkeypatch, tmp_path):
    console, output = _console_output()
    calls = []
    status_before = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama.exe"), server_running=True, missing_models=["qwen3:8b"]
    )
    status_after = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama.exe"), server_running=True, missing_models=[]
    )
    statuses = iter([status_before, status_after])
    monkeypatch.setattr(repl, "collect_status", lambda *args, **kwargs: next(statuses))

    def pull(ollama_path, models, console):
        calls.append(models)
        return {"qwen3:8b": 0}

    monkeypatch.setattr(repl, "_pull_models_with_progress", pull)

    repl._pull_missing_models_for_active_tier(console)

    assert calls == [["qwen3:8b"]]
    assert "Downloading default tier model(s)" in output.getvalue()
