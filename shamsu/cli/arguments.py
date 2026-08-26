"""Command-line argument contract for the small SHAMSU harness."""

from __future__ import annotations

import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shamsu",
        description="Local-first small TUI coding harness.",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("run", "web"),
        help=(
            "`run` executes one prompt noninteractively; `web` serves the local "
            "web view. Omit to open the TUI."
        ),
    )
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
        help="For `web`: find workspaces under DIR and remember them.",
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
        help="Return a dry-run result without calling the model.",
    )
    args = parser.parse_args(argv)
    if args.web_flag:
        if args.command not in (None, "web"):
            parser.error(f"--web cannot be combined with `{args.command}`")
        args.command = "web"
    if args.command == "run" and not str(args.prompt or "").strip():
        parser.error("`run` requires --prompt")
    if args.command == "web":
        for name in ("prompt", "output", "approval", "timeout", "dry_run", "session"):
            value = getattr(args, name)
            if name == "output" and value == "text":
                continue
            if name == "approval" and value == "deny":
                continue
            if name == "timeout" and value == 300.0:
                continue
            if name == "dry_run" and value is False:
                continue
            if value not in (None, False):
                parser.error(f"--{name.replace('_', '-')} is only valid with `run`")
        return args
    if args.command is None:
        for name in ("prompt", "output", "approval", "timeout", "dry_run"):
            value = getattr(args, name)
            if name == "output" and value == "text":
                continue
            if name == "approval" and value == "deny":
                continue
            if name == "timeout" and value == 300.0:
                continue
            if name == "dry_run" and value is False:
                continue
            if value not in (None, False):
                parser.error(f"--{name.replace('_', '-')} is only valid with `run`")
        if args.port is not None:
            parser.error("--port is only valid with `web`")
        if args.scan:
            parser.error("--scan is only valid with `web`")
    if args.command == "run":
        if args.port is not None:
            parser.error("--port is only valid with `web`")
        if args.scan:
            parser.error("--scan is only valid with `web`")
    return args
