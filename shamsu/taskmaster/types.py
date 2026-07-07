"""Dataclasses for the Taskmaster adapter/service boundary."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TaskmasterHealth:
    available: bool
    node_path: str = ""
    cli_path: str = ""
    version: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.available

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "node_path": self.node_path,
            "cli_path": self.cli_path,
            "version": self.version,
            "message": self.message,
        }


@dataclass(frozen=True)
class TaskmasterTask:
    id: str
    title: str = ""
    description: str = ""
    details: str = ""
    test_strategy: str = ""
    priority: str = ""
    status: str = "pending"
    dependencies: list[str] = field(default_factory=list)
    subtasks: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_json(payload: dict[str, Any]) -> "TaskmasterTask":
        return TaskmasterTask(
            id=str(payload.get("id", "")),
            title=str(payload.get("title", "")),
            description=str(payload.get("description", "")),
            details=str(payload.get("details", "")),
            test_strategy=str(payload.get("testStrategy", "")),
            priority=str(payload.get("priority", "")),
            status=str(payload.get("status", "pending")),
            dependencies=[str(dep) for dep in payload.get("dependencies", []) or []],
            subtasks=list(payload.get("subtasks", []) or []),
            raw=payload,
        )


@dataclass(frozen=True)
class TaskmasterStatus:
    workspace: str
    health: TaskmasterHealth
    initialized: bool
    tag: str = ""
    task_count: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    normal_mode_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "health": self.health.to_dict(),
            "initialized": self.initialized,
            "tag": self.tag,
            "task_count": self.task_count,
            "status_counts": self.status_counts,
            "normal_mode_allowed": self.normal_mode_allowed,
        }
