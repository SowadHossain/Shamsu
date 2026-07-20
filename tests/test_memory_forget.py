"""Tests for Graphiti forget() (G11): tombstone + exclude-from-recall, so a wrong
fact can be evicted and never recalled again. Runs against the SQLite fallback
(Graphiti isn't configured in tests), which is exactly the degraded path users
hit without a backend."""
from __future__ import annotations

from pathlib import Path

from shamsu.memory.service import MemoryService
from shamsu.memory.types import LongTermMemory


def _svc(tmp_path: Path) -> MemoryService:
    service = MemoryService(tmp_path)
    assert service._use_fallback(), "no Graphiti in tests -> SQLite fallback"
    return service


def test_tombstone_excludes_a_live_memory_from_recall(tmp_path: Path):
    svc = _svc(tmp_path)
    fact = "The staging database password is hunter2unique"
    assert svc.remember(fact, kind="task_summary").get("ok")
    assert any("hunter2unique" in m.text for m in svc.get_relevant("database password"))

    # Tombstone only (no store deletion) -> still excluded from recall.
    svc._add_tombstone(fact)
    assert not any("hunter2unique" in m.text for m in svc.get_relevant("database password"))

    # Un-tombstone -> recalled again.
    svc._remove_tombstone(fact)
    assert any("hunter2unique" in m.text for m in svc.get_relevant("database password"))


def test_forget_then_not_recalled_then_re_remember(tmp_path: Path):
    svc = _svc(tmp_path)
    fact = "The API base url is https://old-wrong-host.example"
    svc.remember(fact, kind="task_summary")
    assert any("old-wrong-host" in m.text for m in svc.get_relevant("API base url"))

    out = svc.forget(fact)
    assert out["ok"] is True and out["tombstoned"] is True
    assert not any("old-wrong-host" in m.text for m in svc.get_relevant("API base url"))

    # Deliberately re-remembering the corrected fact un-forgets it.
    svc.remember(fact, kind="task_summary")
    assert any("old-wrong-host" in m.text for m in svc.get_relevant("API base url"))


def test_tombstone_is_also_applied_to_search(tmp_path: Path):
    svc = _svc(tmp_path)
    fact = "The retired queue name is legacy-work-items"
    svc.remember(fact, kind="architecture_note")
    svc._add_tombstone("legacy-work-items")

    result = svc.search("retired queue")

    assert all("legacy-work-items" not in str(item) for item in result["results"])

def test_forget_empty_value_is_rejected(tmp_path: Path):
    svc = _svc(tmp_path)
    out = svc.forget("   ")
    assert out["ok"] is False


def test_tombstones_persist_across_instances(tmp_path: Path):
    MemoryService(tmp_path)._add_tombstone("forbidden fact phrase here")
    reloaded = MemoryService(tmp_path)._load_tombstones()
    assert "forbidden fact phrase here" in reloaded["ids"]


def test_is_tombstoned_matches_id_exact_and_phrase(tmp_path: Path):
    svc = MemoryService(tmp_path)
    svc._add_tombstone("mem-abc-1234")                 # id-like
    svc._add_tombstone("the secret token value phrase")  # distinctive phrase

    assert svc._is_tombstoned(LongTermMemory(kind="fact", text="x", memory_id="mem-abc-1234"))
    assert svc._is_tombstoned(LongTermMemory(kind="fact", text="THE Secret Token Value Phrase"))
    # substring phrase match inside a longer memory
    assert svc._is_tombstoned(
        LongTermMemory(kind="fact", text="note: the secret token value phrase is stale")
    )
    # unrelated memory is not excluded
    assert not svc._is_tombstoned(LongTermMemory(kind="fact", text="something else", memory_id="zzz"))


def test_no_tombstones_means_nothing_excluded(tmp_path: Path):
    svc = MemoryService(tmp_path)
    assert svc._is_tombstoned(LongTermMemory(kind="fact", text="anything", memory_id="id1")) is False
