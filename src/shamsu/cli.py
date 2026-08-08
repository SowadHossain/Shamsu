"""Argument parsing and the process entry point.

Deliberately thin. Everything it does is turn strings into an `AppOptions` and
pick a model client; the work lives in `ui.app` and the runtime. v1's CLI was
18,729 lines because it accumulated agent control, session management, and
display alongside argument handling — this file should stay boring.

Selecting a backend never contacts a server. `OllamaClient` construction is
pure, so an unreachable model surfaces on the first request as
`ModelUnavailable` -- naming the host and the model -- rather than as a
connection error thrown during argument parsing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from shamsu.interfaces.models import ModelClient
from shamsu.models.ollama import DEFAULT_HOST, OllamaClient, list_models
from shamsu.runtime.limits import ExecutionLimits
from shamsu.ui.app import AppOptions, exit_code, run_task
from shamsu.ui.repl import Settings, run_repl

#: Used when `--model ollama` names a backend but not a model. A coder-tuned
#: model, because every contract in `models/contracts.py` is a coding decision.
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:14b"


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
        help=(
            "Model backend. 'ollama' uses the default local model, 'fake' runs a "
            "scripted client for smoke-testing the interface, and any other value "
            "is taken as an Ollama model name (e.g. 'qwen2.5-coder:14b')."
        ),
    )
    parser.add_argument(
        "--ollama-host",
        default=DEFAULT_HOST,
        help=f"Ollama server to use (default: {DEFAULT_HOST}).",
    )
    parser.add_argument(
        "--context-tokens",
        type=int,
        default=None,
        help=(
            "Override the context window. Sent to Ollama as num_ctx and used as the "
            "compiler's budget; larger windows cost VRAM."
        ),
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

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"shamsu: {workspace} is not a directory", file=sys.stderr)
        return 2

    # No request means an interactive session rather than a usage dump. The
    # one-shot form is still the scriptable one; this is the one you sit in.
    if not args.request:
        return run_repl(
            Settings(
                model_name=DEFAULT_OLLAMA_MODEL if args.model == "ollama" else args.model,
                host=args.ollama_host,
                workspace=workspace,
                context_tokens=args.context_tokens,
                colour=not args.no_colour,
                limits=(
                    ExecutionLimits(actions_per_step=args.max_actions) if args.max_actions else None
                ),
                database=args.database,
            ),
            lambda settings: _model(
                settings.model_name,
                host=settings.host,
                context_tokens=settings.context_tokens,
            ),
            list_models=lambda host: list_models(host),
        )

    model = _model(args.model, host=args.ollama_host, context_tokens=args.context_tokens)

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


def _model(
    name: str,
    *,
    host: str = DEFAULT_HOST,
    context_tokens: int | None = None,
) -> ModelClient:
    """Resolve a model backend by name.

    `fake` is the scripted client; `ollama` is the default local model; any
    other value names an Ollama model directly, so `--model qwen2.5-coder:14b`
    works without a second flag. Nothing here contacts a server -- an
    unreachable one is reported by the first request, which can name it.
    """
    if name == "fake":
        from shamsu.models.scripted import ScriptedModel

        return ScriptedModel()

    return OllamaClient(
        DEFAULT_OLLAMA_MODEL if name == "ollama" else name,
        host=host,
        context_tokens=context_tokens,
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
