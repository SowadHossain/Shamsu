"""Persistent MCP client runtime and SHAMSU tool bridge."""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import threading
from concurrent.futures import Future
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any, Coroutine, TypeVar
from urllib.parse import urlsplit, urlunsplit

import httpx
import mcp.types as mcp_types
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from shamsu.mcp.auth import OAuthCallbackServer, build_oauth_provider
from shamsu.mcp.config import (
    MCPConfig,
    MCPServerConfig,
    load_mcp_config,
    resolved_headers,
    resolved_server_arguments,
    resolved_server_command,
    resolved_server_cwd,
    resolved_server_environment,
    resolved_server_url,
)
from shamsu.mcp.types import MCPServerStatus, MCPTool
from shamsu.session.manager import SessionLogger

T = TypeVar("T")
_SHARED_MANAGERS: dict[Path, "MCPManager"] = {}


class _AsyncRuntime:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run, name="shamsu-mcp", daemon=True)
        self.thread.start()
        self._closed = False

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coroutine: Coroutine[Any, Any, T], timeout: float) -> T:
        if self._closed:
            raise RuntimeError("MCP runtime is closed")
        future: Future[T] = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=3.0)
        self.loop.close()


class _Connection:
    def __init__(self, name: str, config: MCPServerConfig, workspace: Path) -> None:
        self.name = name
        self.config = config
        self.workspace = workspace
        self.stack: AsyncExitStack | None = None
        self.session: ClientSession | None = None
        self.tools: list[MCPTool] = []

    async def connect(self) -> list[MCPTool]:
        stack = AsyncExitStack()
        try:
            read, write = await self._open_transport(stack)
            self.session = await stack.enter_async_context(
                ClientSession(
                    read,
                    write,
                    read_timeout_seconds=timedelta(seconds=self.config.timeout),
                    list_roots_callback=self._list_roots,
                )
            )
            await self.session.initialize()
            self.tools = await self._list_tools()
            self.stack = stack
            return self.tools
        except BaseException:
            await stack.aclose()
            raise

    async def _open_transport(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        if self.config.transport == "stdio":
            params = StdioServerParameters(
                command=resolved_server_command(self.config),
                args=resolved_server_arguments(self.config),
                env=resolved_server_environment(self.config),
                cwd=resolved_server_cwd(self.config, self.workspace),
            )
            log_dir = self.workspace / ".shamsu" / "mcp" / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / f"{self.name}.stderr.log"
            errlog = stack.enter_context(
                open(log_path, "a", encoding="utf-8", errors="replace")
            )
            return await stack.enter_async_context(stdio_client(params, errlog=errlog))

        headers = resolved_headers(self.config)
        url = resolved_server_url(self.config)
        if self.config.transport == "sse":
            return await stack.enter_async_context(
                sse_client(
                    url,
                    headers=headers,
                    timeout=self.config.timeout,
                    sse_read_timeout=self.config.timeout,
                )
            )

        callback: OAuthCallbackServer | None = None
        auth: httpx.Auth | None = None
        if self.config.auth == "oauth":
            callback = OAuthCallbackServer(self.config.oauth_callback_port)
            stack.callback(callback.close)
            auth = build_oauth_provider(
                self.name,
                _oauth_server_url(url),
                callback,
                self.config.oauth_scopes,
            )
        http_client = await stack.enter_async_context(
            httpx.AsyncClient(
                headers=headers,
                auth=auth,
                follow_redirects=True,
                timeout=self.config.timeout,
            )
        )
        read, write, _session_id = await stack.enter_async_context(
            streamable_http_client(url, http_client=http_client)
        )
        return read, write

    async def _list_roots(self, _context: Any) -> mcp_types.ListRootsResult:
        return mcp_types.ListRootsResult(
            roots=[
                mcp_types.Root(
                    uri=self.workspace.as_uri(),
                    name=self.workspace.name or str(self.workspace),
                )
            ]
        )

    async def _list_tools(self) -> list[MCPTool]:
        assert self.session is not None
        result: list[MCPTool] = []
        cursor: str | None = None
        while True:
            page = await self.session.list_tools(cursor=cursor)
            for tool in page.tools:
                annotations = (
                    tool.annotations.model_dump(exclude_none=True)
                    if tool.annotations is not None
                    else {}
                )
                result.append(
                    MCPTool(
                        server=self.name,
                        name=tool.name,
                        description=tool.description or "",
                        input_schema=dict(tool.inputSchema or {}),
                        annotations=annotations,
                    )
                )
            cursor = page.nextCursor
            if not cursor:
                break
        return result

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.session is None:
            raise RuntimeError(f"MCP server {self.name} is not connected")
        result = await self.session.call_tool(
            name,
            arguments,
            read_timeout_seconds=timedelta(seconds=self.config.timeout),
        )
        return result.model_dump(mode="json", exclude_none=True)

    async def close(self) -> None:
        if self.stack is not None:
            await self.stack.aclose()
            self.stack = None
            self.session = None


class MCPManager:
    """Discover and call configured MCP servers from synchronous SHAMSU tools."""

    def __init__(
        self,
        workspace: Path,
        *,
        config: MCPConfig | None = None,
        session_logger: SessionLogger | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.config = config or load_mcp_config(self.workspace)
        self.session_logger = session_logger
        self.runtime: _AsyncRuntime | None = None
        self.connections: dict[str, _Connection] = {}
        self._tools: dict[str, MCPTool] = {}
        self._errors: dict[str, str] = {}
        self._started = False
        self._closed = False
        if self.config.servers:
            atexit.register(self.close)

    @property
    def configured(self) -> bool:
        return bool(self.config.servers)

    def start(self) -> None:
        if self._started or self._closed:
            return
        self._started = True
        enabled = {name: cfg for name, cfg in self.config.servers.items() if cfg.enabled}
        if not enabled:
            return
        self.runtime = _AsyncRuntime()
        for name, config in enabled.items():
            connection = _Connection(name, config, self.workspace)
            try:
                tools = self.runtime.submit(connection.connect(), config.timeout + 5.0)
            except Exception as exc:
                self._errors[name] = _clean_error(exc)
                self._log("mcp.server.failed", name, error=self._errors[name])
                continue
            self.connections[name] = connection
            for tool in tools:
                if tool.model_name in self._tools:
                    self._errors[name] = f"tool name collision: {tool.model_name}"
                    continue
                self._tools[tool.model_name] = tool
            self._log("mcp.server.connected", name, tool_count=len(tools))

    def tools(self) -> list[MCPTool]:
        self.start()
        return list(self._tools.values())

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [tool.to_ollama_schema() for tool in self.tools()]

    def get_tool(self, model_name: str) -> MCPTool | None:
        self.start()
        return self._tools.get(model_name)

    def call(self, model_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tool = self.get_tool(model_name)
        if tool is None:
            raise KeyError(f"Unknown MCP tool: {model_name}")
        connection = self.connections.get(tool.server)
        if connection is None or self.runtime is None:
            raise RuntimeError(f"MCP server {tool.server} is not connected")
        return self.runtime.submit(
            connection.call(tool.name, arguments), connection.config.timeout + 2.0
        )

    def statuses(self) -> list[MCPServerStatus]:
        self.start()
        result: list[MCPServerStatus] = []
        for name, config in self.config.servers.items():
            connection = self.connections.get(name)
            result.append(
                MCPServerStatus(
                    name=name,
                    transport=config.transport,
                    enabled=config.enabled,
                    connected=connection is not None,
                    tool_count=len(connection.tools) if connection else 0,
                    error=self._errors.get(name, ""),
                )
            )
        return result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        runtime = self.runtime
        if runtime is None:
            return
        for connection in list(self.connections.values())[::-1]:
            with contextlib.suppress(Exception):
                runtime.submit(connection.close(), connection.config.timeout + 2.0)
        runtime.close()
        self.connections.clear()

    def _log(self, event: str, server: str, **payload: Any) -> None:
        if self.session_logger:
            self.session_logger.log(
                event,
                {"server": server, **payload},
                f"MCP server {server}: {event.rsplit('.', 1)[-1]}",
                workflow_id="mcp",
            )


def _oauth_server_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _clean_error(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        details = "; ".join(_clean_error(item) for item in exc.exceptions)
        return details[:2000]
    text = str(exc).strip()
    if text:
        return text[:2000]
    return exc.__class__.__name__


def summarize_mcp_result(result: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Build useful model feedback while preserving structured MCP content."""

    texts: list[str] = []
    for item in result.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            texts.append(str(item.get("text", "")))
    structured = result.get("structuredContent")
    if texts:
        message = "\n".join(texts)
    elif structured is not None:
        message = json.dumps(structured, ensure_ascii=True, default=str)
    else:
        message = "MCP tool returned non-text content."
    data = {
        "content": result.get("content", []),
        "structured_content": structured,
        "meta": result.get("_meta", result.get("meta", {})),
        "is_error": bool(result.get("isError", False)),
    }
    return message[:12000], data


def get_shared_mcp_manager(
    workspace: Path, session_logger: SessionLogger | None = None
) -> MCPManager:
    resolved = Path(workspace).resolve()
    manager = _SHARED_MANAGERS.get(resolved)
    if manager is None or manager._closed:
        manager = MCPManager(resolved, session_logger=session_logger)
        _SHARED_MANAGERS[resolved] = manager
    elif manager.session_logger is None and session_logger is not None:
        manager.session_logger = session_logger
    return manager


def reload_shared_mcp_manager(
    workspace: Path, session_logger: SessionLogger | None = None
) -> MCPManager:
    resolved = Path(workspace).resolve()
    previous = _SHARED_MANAGERS.pop(resolved, None)
    if previous is not None:
        previous.close()
    return get_shared_mcp_manager(resolved, session_logger)
