"""
Minimal REPL shell.

The selected workspace is the sandbox boundary for project reads and indexes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from shamsu.agents.qa_workflow import QAWorkflow
from shamsu.core.coordinator import Coordinator
from shamsu.indexer.walker import FileWalker
from shamsu.prd.parser import MarkdownPRDParser
from shamsu.retriever.search import SearchAgent
from shamsu.safety.sandbox import Sandbox, SecurityError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shamsu",
        description="Local-first coding agent REPL.",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace directory to treat as the sandbox boundary. Defaults to cwd.",
    )
    return parser.parse_args(argv)


def resolve_workspace(workspace_arg: str | None) -> Path:
    workspace = Path(workspace_arg).expanduser() if workspace_arg else Path.cwd()
    resolved = workspace.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {resolved}")
    return resolved


def _print_help(console: Console) -> None:
    console.print(
        Panel(
            "\n".join(
                [
                    "index                  Index the current workspace",
                    "status                 Show index counts",
                    "search <query>         Search indexed snippets",
                    "symbols <name>         Look up indexed symbols",
                    "parse-prd <file.md>    Parse a Markdown PRD into sections",
                    "help                   Show commands",
                    "exit                   Quit",
                    "",
                    "Any other text builds a QA context preview.",
                ]
            ),
            title="SHAMSU Commands",
        )
    )


def _index_db_path(workspace: Path) -> Path:
    return workspace / ".shamsu" / "index.db"


def _has_index(workspace: Path) -> bool:
    return _index_db_path(workspace).exists()


def _handle_index(workspace: Path, console: Console) -> None:
    entries = FileWalker(workspace).index()
    console.print(f"Indexed {len(entries)} files.")
    for entry in entries[:20]:
        console.print(f"{entry.language:10} {entry.path}")
    if len(entries) > 20:
        console.print(f"... {len(entries) - 20} more")


def _handle_parse_prd(user_input: str, workspace: Path, console: Console) -> None:
    _, _, path_text = user_input.partition(" ")
    cleaned_path = path_text.strip().strip('"').strip("'")
    if not cleaned_path:
        console.print("[red]Usage: parse-prd <file.md>[/red]")
        return
    try:
        file_path = _resolve_workspace_file(cleaned_path, workspace)
    except SecurityError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    if not file_path.exists() or not file_path.is_file():
        console.print(f"[red]File not found: {file_path}[/red]")
        return
    parsed = MarkdownPRDParser().parse(file_path)
    console.print(f"Title: {parsed.title}")
    console.print(json.dumps(parsed.sections, indent=2))


def _resolve_workspace_file(path_text: str, workspace: Path) -> Path:
    return Sandbox(workspace).validate(path_text)


def _handle_status(workspace: Path, console: Console) -> None:
    db_path = _index_db_path(workspace)
    if not db_path.exists():
        console.print("[yellow]No index found. Run `index` first.[/yellow]")
        return

    conn = sqlite3.connect(db_path)
    try:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        snippets = conn.execute("SELECT COUNT(*) FROM snippets").fetchone()[0]
    finally:
        conn.close()
    console.print(f"Files: {files}")
    console.print(f"Symbols: {symbols}")
    console.print(f"Snippets: {snippets}")


def _handle_search(user_input: str, workspace: Path, console: Console) -> None:
    _, _, query = user_input.partition(" ")
    query = query.strip()
    if not query:
        console.print("[red]Usage: search <query>[/red]")
        return
    if not _has_index(workspace):
        console.print("[yellow]No index found. Run `index` first.[/yellow]")
        return
    results = SearchAgent(_index_db_path(workspace)).search(query, top_k=5)
    if not results:
        console.print("[yellow]No results.[/yellow]")
        return
    for result in results:
        console.print(
            f"{result.file_path}:{result.line_start}-{result.line_end} "
            f"score={result.score:.4f}"
        )


def _handle_symbols(user_input: str, workspace: Path, console: Console) -> None:
    _, _, name = user_input.partition(" ")
    name = name.strip()
    if not name:
        console.print("[red]Usage: symbols <name>[/red]")
        return
    if not _has_index(workspace):
        console.print("[yellow]No index found. Run `index` first.[/yellow]")
        return
    results = SearchAgent(_index_db_path(workspace)).symbol_lookup(name)
    if not results:
        console.print("[yellow]No symbols found.[/yellow]")
        return
    for result in results:
        symbol = result.symbol_name or name
        console.print(f"{symbol}: {result.file_path}:{result.line_start}-{result.line_end}")


async def _handle_request(user_input: str, workspace: Path, console: Console) -> None:
    qa_workflow = None
    if _has_index(workspace):
        qa_workflow = QAWorkflow(search=SearchAgent(_index_db_path(workspace)))
    result = await Coordinator(qa_workflow=qa_workflow).handle(user_input)
    console.print(
        json.dumps(
            {
                "intent": result.decision.intent,
                "complexity": result.decision.complexity,
                "confidence": result.decision.confidence,
                "needs_tools": result.decision.needs_tools,
            },
            indent=2,
        )
    )
    if result.preview:
        console.print(Panel(result.preview, title="Context Preview"))


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    console = Console()
    console.print("SHAMSU v0.3.0 - local AI coding agent")
    console.print("Type a request, or 'help' for commands.\n")

    try:
        workspace = resolve_workspace(args.workspace)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    console.print(f"Workspace: {workspace}")

    while True:
        try:
            user_input = input("shamsu> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            sys.exit(0)

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Goodbye.")
            break
        if user_input.lower() == "help":
            _print_help(console)
            continue
        if user_input.lower() == "index":
            _handle_index(workspace, console)
            continue
        if user_input.lower() == "status":
            _handle_status(workspace, console)
            continue
        if user_input.lower().startswith("search "):
            _handle_search(user_input, workspace, console)
            continue
        if user_input.lower().startswith("symbols "):
            _handle_symbols(user_input, workspace, console)
            continue
        if user_input.lower().startswith("parse-prd "):
            _handle_parse_prd(user_input, workspace, console)
            continue

        asyncio.run(_handle_request(user_input, workspace, console))


if __name__ == "__main__":
    main()
