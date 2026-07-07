"""Workspace service around the Taskmaster adapter.

Owns SHAMSU's own `.shamsu/taskmaster/` bookkeeping - a health/status
snapshot, a PRD-hash cache (so a PRD is not reparsed on every prompt), and a
per-task execution run record (retry count / last failure reason). None of
this duplicates Taskmaster's own job: task breakdown, dependencies, status,
and execution order all continue to live in Taskmaster's own
`.taskmaster/tasks/tasks.json`, read fresh through the adapter every time.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from shamsu.taskmaster.adapter import TaskmasterAdapter
from shamsu.taskmaster.types import TaskmasterStatus, TaskmasterTask

REQUIRED_TASKMASTER_MESSAGE = (
    "Taskmaster is required for PRD/task-graph workflows but is not available.\n\n"
    "Run: /taskmaster setup\n\n"
    "or: /taskmaster repair\n\n"
    "SHAMSU will not run /prd or /tasks execution workflows until local Taskmaster is ready."
)

# Real Taskmaster status values, verified against task-master-ai@0.43.1
# (`set-status` error message enumerates them): pending, in-progress, done,
# deferred, cancelled, blocked, review. There is no native "failed" status -
# a failed attempt keeps the task retryable (pending) and SHAMSU records the
# failure reason/retry count itself instead of inventing a fake status.
STATUS_BLOCKED = "blocked"
STATUS_DONE = "done"
STATUS_DEFERRED = "deferred"
STATUS_PENDING = "pending"
STATUS_IN_PROGRESS = "in-progress"

DEFAULT_MAX_RETRIES = 3


class TaskmasterService:
    def __init__(self, workspace: Path, adapter: TaskmasterAdapter | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.adapter = adapter or TaskmasterAdapter()
        self.state_dir = self.workspace / ".shamsu" / "taskmaster"

    # -- paths ------------------------------------------------------------------

    def _status_path(self) -> Path:
        return self.state_dir / "status.json"

    def _prd_cache_path(self) -> Path:
        return self.state_dir / "prd-cache.json"

    def _events_path(self) -> Path:
        return self.state_dir / "taskmaster-events.jsonl"

    def _run_record_path(self, task_id: str) -> Path:
        return self.state_dir / "runs" / f"{task_id}.json"

    # -- json helpers -------------------------------------------------------------

    def _write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _read_json(self, path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _log_event(self, event: str, detail: dict[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"event": event, "ts": time.time(), **detail}, ensure_ascii=True)
        with self._events_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    # -- health/status ------------------------------------------------------------

    def healthcheck(self):
        return self.adapter.healthcheck(self.workspace)

    def status(self) -> TaskmasterStatus:
        raw = self.adapter.status(self.workspace)
        health = self.adapter.healthcheck(self.workspace)
        status = TaskmasterStatus(
            workspace=str(self.workspace),
            health=health,
            initialized=bool(raw.get("initialized")),
            tag="master",
            task_count=int(raw.get("task_count", 0)),
            status_counts=raw.get("status_counts", {}),
            normal_mode_allowed=health.ok,
        )
        self._write_json(self._status_path(), status.to_dict())
        return status

    def ensure_ready(self) -> tuple[bool, str]:
        health = self.healthcheck()
        if not health.ok:
            return False, REQUIRED_TASKMASTER_MESSAGE
        return True, ""

    def setup(self, project_name: str = "") -> dict[str, Any]:
        result = self.adapter.setup(self.workspace, project_name=project_name)
        self._log_event("setup", {"ok": bool(result.get("ok"))})
        self.status()
        return result

    def repair(self) -> dict[str, Any]:
        result = self.adapter.repair(self.workspace)
        self._log_event("repair", {"ok": bool(result.get("ok"))})
        self.status()
        return result

    # -- PRD parsing with caching --------------------------------------------------

    def parse_prd(self, prd_path: Path, num_tasks: int | None = None, force: bool = False) -> dict[str, Any]:
        """Parse a PRD through Taskmaster, but only actually invoke Taskmaster
        when the PRD content changed (or `force`/first parse) - satisfies
        "do not reparse PRD every prompt" / "cache parsed tasks and reuse them"."""
        resolved = Path(prd_path).resolve()
        digest = _sha256_file(resolved)
        cache = self._read_json(self._prd_cache_path())
        unchanged = (
            not force
            and cache.get("prd_path") == str(resolved)
            and cache.get("sha256") == digest
            and self.adapter.is_initialized(self.workspace)
        )
        if unchanged:
            listing = self.adapter.list_tasks(self.workspace)
            return {
                "ok": listing.get("ok", False),
                "reused_cache": True,
                "tasks": listing.get("tasks", []),
                "error": listing.get("error", ""),
            }
        result = self.adapter.parse_prd(self.workspace, resolved, num_tasks=num_tasks)
        self._log_event("prd_parsed", {"ok": bool(result.get("ok")), "prd_path": str(resolved)})
        if not result.get("ok"):
            return {"ok": False, "reused_cache": False, "tasks": [], "error": result.get("error", "")}
        self._write_json(self._prd_cache_path(), {
            "prd_path": str(resolved), "sha256": digest, "parsed_at": time.time(),
        })
        listing = self.adapter.list_tasks(self.workspace)
        self._log_event("tasks_created", {"count": len(listing.get("tasks", []))})
        return {"ok": listing.get("ok", False), "reused_cache": False, "tasks": listing.get("tasks", []), "error": listing.get("error", "")}

    def last_prd_info(self) -> dict[str, Any]:
        return self._read_json(self._prd_cache_path())

    def prd_changed(self, prd_path: Path) -> bool:
        resolved = Path(prd_path).resolve()
        cache = self._read_json(self._prd_cache_path())
        if cache.get("prd_path") != str(resolved):
            return True
        return cache.get("sha256") != _sha256_file(resolved)

    # -- task queue -----------------------------------------------------------------

    def list_tasks(self, status: str | None = None) -> dict[str, Any]:
        return self.adapter.list_tasks(self.workspace, status=status)

    def show_task(self, task_id: str) -> dict[str, Any]:
        return self.adapter.show_task(self.workspace, task_id)

    def next_task(self) -> dict[str, Any]:
        return self.adapter.next_task(self.workspace)

    def dependencies(self, task_id: str) -> dict[str, Any]:
        shown = self.show_task(task_id)
        if not shown.get("ok"):
            return shown
        task: TaskmasterTask = shown["task"]
        listing = self.list_tasks()
        if not listing.get("ok"):
            return listing
        by_id = {other.id: other for other in listing["tasks"]}
        deps = [
            {"id": dep_id, "status": by_id[dep_id].status if dep_id in by_id else "unknown"}
            for dep_id in task.dependencies
        ]
        return {"ok": True, "task": task, "dependencies": deps}

    def incomplete_dependencies(self, task: TaskmasterTask, all_tasks: list[TaskmasterTask]) -> list[str]:
        by_id = {other.id: other for other in all_tasks}
        return [dep_id for dep_id in task.dependencies if by_id.get(dep_id) and by_id[dep_id].status != STATUS_DONE]

    def plan(self) -> dict[str, Any]:
        listing = self.list_tasks()
        if not listing.get("ok"):
            return listing
        tasks: list[TaskmasterTask] = listing["tasks"]
        by_id = {task.id: task for task in tasks}
        rows = []
        for task in tasks:
            blocked_by = [dep for dep in task.dependencies if by_id.get(dep) and by_id[dep].status != STATUS_DONE]
            rows.append({
                "id": task.id, "title": task.title, "status": task.status,
                "priority": task.priority, "dependencies": task.dependencies,
                "blocked_by": blocked_by, "executable": task.status == STATUS_PENDING and not blocked_by,
            })
        return {"ok": True, "tasks": rows}

    # -- status transitions + SHAMSU-side run bookkeeping --------------------------

    def run_record(self, task_id: str) -> dict[str, Any]:
        return self._read_json(self._run_record_path(task_id)) or {"task_id": task_id, "retry_count": 0, "history": []}

    def _save_run_record(self, task_id: str, record: dict[str, Any]) -> None:
        self._write_json(self._run_record_path(task_id), record)

    def mark_in_progress(self, task_id: str) -> dict[str, Any]:
        result = self.adapter.set_status(self.workspace, task_id, STATUS_IN_PROGRESS)
        self._log_event("task_status_changed", {"task_id": task_id, "status": STATUS_IN_PROGRESS})
        return result

    def mark_done(self, task_id: str, note: str = "") -> dict[str, Any]:
        result = self.adapter.set_status(self.workspace, task_id, STATUS_DONE)
        record = self.run_record(task_id)
        record["last_status"] = STATUS_DONE
        record["history"] = [*record.get("history", []), {"status": STATUS_DONE, "note": note, "ts": time.time()}]
        self._save_run_record(task_id, record)
        self._log_event("task_done", {"task_id": task_id, "note": note})
        return result

    def mark_blocked(self, task_id: str, reason: str) -> dict[str, Any]:
        result = self.adapter.set_status(self.workspace, task_id, STATUS_BLOCKED)
        record = self.run_record(task_id)
        record["last_status"] = STATUS_BLOCKED
        record["last_error"] = reason
        record["history"] = [*record.get("history", []), {"status": STATUS_BLOCKED, "reason": reason, "ts": time.time()}]
        self._save_run_record(task_id, record)
        self._log_event("task_blocked", {"task_id": task_id, "reason": reason})
        return result

    def mark_failed(self, task_id: str, reason: str, max_retries: int = DEFAULT_MAX_RETRIES) -> dict[str, Any]:
        """Taskmaster has no native "failed" status. A failed attempt is kept
        retryable (`pending`) until `max_retries` is exceeded, at which point
        it's set to `deferred` (Taskmaster's own "not now" status) so it stops
        being picked up by `/tasks next`/`/tasks continue` automatically. The
        failure reason and retry count are SHAMSU-side bookkeeping, not a
        second task queue."""
        record = self.run_record(task_id)
        retry_count = int(record.get("retry_count", 0)) + 1
        record["retry_count"] = retry_count
        record["last_status"] = "failed"
        record["last_error"] = reason
        record["history"] = [*record.get("history", []), {"status": "failed", "reason": reason, "ts": time.time()}]
        self._save_run_record(task_id, record)
        next_status = STATUS_DEFERRED if retry_count >= max_retries else STATUS_PENDING
        result = self.adapter.set_status(self.workspace, task_id, next_status)
        self._log_event("task_failed", {"task_id": task_id, "reason": reason, "retry_count": retry_count, "next_status": next_status})
        return {**result, "retry_count": retry_count, "next_status": next_status}


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""
