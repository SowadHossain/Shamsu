"""Phase 5: local SQLite memory + MemoryService degraded fallback (no Graphiti)."""
from __future__ import annotations

from pathlib import Path

from shamsu.memory.service import DEGRADED_MEMORY_MESSAGE, MemoryService
from shamsu.memory.sqlite_store import SQLiteMemoryStore
from shamsu.memory.types import GraphitiHealth


class UnhealthyAdapter:
    def healthcheck(self, workspace) -> GraphitiHealth:
        return GraphitiHealth(available=False, message="graphiti not installed")


def test_sqlite_store_remember_and_recall(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    store.remember("Use tabs not spaces for indentation in this project", "project_decision")
    store.remember("The leaderboard uses a remote Postgres database", "architecture_note")
    hits = store.get_relevant("what indentation should I use here", limit=5)
    assert any("tabs" in m.text.lower() for m in hits)


def test_sqlite_store_dedupes_identical(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    store.remember("Ball speeds up after each paddle hit", "bug_lesson")
    second = store.remember("Ball speeds up after each paddle hit", "bug_lesson")
    assert second.get("deduped") is True


def test_sqlite_store_dedupes_by_content_and_source_run(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    text = "Use the parser bounds check"

    first = store.remember(text, "bug_lesson", {"source_run_id": "run-1"})
    duplicate = store.remember(text, "bug_lesson", {"source_run_id": "run-1"})
    another_source = store.remember(text, "bug_lesson", {"source_run_id": "run-2"})

    assert first.get("memory_id")
    assert duplicate.get("deduped") is True
    assert another_source.get("deduped") is not True
    assert len(store._all()) == 2


def test_sqlite_store_forget(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    created = store.remember("remove this note", "user_preference")
    result = store.forget(created["memory_id"])
    assert result["ok"] and result["deleted"] == 1
    assert store.get_relevant("remove this note") == []


def test_memory_service_runs_degraded_without_graphiti(tmp_path: Path):
    service = MemoryService(tmp_path, adapter=UnhealthyAdapter())
    assert service._use_fallback() is True

    # Degraded gate ALLOWS startup (the whole point); strict gate still blocks.
    degraded = service.ensure_ready_degraded()
    assert degraded.allowed is True
    assert degraded.reason == DEGRADED_MEMORY_MESSAGE
    assert service.ensure_ready().allowed is False

    # Recall works via SQLite instead of returning empty.
    service.fallback.remember("Prefer search replace edits for small local models", "workflow_rule")
    hits = service.get_relevant("what edit format for a weak model")
    assert any("search replace" in m.text.lower() for m in hits)

    # forget routes to SQLite too.
    memory_id = service.fallback._all()[0].memory_id
    assert service.forget(memory_id)["ok"] is True


def test_memory_status_separates_local_success_from_graphiti_health(tmp_path: Path):
    status = MemoryService(tmp_path, adapter=UnhealthyAdapter()).status()

    assert status.normal_mode_allowed is True
    assert status.local_available is True
    assert status.degraded is True
    assert status.storage_mode == "local"
