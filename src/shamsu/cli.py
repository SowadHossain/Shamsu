"""Argument parsing and the process entry point.

Deliberately thin. Everything it does is turn strings into an `AppOptions` and
pick a model client; the work lives in `ui.app` and the runtime. v1's CLI was
18,729 lines because it accumulated agent control, session management, and
display alongside argument handling — this file should stay boring.

**No model client is constructed here.** Local inference is a deployment
concern and this box has no GPU, so `--model` selects a *factory* and an
unconfigured run says so plainly rather than failing halfway through a task
with a connection error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from shamsu.interfaces.models import ModelClient
from shamsu.runtime.limits import ExecutionLimits
from shamsu.ui.app import AppOptions, exit_code, run_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shamsu",
        description="Local-first autonomous coding agent with an evidence-gated runtime.",
    )
    parser.add_argument("request", nargs="?", help="What the agent should do.")
    parser.add_argument(
        "-C",
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Repository to work in (default: the current directory).",
    )
    parser.add_argument(
        "--model",
        default="ollama",
        help="Model backend. 'fake' runs a scripted client for smoke-testing the interface.",
    )
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Line-oriented output instead of the full-screen interface.",
    )
    parser.add_argument("--no-colour", "--no-color", action="store_true", dest="no_colour")
    parser.add_argument(
        "--max-actions",
        type=int,
        default=None,
        help="Override the per-step action budget.",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=None,
        help="State database (default: <workspace>/.shamsu/state.db).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code rather than calling `sys.exit`."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.request:
        parser.print_help()
        return 2

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"shamsu: {workspace} is not a directory", file=sys.stderr)
        return 2

    try:
        model = _model(args.model)
    except NotImplementedError as exc:
        print(f"shamsu: {exc}", file=sys.stderr)
        return 2

    limits = ExecutionLimits(actions_per_step=args.max_actions) if args.max_actions else None
    options = AppOptions(
        workspace=workspace,
        request=args.request,
        tui=not args.no_tui,
        colour=not args.no_colour,
        limits=limits,
        database=args.database,
    )

    try:
        result = asyncio.run(run_task(model, options))
    except KeyboardInterrupt:
        # Reaching here means cancellation did not get a chance to run --
        # during startup, say. The terminal is already restored by the screen
        # context manager's `finally`.
        print("\nshamsu: interrupted", file=sys.stderr)
        return 130

    return exit_code(result)


def _model(name: str) -> ModelClient:
    """Resolve a model backend by name.

    Raises:
        NotImplementedError: the backend is not wired up. Said plainly and
            early, because the alternative is failing three minutes into a task
            with a connection error the user has to interpret.
    """
    if name == "fake":
        from shamsu.models.scripted import ScriptedModel

        return ScriptedModel()

    raise NotImplementedError(
        f"model backend {name!r} is not wired up yet. Local inference lands with the "
        "GPU work; use --model fake to exercise the interface."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
