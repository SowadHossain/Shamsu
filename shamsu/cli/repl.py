"""
Minimal REPL shell for Day 1.

It exposes the first working slices: indexing, Markdown PRD parsing, and
coordinator-to-QA context preview.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

from shamsu.core.coordinator import Coordinator
from shamsu.indexer.walker import FileWalker
from shamsu.prd.parser import MarkdownPRDParser


def _print_help(console: Console) -> None:
    console.print(
        Panel(
            "\n".join(
                [
                    "index                  Index the current workspace",
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


def _handle_index(workspace: Path, console: Console) -> None:
    entries = FileWalker(workspace).index()
    console.print(f"Indexed {len(entries)} files.")
    for entry in entries[:20]:
        console.print(f"{entry.language:10} {entry.path}")
    if len(entries) > 20:
        console.print(f"... {len(entries) - 20} more")


def _handle_parse_prd(user_input: str, workspace: Path, console: Console) -> None:
    _, _, path_text = user_input.partition(" ")
    if not path_text.strip():
        console.print("[red]Usage: parse-prd <file.md>[/red]")
        return
    file_path = (workspace / path_text.strip()).resolve()
    parsed = MarkdownPRDParser().parse(file_path)
    console.print(f"Title: {parsed.title}")
    console.print(json.dumps(parsed.sections, indent=2))


async def _handle_request(user_input: str, console: Console) -> None:
    result = await Coordinator().handle(user_input)
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


def main() -> None:
    console = Console()
    console.print("SHAMSU v0.3.0 - local AI coding agent")
    console.print("Type a request, or 'help' for commands.\n")

    workspace = Path.cwd()
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
        if user_input.lower().startswith("parse-prd "):
            _handle_parse_prd(user_input, workspace, console)
            continue

        asyncio.run(_handle_request(user_input, console))


if __name__ == "__main__":
    main()
