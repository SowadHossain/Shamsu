"""Small SHAMSU-facing MCP contracts."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_-]+")


def safe_name(value: str) -> str:
    return _UNSAFE_NAME.sub("_", value).strip("_") or "unnamed"


@dataclass(frozen=True)
class MCPTool:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] = field(default_factory=dict)

    @property
    def model_name(self) -> str:
        return f"mcp__{safe_name(self.server)}__{safe_name(self.name)}"

    @property
    def read_only(self) -> bool:
        return self.annotations.get("readOnlyHint") is True

    @property
    def destructive(self) -> bool:
        return self.annotations.get("destructiveHint") is True

    def to_ollama_schema(self) -> dict[str, Any]:
        description = self.description or f"Call {self.name} on the {self.server} MCP server."
        return {
            "type": "function",
            "function": {
                "name": self.model_name,
                "description": f"[External MCP: {self.server}] {description}",
                "parameters": self.input_schema or {"type": "object", "properties": {}},
            },
        }


@dataclass(frozen=True)
class MCPServerStatus:
    name: str
    transport: str
    enabled: bool
    connected: bool
    tool_count: int = 0
    error: str = ""
