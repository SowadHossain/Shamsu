"""Command-line argument contract for interactive and headless SHAMSU."""

from __future__ import annotations

import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shamsu",
        description="Local-first coding agent REPL.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run",),
        help="Run one prompt noninteractively instead of opening the REPL.",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace directory to treat as the sandbox boundary. Defaults to cwd.",
    )
    parser.add_argument("--session", default=None, help="Resume a session by id or title prefix.")
    parser.add_argument(
        "--new-session",
        nargs="?",
        const="Untitled Session",
        default=None,
        help="Create a new session with an optional title.",
    )
    parser.add_argument("--prompt", default=None, help="Prompt for noninteractive `run` mode.")
    parser.add_argument(
        "--output",
        choices=("text", "json"),
        default="text",
        help="Output format for noninteractive `run` mode.",
    )
    parser.add_argument(
        "--approval",
        choices=("allow", "deny"),
        default="deny",
        help="Deterministic approval policy for noninteractive `run` mode.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Maximum request time in seconds for noninteractive `run` mode.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview approval-gated actions without allowing mutations.",
    )
    args = parser.parse_args(argv)
    if args.command == "run" and not str(args.prompt or "").strip():
        parser.error("`run` requires --prompt")
    if args.command != "run" and args.prompt is not None:
        parser.error("--prompt is only valid with `run`")
    return args
