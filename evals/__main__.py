"""Live baseline runner for the eval harness.

Drives the seed cases through the real agent loop against the active model tier
and prints a BENCHMARK-style report. Requires Ollama running with the tier's
models pulled. Exit code is non-zero if any case fails, so it can gate CI-lite.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from evals.cases import SEED_CASES
from evals.harness import run_evals


def _tier_label() -> str:
    try:
        from shamsu.runtime.models import active_tier

        return str(getattr(active_tier(), "value", active_tier()))
    except Exception:
        return "(unknown)"


async def _amain(args: argparse.Namespace) -> int:
    cases = SEED_CASES
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        cases = [case for case in cases if case.name in wanted]
        if not cases:
            print(f"No cases matched {sorted(wanted)}. Known: {[c.name for c in SEED_CASES]}")
            return 2
    report = await run_evals(cases, tier=_tier_label(), samples=args.samples)
    text = report.render()
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\nWrote {args.out}")
    return 0 if report.passed == report.total else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description="Run SHAMSU task-success evals.")
    parser.add_argument("--list", action="store_true", help="List case names and exit.")
    parser.add_argument("--only", default="", help="Comma-separated case names to run.")
    parser.add_argument("--out", default="", help="Also write the report to this file.")
    parser.add_argument(
        "--samples",
        type=int,
        default=1,
        help=(
            "Run each case N times and score by majority. Local models are "
            "stochastic - use 3+ for a baseline you intend to compare against, "
            "since a single sample cannot tell a regression from a coin flip."
        ),
    )
    args = parser.parse_args(argv)

    if args.list:
        for case in SEED_CASES:
            print(f"{case.name}\t{', '.join(case.tags)}")
        return 0
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
