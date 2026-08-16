#!/usr/bin/env python3
"""Run the §31.1 initial task suite against a real local model.

    python scripts/run_task_evals.py                       # the shipped default
    python scripts/run_task_evals.py --model qwen2.5-coder:14b
    python scripts/run_task_evals.py --model fake          # harness self-check
    python scripts/run_task_evals.py --task single_file_bug_fix -v

**A `fake` run scores the harness, not the agent.** The scripted client always
emits valid JSON in a fixed order, so it exercises the runtime and says nothing
about whether a 7B can satisfy these contracts. Only a real model produces a
number worth quoting.

Workspaces are kept on failure so a bad run can be inspected — the path is
printed with the result.
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
for entry in (REPO_ROOT, REPO_ROOT / "src"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from tests.evals.harness import (  # noqa: E402
    DEFAULT_WALL_CLOCK_SECONDS,
    Comparison,
    SuiteResult,
    TaskResult,
    load_baseline,
    run_repeated,
    save_baseline,
)
from tests.evals.tasks import BY_NAME, TASKS  # noqa: E402

from shamsu.cli import DEFAULT_OLLAMA_MODEL  # noqa: E402
from shamsu.interfaces.models import ModelClient  # noqa: E402
from shamsu.runtime.limits import ExecutionLimits  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_task_evals",
        description="Score SHAMSU against the seven §31.1 tasks.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_OLLAMA_MODEL,
        help=(
            "Ollama model name, or 'fake' to self-check the harness. Defaults to "
            "whatever `shamsu` itself defaults to, so the score describes the "
            "shipped configuration rather than a better one."
        ),
    )
    parser.add_argument("--ollama-host", default=None)
    parser.add_argument(
        "--task",
        action="append",
        default=None,
        help="Run only this task (repeatable). Default: all seven.",
    )
    parser.add_argument(
        "--workspaces",
        type=Path,
        default=None,
        help="Where to build task repositories (default: a temporary directory).",
    )
    parser.add_argument(
        "--wall-clock",
        type=float,
        default=DEFAULT_WALL_CLOCK_SECONDS,
        help=f"Seconds per task (default: {DEFAULT_WALL_CLOCK_SECONDS:.0f}).",
    )
    parser.add_argument(
        "--actions",
        type=int,
        default=None,
        help="Override the per-step action budget.",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help=(
            "Run the whole suite this many times at identical paths and report "
            "per-task pass rates. One run is a sample, not a measurement: this "
            "suite has spanned four tasks between repetitions of the same code."
        ),
    )
    parser.add_argument(
        "--save-baseline",
        type=Path,
        default=None,
        help="Write the result to this JSON file, to compare a later run against.",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help=(
            "Compare against a previously saved baseline. A direction is only "
            "claimed when the two sets of per-run scores do not overlap."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        tasks = TASKS if not args.task else tuple(BY_NAME[name] for name in args.task)
    except KeyError as exc:
        print(f"unknown task {exc}; known: {', '.join(BY_NAME)}", file=sys.stderr)
        return 2

    def build_model() -> ModelClient:
        # Constructed per task so one wedged client cannot poison the suite.
        if args.model == "fake":
            from tests.fixtures.fake_model import FakeModelClient

            return FakeModelClient([])
        from shamsu.models.ollama import DEFAULT_HOST, OllamaClient

        return OllamaClient(args.model, host=args.ollama_host or DEFAULT_HOST)

    limits = ExecutionLimits(
        wall_clock_seconds=args.wall_clock,
        **({"actions_per_step": args.actions} if args.actions else {}),
    )

    # A *fixed* directory, wiped rather than freshly minted. `mkdtemp` put a
    # random component in every absolute path, and absolute paths reach the
    # model — through error messages, through git output, through anything that
    # echoes a location back. A benchmark whose inputs differ every run cannot
    # measure a change smaller than its own noise, and this one was swinging by
    # two tasks out of seven.
    root = args.workspaces or Path(tempfile.gettempdir()) / "shamsu-evals"
    if args.workspaces is None and root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    repeat = max(1, args.repeat)
    print(f"running {len(tasks)} task(s) against {args.model}, {repeat} repetition(s)")
    print(f"workspaces: {root}\n", flush=True)

    def report(repetition: int, result: TaskResult) -> None:
        prefix = f"[{repetition}/{repeat}]" if repeat > 1 else "     "
        print(f"{prefix}{result.line()}", flush=True)
        if args.verbose:
            print(f"      state={result.state} stopped={result.stopped_because}")
            print(f"      changed={', '.join(result.files_changed) or 'nothing'}")
            if result.error:
                print(f"      error={result.error}")
            print(f"      workspace={root / result.task}", flush=True)

    def report_suite(repetition: int, suite: SuiteResult) -> None:
        if repeat > 1:
            print(
                f"  -- repetition {repetition}: {suite.correct}/{suite.total} correct\n",
                flush=True,
            )

    result = run_repeated(
        tasks,
        build_model,
        root,
        repeat=repeat,
        model_name=args.model,
        limits=limits,
        on_result=report,
        on_suite=report_suite,
    )

    print()
    print(result.render())

    if args.save_baseline:
        save_baseline(result, args.save_baseline)
        print(f"\nbaseline written to {args.save_baseline}")

    if args.baseline:
        print()
        print(Comparison(baseline=load_baseline(args.baseline), current=result).render())

    print(f"\nworkspaces kept at {root}")

    # Non-zero on any false success, whatever the pass rate: a run that reported
    # verified completion for work it did not do is not a run to shrug at.
    return 1 if result.false_successes else 0


if __name__ == "__main__":
    raise SystemExit(main())
