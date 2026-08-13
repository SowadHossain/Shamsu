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
from shamsu.ui.app import AppOptions, exit_code, run_task, small_talk
from shamsu.ui.approval import approver_for
from shamsu.ui.repl import Settings, run_repl
from shamsu.ui.theme import use_utf8_output

#: Used when `--model ollama` names a backend but not a model. Coder-tuned,
#: because every contract in `models/contracts.py` is a coding decision.
#:
#: **Chosen to fit 8 GB of VRAM, which is the constraint that matters.** The
#: previous default was `qwen2.5-coder:14b`, whose weights alone exceed that —
#: so the out-of-the-box experience on an ordinary GPU was either a `pull` of
#: something that would not fit, or a silent spill into system RAM that makes
#: every run slow enough to look broken.
#:
#: The budget, approximately, at `DEFAULT_MAX_CONTEXT_TOKENS` (16384):
#:
#:   weights (7.6B at Q4_K_M)            ~4.7 GB
#:   KV cache (28 layers, 4 KV heads,
#:     head_dim 128, fp16 → ~56 KiB/tok)  ~0.9 GB
#:   compute buffers                      ~0.5 GB
#:                                        -------
#:                                        ~6.1 GB
#:
#: which leaves headroom on an 8 GB card. The same model at its full 32k window
#: would add another ~0.9 GB of KV and start spilling, which is why the context
#: ceiling and this choice belong together.
#:
#: Any other model still works: `--model <name>` takes an Ollama model directly,
#: and a name that is not pulled reports what *is* available.
DEFAULT_OLLAMA_MODEL = "qwen2.5-coder:7b-instruct-q4_K_M"


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
            f"Model backend. 'ollama' uses the default ({DEFAULT_OLLAMA_MODEL}, "
            "quantized to fit 8 GB of VRAM), 'fake' runs a scripted client for "
            "smoke-testing the interface, and any other value is taken as an "
            "Ollama model name (e.g. 'qwen2.5-coder:14b' if you have the VRAM)."
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
    parser.add_argument(
        "--plan",
        action="store_true",
        help=(
            "Read-only run: the gateway is built without any mutating tool and "
            "every plan step is forced to 'investigate'. Nothing can be edited."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Approve high-risk steps without asking. Only for unattended runs "
            "you have already decided to trust; the default asks at the terminal."
        ),
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help=(
            "Never start Ollama and never download a model. Fails with what is "
            "missing instead. For machines where those are someone else's job."
        ),
    )
    return parser


def _prepare_model(name: str, host: str, *, offline: bool) -> int:
    """Start Ollama and fetch the model if needed. Returns an exit code, 0 to go on.

    Done here rather than inside `OllamaClient` on purpose. Construction is
    documented not to contact a server, and a client that quietly downloaded
    four gigabytes the first time it was used would be a surprising thing to
    build. This is the entry point: the one place where doing chores on the
    user's behalf is expected, and where there is somewhere to print them.
    """
    if name == "fake":
        return 0

    from shamsu.models.provisioning import NotProvisionable, Progress, ensure_ready

    model = DEFAULT_OLLAMA_MODEL if name == "ollama" else name
    last = ""

    def show(progress: Progress) -> None:
        # Percentages rewrite one line; anything else gets its own. A pull
        # prints a few hundred updates and a scrolling wall of them is worse
        # than no progress at all.
        nonlocal last
        line = progress.render()
        if line == last:
            return
        last = line
        end = "\r" if progress.fraction is not None and progress.fraction < 1.0 else "\n"
        print(f"  {line}", end=end, flush=True)

    try:
        ensure_ready(model, host, start=not offline, pull=not offline, on_progress=show)
    except NotProvisionable as exc:
        if last.endswith("%"):
            print()
        print(f"shamsu: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nshamsu: interrupted while preparing the model", file=sys.stderr)
        return 130
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Returns a process exit code rather than calling `sys.exit`."""
    # Before anything can print. The display draws box glyphs and check marks,
    # and a default Windows console (cp1252) cannot encode them -- the first
    # activity line raised UnicodeEncodeError and lost a run whose work had
    # already finished.
    use_utf8_output()

    parser = build_parser()
    args = parser.parse_args(argv)

    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        print(f"shamsu: {workspace} is not a directory", file=sys.stderr)
        return 2

    # Before either path, so "Ollama is not running" is answered once, here,
    # rather than surfacing as a failed run several seconds later.
    prepared = _prepare_model(args.model, args.ollama_host, offline=args.offline)
    if prepared != 0:
        return prepared

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
                # `--plan` sets the starting mode; `/mode build` still switches
                # out of it, which is what a session is for.
                mode="plan" if args.plan else "build",
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
        read_only=args.plan,
        approver=approver_for(assume_yes=args.yes),
    )

    # A greeting is not a task. Answering it here keeps it out of the state
    # machine entirely, which is the only place the distinction can be made
    # cheaply -- once a run exists it has a plan, and a plan for "hi" is a plan
    # that cannot be completed.
    reply = asyncio.run(small_talk(model, args.request, workspace=workspace, limits=limits))
    if reply is not None:
        if reply:
            print(reply)
        return 0

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

    `fake` is the scripted client; `ollama` is `DEFAULT_OLLAMA_MODEL`; any other
    value names an Ollama model directly, so `--model qwen2.5-coder:14b` works
    without a second flag. Nothing here contacts a server -- an unreachable one
    is reported by the first request, which can name it, and a model that is
    not pulled is reported with the list of ones that are.
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
