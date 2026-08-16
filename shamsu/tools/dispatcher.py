"""Tool dispatch boundary for agent execution."""
from __future__ import annotations

from typing import Any, Protocol


class ToolRegistry(Protocol):
    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        ...


class ToolDispatcher:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def dispatch(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.registry.execute(name, arguments)
