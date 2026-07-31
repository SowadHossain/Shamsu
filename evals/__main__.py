"""Live baseline runner for the eval harness.

Drives the seed cases through the real agent loop against the active model tier
and prints a BENCHMARK-style report. Requires Ollama running with the tier's
models pulled. Exit code is non-zero if any case fails, so it can gate CI-lite.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from evals.cases import SEED_CASES
from evals.harness import EvalCase, EvalReport, run_evals
from evals.prd_cases import PRD_BENCHMARK_CASES


def _resolve_tier() -> str:
    """Resolve and ACTIVATE the model tier before any case runs.

    ``python -m evals`` must honor SHAMSU_MODEL_TIER exactly like the REPL
    does. Reading ``active_tier()`` alone is a trap: it returns a module
    global that nothing in this process has initialized, so the run silently
    uses (and reports) the default tier no matter what the env says.
    """
    try:
        from shamsu.runtime.models import initialize_model_tier

        return str(initialize_model_tier(Path.cwd()).value)
    except Exception:
        return "(unknown)"


async def _amain(args: argparse.Namespace) -> int:
    cases = _select_cases(include_prd=args.include_prd, prd_only=args.prd_only)
    if args.only:
        wanted = {name.strip() for name in args.only.split(",") if name.strip()}
        cases = [case for case in cases if case.name in wanted]
        if not cases:
            known = [c.name for c in _select_cases(include_prd=True, prd_only=False)]
            print(f"No cases matched {sorted(wanted)}. Known: {known}")
            return 2
    report = await run_evals(
        cases,
        tier=_resolve_tier(),
        samples=args.samples,
        artifacts_dir=Path(args.artifacts_dir) if args.artifacts_dir else None,
        progress=None if args.no_progress else _make_progress_sink(),
        progress_interval_s=args.progress_interval,
    )
    text = report.render()
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"\nWrote {args.out}")
    if args.json_out:
        Path(args.json_out).write_text(
            json.dumps(report_to_dict(report), indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        print(f"Wrote {args.json_out}")
    return 0 if report.passed == report.total else 1


def _select_cases(*, include_prd: bool, prd_only: bool) -> list[EvalCase]:
    if prd_only:
        return list(PRD_BENCHMARK_CASES)
    cases = list(SEED_CASES)
    if include_prd:
        cases.extend(PRD_BENCHMARK_CASES)
    return cases


def report_to_dict(report: EvalReport) -> dict[str, object]:
    return {
        "tier": report.tier,
        "artifacts_dir": report.artifacts_dir,
        "total": report.total,
        "passed": report.passed,
        "pass_rate": report.pass_rate,
        "results": [
            {
                "name": result.name,
                "passed": result.passed,
                "status": result.status,
                "passes": result.passes,
                "runs": result.runs,
                "flaky": result.flaky,
                "duration_s": result.duration_s,
                "tags": list(result.tags),
                "note": result.note,
                "error": result.error,
                "final": result.final,
                "workspace": result.workspace,
                "attempt_workspaces": list(result.attempt_workspaces),
            }
            for result in report.results
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description="Run SHAMSU task-success evals.")
    parser.add_argument("--list", action="store_true", help="List case names and exit.")
    parser.add_argument("--only", default="", help="Comma-separated case names to run.")
    parser.add_argument("--out", default="", help="Also write the report to this file.")
    parser.add_argument("--json-out", default="", help="Also write a machine-readable JSON report.")
    parser.add_argument(
        "--artifacts-dir",
        default="",
        help="Preserve per-sample workspaces under this directory for audit/debugging.",
    )
    parser.add_argument("--include-prd", action="store_true", help="Include medium/long PRD benchmarks.")
    parser.add_argument("--prd-only", action="store_true", help="Run only medium/long PRD benchmarks.")
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
    parser.add_argument(
        "--no-progress",
        action="store_true",
        help="Suppress live sample/heartbeat progress on stderr.",
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=30.0,
        help="Seconds between heartbeat lines for long-running cases.",
    )
    args = parser.parse_args(argv)

    if args.list:
        for case in _select_cases(include_prd=args.include_prd, prd_only=args.prd_only):
            print(f"{case.name}\t{', '.join(case.tags)}")
        return 0
    return asyncio.run(_amain(args))


def _make_progress_sink():
    progress_path: dict[str, Path | None] = {"path": None}

    def _sink(event: dict[str, object]) -> None:
        path = progress_path["path"]
        if event.get("event") == "run_start" and event.get("artifacts_dir"):
            path = Path(str(event["artifacts_dir"])) / "progress.jsonl"
            progress_path["path"] = path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="utf-8")
        if path is not None:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=True) + "\n")
        _print_progress_event(event)

    return _sink


def _print_progress_event(event: dict[str, object]) -> None:
    message = _format_progress_event(event)
    if message:
        print(message, file=sys.stderr, flush=True)


def _format_progress_event(event: dict[str, object]) -> str:
    kind = str(event.get("event") or "")
    if kind == "run_start":
        return (
            "[eval] running "
            f"{event.get('cases')} case(s) x {event.get('samples')} sample(s) "
            f"= {event.get('total_attempts')} attempt(s)"
            + _suffix("artifacts", event.get("artifacts_dir"))
        )
    if kind == "case_start":
        return (
            f"[eval] case {event.get('case_index')}/{event.get('total_cases')} "
            f"{event.get('case')} starting ({event.get('samples')} sample(s))"
        )
    if kind == "sample_start":
        return (
            f"[eval] start attempt {event.get('attempt')}/{event.get('total_attempts')}: "
            f"{event.get('case')} sample {event.get('sample')}/{event.get('samples')}"
            + _suffix("workspace", event.get("workspace"))
        )
    if kind == "sample_heartbeat":
        elapsed = _elapsed(event.get("elapsed_s"))
        return (
            f"[eval] still running after {elapsed}: "
            f"{event.get('case')} sample {event.get('sample')}/{event.get('samples')} "
            f"(attempt {event.get('attempt')}/{event.get('total_attempts')})"
        )
    if kind == "sample_finish":
        status = "PASS" if bool(event.get("passed")) else "FAIL"
        if event.get("error"):
            status = "ERROR"
        detail = event.get("error") or event.get("note") or ""
        return (
            f"[eval] finish attempt {event.get('attempt')}/{event.get('total_attempts')}: "
            f"{event.get('case')} sample {event.get('sample')}/{event.get('samples')} "
            f"{status} in {_elapsed(event.get('duration_s'))}"
            + _suffix("note", detail)
        )
    if kind == "case_finish":
        status = "PASS" if bool(event.get("passed")) else "FAIL"
        flaky = " flaky" if bool(event.get("flaky")) else ""
        return (
            f"[eval] case {event.get('case_index')}/{event.get('total_cases')} "
            f"{event.get('case')} {status} {event.get('passes')}/{event.get('runs')}"
            f"{flaky} in {_elapsed(event.get('duration_s'))}"
        )
    if kind == "run_finish":
        return (
            f"[eval] complete: {event.get('passed')}/{event.get('total')} passed "
            f"({round(float(event.get('pass_rate') or 0) * 100)}%)"
        )
    return ""


def _elapsed(value: object) -> str:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        seconds = 0.0
    if seconds >= 60:
        minutes = int(seconds // 60)
        remainder = int(seconds % 60)
        return f"{minutes}m{remainder:02d}s"
    return f"{seconds:.1f}s"


def _suffix(label: str, value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > 120:
        text = text[:117] + "..."
    return f" ({label}: {text})"


if __name__ == "__main__":
    sys.exit(main())
