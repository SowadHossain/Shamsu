"""Phase 5: local SQLite memory + MemoryService degraded fallback (no Graphiti)."""
from __future__ import annotations

from pathlib import Path

from shamsu.memory.service import DEGRADED_MEMORY_MESSAGE, MemoryService
from shamsu.memory.sqlite_store import SQLiteMemoryStore
from shamsu.memory.types import GraphitiHealth, MemoryFreshness, MemoryRecordStatus


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

    # Local SQLite is authoritative, so both gates allow normal operation.
    degraded = service.ensure_ready_degraded()
    assert degraded.allowed is True
    assert degraded.reason == DEGRADED_MEMORY_MESSAGE
    assert service.ensure_ready().allowed is True

    # Recall works via SQLite instead of returning empty.
    service.fallback.remember("Prefer search replace edits for small local models", "workflow_rule")
    hits = service.get_relevant("what edit format for a weak model")
    assert any("search replace" in m.text.lower() for m in hits)

    # forget routes to SQLite too.
    memory_id = service.fallback._all()[0].memory_id
    assert service.forget(memory_id)["ok"] is True


def test_memory_status_separates_local_success_from_graphiti_health(tmp_path: Path):
    status = MemoryService(tmp_path).status()

    assert status.normal_mode_allowed is True
    assert status.local_available is True
    assert status.degraded is False
    assert status.storage_mode == "local"


def test_sqlite_memory_records_authoritative_project_fields(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "m.db")

    result = store.remember(
        "The API layer lives in app/api.py",
        "project_fact",
        {"evidence_id": "ev-1"},
        project_id="demo",
        source="repository_evidence",
        confidence=1.0,
    )

    memory = store._all()[0]
    assert result["ok"] is True
    assert memory.kind == "project_fact"
    assert memory.text == "The API layer lives in app/api.py"
    assert memory.project_id == "demo"
    assert memory.source == "repository_evidence"
    assert memory.confidence == 1.0
    assert memory.freshness == MemoryFreshness.FRESH.value
    assert memory.status == MemoryRecordStatus.ACTIVE.value
    assert memory.last_verified_at > 0
    assert memory.metadata["evidence_id"] == "ev-1"


def test_stale_memory_is_not_recalled(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    created = store.remember("Use Redis for sessions", "architecture_note")
    assert store.get_relevant("Redis sessions")

    store.mark_stale(created["memory_id"], reason="repo evidence changed")

    assert store.get_relevant("Redis sessions") == []


def test_fresh_repository_evidence_overrides_old_memory(tmp_path: Path):
    store = SQLiteMemoryStore(tmp_path / "m.db")
    store.remember(
        "The API layer lives in old/api.py",
        "project_fact",
        project_id="demo",
        source="user_note",
        confidence=0.7,
    )

    fresh = store.remember_evidence(
        "The API layer lives in app/api.py",
        "project_fact",
        project_id="demo",
    )

    memories = store._all()
    old = next(memory for memory in memories if "old/api.py" in memory.text)
    hits = store.get_relevant("where does the API layer live", limit=5)
    assert fresh["ok"] is True
    assert old.status == MemoryRecordStatus.SUPERSEDED.value
    assert any("app/api.py" in memory.text for memory in hits)
    assert all("old/api.py" not in memory.text for memory in hits)


def test_memory_service_operates_with_graphiti_disabled_by_default(tmp_path: Path):
    service = MemoryService(tmp_path)

    result = service.remember_project_memory(
        kind="project_decision",
        content="Use SQLite for authoritative project memory",
        source="test",
        confidence=0.9,
    )
    search = service.search("authoritative project memory")

    assert service.graphiti_enabled is False
    assert result["ok"] is True
    assert search["storage_mode"] == "local"
    assert any("SQLite" in item for item in search["results"])
