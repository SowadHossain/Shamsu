"""Real stdio MCP server used by SHAMSU integration tests."""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP(
    "SHAMSU MCP integration fixture",
    host="127.0.0.1",
    port=int(os.environ.get("MCP_FIXTURE_PORT", "8000")),
    json_response=True,
)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
def echo(message: str) -> dict[str, str]:
    """Echo a message with one environment value."""
    return {"message": message, "fixture_env": os.environ.get("MCP_FIXTURE_VALUE", "")}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def external_write(value: str) -> str:
    """Represent an external mutation without changing the local workspace."""
    return f"wrote:{value}"


@mcp.tool()
def fail() -> str:
    """Raise a server-side error."""
    raise RuntimeError("fixture failure")


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_FIXTURE_TRANSPORT", "stdio"))
