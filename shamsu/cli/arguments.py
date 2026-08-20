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
        choices=("run", "web"),
        help=(
            "`run` executes one prompt noninteractively; `web` serves the local "
            "web view. Omit to open the REPL."
        ),
    )
    # `--web`, and `-web` because that is what people type. argparse treats a
    # single-dash multi-character string as one option, so this is unambiguous
    # rather than a clash with `-w -e -b`.
    parser.add_argument(
        "--web",
        "-web",
        dest="web_flag",
        action="store_true",
        help="Same as the `web` command: serve the local web view.",
    )
    parser.add_argument(
        "--scan",
        action="append",
        default=None,
        metavar="DIR",
        help=(
            "For `web`: find workspaces under DIR and remember them. "
            "Repeatable. Bounded depth; opt-in, so nothing is scanned unasked."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for `web`. Defaults to 8765; 0 picks a free one.",
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
    if args.web_flag:
        if args.command not in (None, "web"):
            parser.error(f"--web cannot be combined with `{args.command}`")
        args.command = "web"
    if args.command == "run" and not str(args.prompt or "").strip():
        parser.error("`run` requires --prompt")
    if args.command != "run" and args.prompt is not None:
        parser.error("--prompt is only valid with `run`")
    if args.port is not None and args.command != "web":
        parser.error("--port is only valid with `web`")
    if args.scan and args.command != "web":
        parser.error("--scan is only valid with `web`")
    return args
