from __future__ import annotations

import asyncio

import pytest

from shamsu.llm.manager import _MODEL_PULL_LOCKS, LLMManager, ModelPullProgress
from shamsu.runtime.models import model_for_role
from shamsu.types import ContextPack


class _StubLLMManager(LLMManager):
    """Skips the real network call in _generate; only exercises _ensure_model."""

    async def _generate(self, model, system, prompt, **kwargs):
        return "stub response"


def _patch_runtime(monkeypatch, tmp_path, installed_models):
    ollama_path = tmp_path / "ollama.exe"
    monkeypatch.setattr(
        "shamsu.runtime.ollama.find_ollama_executable", lambda *args, **kwargs: ollama_path
    )
    monkeypatch.setattr(
        "shamsu.runtime.ollama.list_installed_models",
        lambda _path: list(installed_models),
    )
    return ollama_path


@pytest.mark.asyncio
async def test_ensure_model_skips_pull_when_already_installed(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path, installed_models=["qwen3:8b"])
    pull_calls = []
    monkeypatch.setattr(
        "shamsu.runtime.ollama.ensure_model_available",
        lambda *args, **kwargs: pull_calls.append(args) or True,
    )

    manager = _StubLLMManager()
    await manager._ensure_model("qwen3:8b")

    assert pull_calls == []


@pytest.mark.asyncio
async def test_ensure_model_pulls_when_missing_and_logs(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path, installed_models=[])
    pull_calls = []
    monkeypatch.setattr(
        "shamsu.runtime.ollama.ensure_model_available",
        lambda _path, model_name, _cb=None: pull_calls.append(model_name) or True,
    )

    class RecordingLogger:
        def __init__(self):
            self.events = []

        def log(self, event_type, payload, summary, workflow_id=None):
            self.events.append(event_type)

    logger = RecordingLogger()
    manager = _StubLLMManager(session_logger=logger)
    await manager._ensure_model("qwen2.5-coder:7b-instruct")

    assert pull_calls == ["qwen2.5-coder:7b-instruct"]
    assert "model.pull.started" in logger.events
    assert "model.pull.finished" in logger.events


@pytest.mark.asyncio
async def test_ensure_model_reports_progress_via_hooks(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path, installed_models=[])

    def fake_ensure(_path, model_name, progress_callback=None):
        if progress_callback:
            progress_callback("chunk-1")
            progress_callback("chunk-2")
        return True

    monkeypatch.setattr("shamsu.runtime.ollama.ensure_model_available", fake_ensure)

    events: list[tuple[str, ...]] = []
    progress = ModelPullProgress(
        on_start=lambda model: events.append(("start", model)),
        on_chunk=lambda model, chunk: events.append(("chunk", model, chunk)),
        on_finish=lambda model, success: events.append(("finish", model, str(success))),
    )
    manager = _StubLLMManager(model_pull_progress=progress)

    await manager._ensure_model("qwen2.5-coder:7b-instruct")

    assert events[0] == ("start", "qwen2.5-coder:7b-instruct")
    assert ("chunk", "qwen2.5-coder:7b-instruct", "chunk-1") in events
    assert ("chunk", "qwen2.5-coder:7b-instruct", "chunk-2") in events
    assert events[-1] == ("finish", "qwen2.5-coder:7b-instruct", "True")


@pytest.mark.asyncio
async def test_ensure_model_does_nothing_when_ollama_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "shamsu.runtime.ollama.find_ollama_executable", lambda *args, **kwargs: None
    )
    pull_calls = []
    monkeypatch.setattr(
        "shamsu.runtime.ollama.ensure_model_available",
        lambda *args, **kwargs: pull_calls.append(args) or True,
    )

    manager = _StubLLMManager()
    await manager._ensure_model("qwen3:8b")

    assert pull_calls == []


@pytest.mark.asyncio
async def test_run_specialist_ensures_model_before_generating(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path, installed_models=[])
    calls = []
    monkeypatch.setattr(
        "shamsu.runtime.ollama.ensure_model_available",
        lambda _path, model_name, _cb=None: calls.append(model_name) or True,
    )

    manager = _StubLLMManager()
    pack = ContextPack(task_id="t1", step_id=1, specialist="coder", user_request="do it")
    await manager.run_specialist("coder", pack)

    # Asserted against the resolver rather than a literal name: what matters is
    # that the role's model is ensured before generating, not which model a given
    # tier or single-model default happens to pick.
    assert calls == [model_for_role("coder")]


@pytest.mark.asyncio
async def test_route_ensures_router_model_before_routing(monkeypatch, tmp_path):
    _patch_runtime(monkeypatch, tmp_path, installed_models=[])
    calls = []
    monkeypatch.setattr(
        "shamsu.runtime.ollama.ensure_model_available",
        lambda _path, model_name, _cb=None: calls.append(model_name) or True,
    )

    class RoutingStub(_StubLLMManager):
        async def _generate(self, model, system, prompt, **kwargs):
            return '{"intent": "qa", "complexity": "single", "confidence": 0.9}'

    manager = RoutingStub()
    await manager.route("hello", "a project")

    assert calls == [model_for_role("router")]


@pytest.mark.asyncio
async def test_concurrent_ensure_model_calls_do_not_double_pull(monkeypatch, tmp_path):
    _MODEL_PULL_LOCKS.pop("qwen3:8b", None)
    ollama_path = tmp_path / "ollama.exe"
    installed: set[str] = set()
    pull_calls = []

    monkeypatch.setattr(
        "shamsu.runtime.ollama.find_ollama_executable", lambda *args, **kwargs: ollama_path
    )
    monkeypatch.setattr(
        "shamsu.runtime.ollama.list_installed_models", lambda _path: list(installed)
    )

    def fake_ensure(_path, model_name, _cb=None):
        pull_calls.append(model_name)
        installed.add(model_name)
        return True

    monkeypatch.setattr("shamsu.runtime.ollama.ensure_model_available", fake_ensure)

    manager = _StubLLMManager()
    await asyncio.gather(
        manager._ensure_model("qwen3:8b"),
        manager._ensure_model("qwen3:8b"),
    )

    assert pull_calls == ["qwen3:8b"]
