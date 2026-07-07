from __future__ import annotations

from pathlib import Path

from shamsu.taskmaster.service import (
    STATUS_BLOCKED,
    STATUS_DEFERRED,
    STATUS_DONE,
    STATUS_PENDING,
    TaskmasterService,
)
from shamsu.taskmaster.types import TaskmasterHealth, TaskmasterTask


class FakeTaskmasterAdapter:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.tool_dir = Path("/fake/tools/taskmaster")
        self.parse_calls: list[Path] = []
        self.set_status_calls: list[tuple[str, str]] = []
        self.tasks: dict[str, TaskmasterTask] = {}

    def healthcheck(self, workspace: Path) -> TaskmasterHealth:
        if self.available:
            return TaskmasterHealth(True, node_path="/fake/node", cli_path="/fake/task-master.js", version="0.fake", message="ready")
        return TaskmasterHealth(False, message="Taskmaster missing")

    def is_initialized(self, workspace: Path) -> bool:
        return bool(self.tasks)

    def status(self, workspace: Path) -> dict:
        return {"initialized": bool(self.tasks), "task_count": len(self.tasks), "status_counts": {}}

    def setup(self, workspace: Path, project_name: str = "") -> dict:
        self.available = True
        return {"ok": True}

    def repair(self, workspace: Path) -> dict:
        self.available = True
        return {"ok": True}

    def parse_prd(self, workspace: Path, prd_path: Path, num_tasks=None) -> dict:
        self.parse_calls.append(Path(prd_path))
        self.tasks = {
            "1": TaskmasterTask(id="1", title="First task", status="pending", dependencies=[]),
            "2": TaskmasterTask(id="2", title="Second task", status="pending", dependencies=["1"]),
        }
        return {"ok": True}

    def list_tasks(self, workspace: Path, status=None) -> dict:
        tasks = list(self.tasks.values())
        if status:
            tasks = [task for task in tasks if task.status == status]
        return {"ok": True, "tasks": tasks}

    def show_task(self, workspace: Path, task_id: str) -> dict:
        task = self.tasks.get(str(task_id))
        if task is None:
            return {"ok": False, "error": f"Task {task_id} was not found."}
        return {"ok": True, "task": task}

    def next_task(self, workspace: Path) -> dict:
        for task in self.tasks.values():
            if task.status == "pending" and not task.dependencies:
                return {"ok": True, "task": task}
        return {"ok": True, "task": None}

    def set_status(self, workspace: Path, task_id: str, status: str) -> dict:
        self.set_status_calls.append((str(task_id), status))
        if str(task_id) in self.tasks:
            old = self.tasks[str(task_id)]
            self.tasks[str(task_id)] = TaskmasterTask(
                id=old.id, title=old.title, description=old.description, details=old.details,
                test_strategy=old.test_strategy, priority=old.priority, status=status,
                dependencies=old.dependencies, subtasks=old.subtasks,
            )
        return {"ok": True}


def _write_prd(tmp_path: Path, text: str = "some prd content") -> Path:
    path = tmp_path / "prd.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_ensure_ready_reports_missing_taskmaster(tmp_path):
    service = TaskmasterService(tmp_path, adapter=FakeTaskmasterAdapter(available=False))

    ready, reason = service.ensure_ready()

    assert ready is False
    assert "/taskmaster setup" in reason


def test_parse_prd_calls_adapter_once_and_lists_tasks(tmp_path):
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    prd = _write_prd(tmp_path)

    result = service.parse_prd(prd)

    assert result["ok"] is True
    assert len(adapter.parse_calls) == 1
    assert len(result["tasks"]) == 2


def test_parse_prd_does_not_reparse_unchanged_prd(tmp_path):
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    prd = _write_prd(tmp_path)

    first = service.parse_prd(prd)
    second = service.parse_prd(prd)

    assert first["reused_cache"] is False
    assert second["reused_cache"] is True
    assert len(adapter.parse_calls) == 1  # Taskmaster was only actually invoked once.


def test_reparse_only_happens_when_prd_content_changes_or_forced(tmp_path):
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    prd = _write_prd(tmp_path, "version 1")

    service.parse_prd(prd)
    assert len(adapter.parse_calls) == 1

    # Same content -> still cached, no reparse.
    service.parse_prd(prd)
    assert len(adapter.parse_calls) == 1

    # Content changed -> reparses automatically.
    prd.write_text("version 2", encoding="utf-8")
    result = service.parse_prd(prd)
    assert result["reused_cache"] is False
    assert len(adapter.parse_calls) == 2

    # force=True reparses even with unchanged content.
    service.parse_prd(prd, force=True)
    assert len(adapter.parse_calls) == 3


def test_incomplete_dependencies_blocks_execution(tmp_path):
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    service.parse_prd(_write_prd(tmp_path))
    listing = service.list_tasks()["tasks"]
    task_2 = next(task for task in listing if task.id == "2")

    incomplete = service.incomplete_dependencies(task_2, listing)

    assert incomplete == ["1"]


def test_incomplete_dependencies_empty_once_dependency_is_done(tmp_path):
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    service.parse_prd(_write_prd(tmp_path))
    service.mark_done("1")
    listing = service.list_tasks()["tasks"]
    task_2 = next(task for task in listing if task.id == "2")

    incomplete = service.incomplete_dependencies(task_2, listing)

    assert incomplete == []


def test_mark_blocked_sets_real_taskmaster_status_and_records_reason(tmp_path):
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    service.parse_prd(_write_prd(tmp_path))

    service.mark_blocked("2", "waiting on task 1")

    assert adapter.set_status_calls[-1] == ("2", STATUS_BLOCKED)
    record = service.run_record("2")
    assert record["last_error"] == "waiting on task 1"


def test_mark_failed_keeps_task_retryable_until_max_retries(tmp_path):
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    service.parse_prd(_write_prd(tmp_path))

    first = service.mark_failed("1", "flaky test", max_retries=2)
    assert first["next_status"] == STATUS_PENDING
    assert adapter.set_status_calls[-1] == ("1", STATUS_PENDING)

    second = service.mark_failed("1", "flaky test again", max_retries=2)
    assert second["next_status"] == STATUS_DEFERRED
    assert adapter.set_status_calls[-1] == ("1", STATUS_DEFERRED)
    assert service.run_record("1")["retry_count"] == 2


def test_mark_done_sets_done_status(tmp_path):
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    service.parse_prd(_write_prd(tmp_path))

    service.mark_done("1", note="verified via pytest")

    assert adapter.set_status_calls[-1] == ("1", STATUS_DONE)
    assert service.run_record("1")["last_status"] == STATUS_DONE


def test_plan_reports_executable_and_blocked_tasks(tmp_path):
    adapter = FakeTaskmasterAdapter()
    service = TaskmasterService(tmp_path, adapter=adapter)
    service.parse_prd(_write_prd(tmp_path))

    plan = service.plan()

    rows = {row["id"]: row for row in plan["tasks"]}
    assert rows["1"]["executable"] is True
    assert rows["2"]["executable"] is False
    assert rows["2"]["blocked_by"] == ["1"]
