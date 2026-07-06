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


def test_ensure_ready_blocks_when_codebase_memory_unavailable(tmp_path):
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=False))

    gate = service.ensure_ready()

    assert gate.allowed is False
    assert gate.reason == REQUIRED_TOOL_MESSAGE
    assert "/abstract setup" in gate.reason


def test_status_and_repair_work_without_calling_ensure_ready(tmp_path):
    # /abstract status and /abstract repair must stay reachable even when the
    # gate would block ensure_ready(); the REPL also routes /doctor, /abstract *,
    # and /help before AgentOrchestrator so they never hit the gate at all.
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=False))

    status = service.status()
    repair_result = service.repair()

    assert status.normal_mode_allowed is False
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


def test_repair_rebuilds_index_once_available_again(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=False)
    service = AbstractService(tmp_path, adapter=adapter)

    result = service.repair()

    assert result["ok"] is True
    assert adapter.index_calls == 1
