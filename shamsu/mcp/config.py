"""Claude-compatible MCP server configuration loading."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class MCPServerConfig(BaseModel):
    """One stdio or remote MCP server.

    ``command``/``args``/``env`` matches Claude Desktop and Claude Code's
    ``mcpServers`` shape. Remote servers may use ``url`` with an explicit
    ``type`` of ``http`` or ``sse``.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    type: Literal["stdio", "http", "streamable-http", "sse"] | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    auth: Literal["none", "oauth"] = "none"
    oauth_scopes: str | None = None
    oauth_callback_port: int = 0
    enabled: bool = True
    timeout: float = 30.0
    approval: Literal["always", "writes", "never"] = "writes"
    tool_permissions: dict[str, Literal["allow", "ask", "deny"]] = Field(default_factory=dict)
    read_only_tools: list[str] = Field(default_factory=list)
    trust_tool_annotations: bool = False

    @model_validator(mode="before")
    @classmethod
    def accept_common_permission_aliases(cls, value: Any) -> Any:
        """Accept camelCase keys used by Claude-style JSON configuration."""
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        aliases = {
            "toolPermissions": "tool_permissions",
            "readOnlyTools": "read_only_tools",
            "trustToolAnnotations": "trust_tool_annotations",
            "oauthScopes": "oauth_scopes",
            "oauthCallbackPort": "oauth_callback_port",
        }
        for source, target in aliases.items():
            if source in normalized and target not in normalized:
                normalized[target] = normalized[source]
        return normalized

    @model_validator(mode="after")
    def validate_transport(self) -> "MCPServerConfig":
        transport = self.transport
        if transport == "stdio" and not self.command:
            raise ValueError("stdio MCP server requires 'command'")
        if transport != "stdio" and not self.url:
            raise ValueError("remote MCP server requires 'url'")
        if self.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        return self

    @property
    def transport(self) -> Literal["stdio", "http", "sse"]:
        if self.type in {"http", "streamable-http"}:
            return "http"
        if self.type == "sse":
            return "sse"
        return "stdio" if self.command else "http"


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    servers: dict[str, MCPServerConfig] = Field(default_factory=dict, alias="mcpServers")
    sources: list[str] = Field(default_factory=list, exclude=True)
    errors: list[str] = Field(default_factory=list, exclude=True)


def load_mcp_config(workspace: Path) -> MCPConfig:
    """Merge user and workspace MCP configuration.

    Later files win. ``.mcp.json`` is accepted directly so a project can share
    the same server declarations with Claude Code.
    """

    workspace = Path(workspace).resolve()
    paths = (
        Path.home() / ".shamsu" / "mcp.json",
        workspace / ".shamsu" / "mcp.json",
        workspace / ".mcp.json",
    )
    servers: dict[str, MCPServerConfig] = {}
    sources: list[str] = []
    errors: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        sources.append(str(path))
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            entries = raw.get("mcpServers", raw.get("servers", {}))
            if not isinstance(entries, dict):
                raise ValueError("'mcpServers' must be an object")
        except (OSError, json.JSONDecodeError, ValidationError, ValueError) as exc:
            errors.append(f"{path}: {exc}")
            continue
        for name, value in entries.items():
            if value is None:
                servers.pop(str(name), None)
                continue
            try:
                if not isinstance(value, dict):
                    raise ValueError(f"server {name!r} must be an object")
                servers[str(name)] = MCPServerConfig.model_validate(value)
            except (ValidationError, ValueError) as exc:
                errors.append(f"{path}: server {name!r}: {exc}")
    return MCPConfig(mcpServers=servers, sources=sources, errors=errors)


def expand_env_value(value: str) -> str:
    """Resolve ${NAME} references without treating missing secrets as empty."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise ValueError(f"environment variable {name} is not set")
        return os.environ[name]

    return _ENV_REF.sub(replace, value)


def resolved_server_environment(config: MCPServerConfig) -> dict[str, str]:
    env = os.environ.copy()
    env.update({key: expand_env_value(value) for key, value in config.env.items()})
    return env


def resolved_server_command(config: MCPServerConfig) -> str:
    return expand_env_value(str(config.command or ""))


def resolved_server_arguments(config: MCPServerConfig) -> list[str]:
    return [expand_env_value(value) for value in config.args]


def resolved_server_cwd(config: MCPServerConfig, workspace: Path) -> Path:
    raw = expand_env_value(config.cwd) if config.cwd else str(workspace)
    cwd = Path(raw)
    if not cwd.is_absolute():
        cwd = Path(workspace) / cwd
    return cwd.resolve()


def resolved_server_url(config: MCPServerConfig) -> str:
    return expand_env_value(str(config.url or ""))


def resolved_headers(config: MCPServerConfig) -> dict[str, str]:
    return {key: expand_env_value(value) for key, value in config.headers.items()}


def config_template() -> dict[str, Any]:
    return {
        "mcpServers": {
            "example": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-example"],
                "env": {"API_TOKEN": "${API_TOKEN}"},
                "enabled": False,
            }
        }
    }
