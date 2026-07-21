from __future__ import annotations

from pathlib import Path

from shamsu.abstract.service import REQUIRED_TOOL_MESSAGE, AbstractService
from shamsu.abstract.types import CodebaseMemoryHealth


class FakeCodebaseMemoryAdapter:
    """Fake adapter for unit tests - never touches the real upstream binary."""

    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.index_calls = 0
        self.refresh_calls = 0

    def healthcheck(self, workspace: Path) -> CodebaseMemoryHealth:
        if self.available:
            return CodebaseMemoryHealth(available=True, binary_path="/fake/codebase-memory-mcp", message="ready")
        return CodebaseMemoryHealth(available=False, message="Codebase-Memory MCP binary was not found.")

    def index_workspace(self, workspace: Path) -> dict:
        self.index_calls += 1
        return {"ok": True, "status": "indexed"}

    def refresh_workspace(self, workspace: Path) -> dict:
        self.refresh_calls += 1
        return {"ok": True, "status": "refreshed"}

    def setup(self, workspace: Path) -> dict:
        self.available = True
        return {"ok": True, "path": "/fake/codebase-memory-mcp"}

    def repair(self, workspace: Path) -> dict:
        if not self.available:
            self.setup(workspace)
        return {"ok": self.available, "message": "ok" if self.available else "still missing"}


def test_ensure_ready_allows_degraded_mode_when_codebase_memory_unavailable(tmp_path):
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=False))

    gate = service.ensure_ready()

    assert gate.allowed is True
    assert gate.reason == REQUIRED_TOOL_MESSAGE
    assert "/abstract setup" in gate.reason
    assert gate.status is not None
    assert gate.status.degraded is True
    assert gate.status.retrieval_mode == "local"


def test_status_and_repair_work_without_calling_ensure_ready(tmp_path):
    # /abstract status and /abstract repair must stay reachable even when the
    # gate would block ensure_ready(); the REPL also routes /doctor, /abstract *,
    # and /help before AgentOrchestrator so they never hit the gate at all.
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=False))

    status = service.status()
    repair_result = service.repair()

    assert status.normal_mode_allowed is True
    assert status.degraded is True
    assert repair_result["ok"] is True  # fake adapter flips available=True on setup


def test_ensure_ready_builds_index_when_missing(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    service = AbstractService(tmp_path, adapter=adapter)

    gate = service.ensure_ready()

    assert gate.allowed is True
    assert adapter.index_calls == 1
    assert adapter.refresh_calls == 0
    assert service.index_status().stale is False


def test_ensure_ready_refreshes_when_stale(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    service = AbstractService(tmp_path, adapter=adapter)
    service.ensure_ready()  # builds the initial index
    (tmp_path / "new_file.py").write_text("print(1)\n", encoding="utf-8")

    gate = service.ensure_ready()

    assert gate.allowed is True
    assert adapter.refresh_calls == 1


def test_ensure_ready_does_not_refresh_when_fresh(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    service = AbstractService(tmp_path, adapter=adapter)
    service.ensure_ready()

    service.ensure_ready()

    assert adapter.index_calls == 1
    assert adapter.refresh_calls == 0


def test_mark_stale_forces_refresh_on_next_ensure_ready(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    service = AbstractService(tmp_path, adapter=adapter)
    service.ensure_ready()

    service.mark_stale()
    assert service.index_status().stale is True

    service.ensure_ready()
    assert adapter.refresh_calls == 1


def test_multiple_writes_in_one_task_are_debounced_into_one_refresh(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    service = AbstractService(tmp_path, adapter=adapter)
    service.ensure_ready()

    service.mark_stale()
    service.mark_stale()
    service.mark_stale()
    service.ensure_ready()

    assert adapter.refresh_calls == 1


def test_mark_stale_updates_status_and_debounces_generation(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    service = AbstractService(tmp_path, adapter=adapter)
    service.ensure_ready()

    service.mark_stale()
    first = service._read_json(service._last_index_path())
    status = service._read_json(service._status_path())
    service.mark_stale()
    second = service._read_json(service._last_index_path())

    assert status["index"]["stale"] is True
    assert status["index"]["workspace_generation"] == 1
    assert first["workspace_generation"] == second["workspace_generation"] == 1


def test_manifest_detects_file_set_change_even_when_count_is_unchanged(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    first = tmp_path / "first.py"
    first.write_text("value = 1\n", encoding="utf-8")
    service = AbstractService(tmp_path, adapter=adapter)
    service.ensure_ready()

    first.unlink()
    (tmp_path / "second.py").write_text("value = 1\n", encoding="utf-8")

    assert service.index_status().stale is True


def test_internal_shamsu_changes_do_not_make_index_stale(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    service = AbstractService(tmp_path, adapter=adapter)
    service.ensure_ready()

    internal = tmp_path / ".shamsu" / "mutations"
    internal.mkdir(parents=True)
    (internal / "backup.py").write_text("value = 2\n", encoding="utf-8")

    assert service.index_status().stale is False


def test_repair_rebuilds_index_once_available_again(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=False)
    service = AbstractService(tmp_path, adapter=adapter)

    result = service.repair()

    assert result["ok"] is True
    assert adapter.index_calls == 1
