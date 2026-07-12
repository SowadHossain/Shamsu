"""Deterministic task-success eval harness.

The reliability design's Phase 0: *measure task success so every later change is
provable.* This runs a set of cases through the real request path in a throwaway
workspace and scores each with a deterministic check (file contains X, exit 0,
answer mentions Y) — never the model's self-report. It emits a BENCHMARK-style
pass-rate table so a prompt/loop change shows up as an eval delta.

The runner is dependency-injected: pass a fake ``driver`` to unit-test the
framework without Ollama; the default driver drives ``AgentChatLoop`` against the
active model tier (see ``evals/__main__.py`` for the live baseline runner).
"""
from __future__ import annotations

import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

# A check is deterministic: given the final workspace and the agent's final
# answer text, decide pass/fail. File-oriented cases ignore the text; QA cases
# ignore the workspace.
CheckFn = Callable[[Path, str], bool]
# A seed prepares the starting workspace (write files, etc.).
SeedFn = Callable[[Path], None]
# A driver runs a case's prompt against a workspace and returns the final answer.
Driver = Callable[[Path, "EvalCase"], Awaitable[str]]


@dataclass(frozen=True)
class EvalCase:
    name: str
    prompt: str
    check: CheckFn
    seed: SeedFn | None = None
    long_running: bool = False
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalResult:
    name: str
    passed: bool
    final: str = ""
    error: str = ""
    duration_s: float = 0.0
    tags: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        if self.error:
            return "ERROR"
        return "PASS" if self.passed else "FAIL"


@dataclass(frozen=True)
class EvalReport:
    results: list[EvalResult] = field(default_factory=list)
    tier: str = ""

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total) if self.total else 0.0

    def render(self) -> str:
        return render_report(self)


async def run_evals(
    cases: list[EvalCase],
    *,
    driver: Driver | None = None,
    tier: str = "",
) -> EvalReport:
    """Run each case in an isolated temp workspace and score it. A driver or
    check raising is recorded as a failed case (never aborts the run)."""
    run = driver or chat_loop_driver
    results: list[EvalResult] = []
    for case in cases:
        results.append(await _run_one(case, run))
    return EvalReport(results=results, tier=tier)


async def _run_one(case: EvalCase, driver: Driver) -> EvalResult:
    # ignore_cleanup_errors: on Windows the agent may leave a handle briefly open.
    with tempfile.TemporaryDirectory(prefix="shamsu_eval_", ignore_cleanup_errors=True) as tmp:
        workspace = Path(tmp)
        started = time.perf_counter()
        try:
            if case.seed is not None:
                case.seed(workspace)
            final = await driver(workspace, case)
            passed = bool(case.check(workspace, final or ""))
            error = ""
        except Exception as exc:  # a broken case must not sink the whole run
            final = ""
            passed = False
            error = f"{type(exc).__name__}: {exc}"
        duration = time.perf_counter() - started
    return EvalResult(
        name=case.name,
        passed=passed,
        final=final or "",
        error=error,
        duration_s=duration,
        tags=case.tags,
    )


async def chat_loop_driver(workspace: Path, case: EvalCase) -> str:
    """Default driver: drive the real interactive agent loop headlessly, with
    writes auto-approved (no human in the loop), against the active model tier."""
    from shamsu.agents.chat_loop import AgentChatLoop
    from shamsu.tools.agent_tools import AgentToolRegistry

    tools = AgentToolRegistry(workspace, approval_func=lambda _request: True)
    loop = AgentChatLoop(workspace, tools=tools, long_running=case.long_running)
    result = await loop.run(case.prompt)
    return result.final


def render_report(report: EvalReport) -> str:
    """Render a BENCHMARK.md-style pass-rate table."""
    pct = round(report.pass_rate * 100)
    lines: list[str] = ["# SHAMSU Eval Benchmark", ""]
    tier = report.tier or "(unknown)"
    lines.append(f"- **Tier:** {tier}")
    lines.append(f"- **Pass rate:** {report.passed}/{report.total} ({pct}%)")
    lines.append("")
    lines.append("| Case | Result | Time | Notes |")
    lines.append("|------|--------|------|-------|")
    for result in report.results:
        note = result.error or ("" if result.passed else "check failed")
        lines.append(
            f"| {result.name} | {result.status} | {result.duration_s:.1f}s | {_escape_cell(note)} |"
        )
    lines.append("")
    return "\n".join(lines)


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:160]
