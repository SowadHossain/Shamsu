"""SHAMSU task-success eval harness (reliability design, Phase 0).

Run headlessly against the active model tier:

    python -m evals                 # run all seed cases, print the benchmark
    python -m evals --list          # list case names
    python -m evals --prd-only      # run only medium/long PRD benchmarks
    python -m evals --artifacts-dir eval-artifacts
    python -m evals --out BENCHMARK.md   # also write the report to a file
"""
from evals.harness import (
    CheckOutcome,
    EvalCase,
    EvalReport,
    EvalResult,
    render_report,
    run_evals,
)

__all__ = [
    "CheckOutcome",
    "EvalCase",
    "EvalReport",
    "EvalResult",
    "render_report",
    "run_evals",
]
