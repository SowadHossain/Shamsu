"""`/mcp` - what external Model Context Protocol servers are configured.

Lived in `shamsu/mcp/cli.py`, which put terminal rendering inside the protocol
package. It is a command, so it lives with the commands.
"""
from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console
from rich.table import Table

from shamsu.cli.commands import Command, CommandContext
from shamsu.mcp.auth import KeyringTokenStorage
from shamsu.mcp.manager import get_shared_mcp_manager, reload_shared_mcp_manager


def handle(context: CommandContext, argument: str) -> None:
    workspace = context.workspace
    console = context.console
    session_logger = context.session_logger
    parts = argument.strip().split()
    subcommand = parts[0].lower() if parts else "status"
    manager = get_shared_mcp_manager(workspace, session_logger)

    if subcommand in {"status", "list", "servers", "test"}:
        _print_status(manager, console)
        return
    if subcommand == "tools":
        server = parts[1] if len(parts) > 1 else ""
        _print_tools(manager, console, server)
        return
    if subcommand == "config":
        _print_config(manager, workspace, console)
        return
    if subcommand == "reload":
        manager = reload_shared_mcp_manager(workspace, session_logger)
        _print_status(manager, console)
        return
    if subcommand == "auth" and len(parts) > 2 and parts[1].lower() == "logout":
        server = parts[2]
        if server not in manager.config.servers:
            console.print(f"[red]Unknown MCP server: {server}[/red]")
            return
        KeyringTokenStorage(server).clear()
        reload_shared_mcp_manager(workspace, session_logger)
        console.print(f"[green]Cleared stored OAuth credentials for {server}.[/green]")
        return
    console.print(
        "[red]Usage: /mcp status | tools <server> | config | reload | auth logout <server>[/red]"
    )


def _print_status(manager, console: Console) -> None:
    statuses = manager.statuses()
    if not statuses:
        console.print("[dim]No MCP servers configured. Add mcpServers to .mcp.json.[/dim]")
        for error in manager.config.errors:
            console.print(f"[red]{error}[/red]")
        return
    table = Table(title="MCP Servers")
    table.add_column("Server")
    table.add_column("Transport")
    table.add_column("State")
    table.add_column("Tools", justify="right")
    table.add_column("Error")
    for status in statuses:
        state = "disabled" if not status.enabled else "connected" if status.connected else "failed"
        style = "dim" if not status.enabled else "green" if status.connected else "red"
        table.add_row(
            status.name,
            status.transport,
            f"[{style}]{state}[/{style}]",
            str(status.tool_count),
            status.error,
        )
    console.print(table)
    for error in manager.config.errors:
        console.print(f"[red]Config: {error}[/red]")


def _print_tools(manager, console: Console, server: str) -> None:
    tools = [tool for tool in manager.tools() if not server or tool.server == server]
    if not tools:
        console.print(f"[dim]No MCP tools found{f' for {server}' if server else ''}.[/dim]")
        return
    table = Table(title="MCP Tools")
    table.add_column("Model tool name")
    table.add_column("Server tool")
    table.add_column("Read only")
    table.add_column("Description")
    for tool in tools:
        table.add_row(
            tool.model_name,
            f"{tool.server}/{tool.name}",
            "yes" if tool.read_only else "no/unknown",
            tool.description[:100],
        )
    console.print(table)


def _print_config(manager, workspace: Path, console: Console) -> None:
    console.print(f"Workspace config: {Path(workspace).resolve() / '.mcp.json'}")
    console.print(f"SHAMSU workspace config: {Path(workspace).resolve() / '.shamsu' / 'mcp.json'}")
    console.print(f"User config: {Path.home() / '.shamsu' / 'mcp.json'}")
    if manager.config.sources:
        console.print("Loaded (later files override earlier ones):")
        for source in manager.config.sources:
            console.print(f"  {source}")
    else:
        console.print("[dim]No MCP config file exists yet.[/dim]")
    if manager.config.servers:
        safe = {
            name: {
                "transport": config.transport,
                "enabled": config.enabled,
                "auth": config.auth,
                "approval": config.approval,
            }
            for name, config in manager.config.servers.items()
        }
        console.print_json(json.dumps(safe))


COMMANDS = (
    Command(
        name="/mcp",
        summary="External MCP servers: status, tools, config, reload",
        handler=handle,
        completions=("status", "tools ", "config", "reload", "auth logout "),
    ),
)
