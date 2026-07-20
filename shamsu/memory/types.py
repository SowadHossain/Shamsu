"""Shared types for SHAMSU's required Graphiti long-term memory backend."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

MemoryKind = Literal[
    "user_preference",
    "project_decision",
    "workflow_rule",
    "bug_lesson",
    "architecture_note",
    "task_summary",
    "safety_rule",
]


@dataclass(frozen=True)
class GraphitiHealth:
    available: bool = False
    tool_path: str = ""
    config_path: str = ""
    version: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.available


@dataclass(frozen=True)
class MemoryStatus:
    workspace: str
    health: GraphitiHealth
    memory_path: str
    normal_mode_allowed: bool = True
    local_available: bool = True
    degraded: bool = False
    storage_mode: str = "local"

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "memory_path": self.memory_path,
            "normal_mode_allowed": self.normal_mode_allowed,
            "local_available": self.local_available,
            "degraded": self.degraded,
            "storage_mode": self.storage_mode,
            "health": {
                "available": self.health.available,
                "tool_path": self.health.tool_path,
                "config_path": self.health.config_path,
                "version": self.health.version,
                "message": self.health.message,
            },
        }


@dataclass(frozen=True)
class MemoryGate:
    allowed: bool
    reason: str = ""
    status: MemoryStatus | None = None


@dataclass(frozen=True)
class LongTermMemory:
    kind: MemoryKind
    text: str
    memory_id: str = ""
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)
