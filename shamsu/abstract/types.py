"""Data types shared by the Codebase-Memory MCP adapter and service layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CodebaseMemoryHealth:
    """Result of checking whether the real Codebase-Memory MCP tool is usable."""

    available: bool = False
    binary_path: str = ""
    version: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.available


@dataclass(frozen=True)
class IndexStatus:
    """Freshness of the workspace's code-memory index, per SHAMSU's own bookkeeping."""

    exists: bool = False
    stale: bool = True
    message: str = ""


@dataclass(frozen=True)
class AbstractStatus:
    workspace: str
    health: CodebaseMemoryHealth
    index: IndexStatus
    normal_mode_allowed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace": self.workspace,
            "health": {
                "available": self.health.available,
                "binary_path": self.health.binary_path,
                "version": self.health.version,
                "message": self.health.message,
            },
            "index": {
                "exists": self.index.exists,
                "stale": self.index.stale,
                "message": self.index.message,
            },
            "normal_mode_allowed": self.normal_mode_allowed,
        }


@dataclass(frozen=True)
class GateResult:
    """Whether normal SHAMSU code-agent workflows may run in this workspace."""

    allowed: bool
    reason: str = ""
    status: AbstractStatus | None = None


@dataclass(frozen=True)
class AdapterResult:
    """Generic result envelope for adapter calls into the real CLI/tool.

    Never fabricated: `ok=False` with `error` set is the honest outcome when the
    tool is missing, a call fails, or output can't be parsed as JSON.
    """

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {"ok": self.ok, **self.data}
        if self.error:
            payload["error"] = self.error
        return payload
