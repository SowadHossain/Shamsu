from __future__ import annotations

from rich.console import Console

from shamsu.abstract.service import AbstractService
from shamsu.cli.repl import _ensure_code_memory_ready_at_startup
from tests.test_abstract_service import FakeCodebaseMemoryAdapter


def _run(tmp_path, adapter, monkeypatch):
    monkeypatch.setattr(
        "shamsu.cli.repl.AbstractService",
        lambda workspace: AbstractService(workspace, adapter=adapter),
    )
    console = Console(record=True)
    _ensure_code_memory_ready_at_startup(tmp_path, console)
    return console.export_text()


def test_startup_shows_required_tool_message_when_unavailable(tmp_path, monkeypatch):
    output = _run(tmp_path, FakeCodebaseMemoryAdapter(available=False), monkeypatch)

    assert "Codebase-Memory MCP is not available" in output
    assert "degraded" in output


def test_startup_builds_index_automatically_when_missing(tmp_path, monkeypatch):
    adapter = FakeCodebaseMemoryAdapter(available=True)

    output = _run(tmp_path, adapter, monkeypatch)

    assert adapter.index_calls == 1
    assert "Code memory: ready" in output


def test_startup_shows_ready_without_rebuilding_when_already_fresh(tmp_path, monkeypatch):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    AbstractService(tmp_path, adapter=adapter).ensure_ready()

    output = _run(tmp_path, adapter, monkeypatch)

    assert adapter.index_calls == 1  # unchanged - no rebuild on the already-fresh path
    assert "Code memory: ready" in output
