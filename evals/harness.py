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
    # Per-case driver override. Most cases go through the agent loop, but some
    # measure a different real path (e.g. the planner, which never touches the
    # loop) - those cannot be scored by driving the loop instead. A fake driver
    # passed to `run_evals` still wins, so unit tests stay Ollama-free.
    driver: Driver | None = None


@dataclass(frozen=True)
class EvalResult:
    name: str
    passed: bool
    final: str = ""
    error: str = ""
    duration_s: float = 0.0
    tags: tuple[str, ...] = ()
    # How many of `runs` attempts passed. Local models are stochastic: the same
    # unchanged commit scored PASS/FAIL/PASS on one case, so a single sample
    # cannot distinguish a regression from a coin flip (gap I3). With runs=1
    # these collapse to the old boolean behavior.
    passes: int = 1
    runs: int = 1

    @property
    def rate(self) -> float:
        return (self.passes / self.runs) if self.runs else 0.0

    @property
    def flaky(self) -> bool:
        """Passed sometimes and failed sometimes - the result to distrust."""
        return 0 < self.passes < self.runs

    @property
    def status(self) -> str:
        if self.error:
            return "ERROR"
        label = "PASS" if self.passed else "FAIL"
        if self.runs > 1:
            return f"{label} {self.passes}/{self.runs}"
        return label


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
    samples: int = 1,
) -> EvalReport:
    """Run each case in an isolated temp workspace and score it. A driver or
    check raising is recorded as a failed case (never aborts the run).

    Driver precedence: an explicit `driver=` argument (tests) > the case's own
    `driver` (a case measuring a non-loop path) > the default loop driver.

    `samples` runs every case N times and scores it by MAJORITY. One sample
    against a stochastic local model is not a measurement - the same unchanged
    commit produced PASS/FAIL/PASS on one case - so a baseline meant to justify
    "this change is safe" should use samples>=3.
    """
    results: list[EvalResult] = []
    for case in cases:
        run = driver or case.driver or chat_loop_driver
        results.append(await _run_case(case, run, max(1, samples)))
    return EvalReport(results=results, tier=tier)


async def _run_case(case: EvalCase, driver: Driver, samples: int) -> EvalResult:
    attempts = [await _run_one(case, driver) for _ in range(samples)]
    passes = sum(1 for attempt in attempts if attempt.passed)
    # Majority, so one unlucky roll doesn't condemn a good change and one lucky
    # roll doesn't bless a bad one. With samples=1 this is the old behavior.
    passed = passes * 2 > samples
    # Surface a failing attempt's detail over a passing one's: when a case is
    # flaky, the failure is the interesting half.
    representative = next((a for a in attempts if not a.passed), attempts[0])
    return EvalResult(
        name=case.name,
        passed=passed,
        final=representative.final,
        error=representative.error,
        duration_s=sum(attempt.duration_s for attempt in attempts),
        tags=case.tags,
        passes=passes,
        runs=samples,
    )


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


async def planning_driver(workspace: Path, case: EvalCase) -> str:
    """Drive the real planner and return the plan markdown for scoring.

    Planning never goes through the agent loop, so the default driver cannot
    measure it - which is exactly why plan quality shipped unmeasured while the
    planner was the one model output still spliced raw into a coder's prompt.
    """
    from shamsu.agents.plan_mode import PlanningWorkflow
    from shamsu.cli.repl import _build_search_agent
    from shamsu.llm.manager import LLMManager

    search, _uses_real_index = _build_search_agent(workspace, None)
    workflow = PlanningWorkflow(workspace, llm=LLMManager(), search=search)
    plan = await workflow.run(case.prompt, route="code_edit")
    return plan.markdown


async def chat_planner_driver(workspace: Path, case: EvalCase) -> str:
    """Drive `create_plan` exactly as the chat loop does: `results=[]`.

    The chat loop's per-request planner call is context-blind - it never gets
    search results - which is the same trap that made plan_mode hallucinate a
    React component into a vanilla-JS workspace. This measures that path's
    grounding on its own.
    """
    from shamsu.agents.planner import create_plan
    from shamsu.context.builder import ContextBuilder
    from shamsu.llm.manager import LLMManager

    plan = await create_plan(
        LLMManager(),
        ContextBuilder(),
        results=[],
        goal=case.prompt,
        task_id="eval-chat-plan",
        workspace=workspace,
    )
    return plan.text


def render_report(report: EvalReport) -> str:
    """Render a BENCHMARK.md-style pass-rate table."""
    pct = round(report.pass_rate * 100)
    samples = max((result.runs for result in report.results), default=1)
    lines: list[str] = ["# SHAMSU Eval Benchmark", ""]
    tier = report.tier or "(unknown)"
    lines.append(f"- **Tier:** {tier}")
    lines.append(f"- **Pass rate:** {report.passed}/{report.total} ({pct}%)")
    lines.append(f"- **Samples per case:** {samples}" + ("" if samples > 1 else "  <- single-sample: a ±1 delta is noise, not signal"))
    lines.append("")
    lines.append("| Case | Result | Time | Notes |")
    lines.append("|------|--------|------|-------|")
    for result in report.results:
        note = result.error or ("" if result.passed else "check failed")
        if result.flaky:
            # The most important thing this harness can tell you: the case is
            # not answering the same way twice, so neither a PASS nor a FAIL
            # from it means anything on its own.
            note = (note + " " if note else "") + "FLAKY - re-run before trusting"
        lines.append(
            f"| {result.name} | {result.status} | {result.duration_s:.1f}s | {_escape_cell(note)} |"
        )
    lines.append("")
    flaky = [result.name for result in report.results if result.flaky]
    if flaky:
        lines.append(
            f"> **Flaky this run:** {', '.join(flaky)}. These cases passed some attempts and "
            "failed others on the SAME code - do not read a delta from them."
        )
        lines.append("")
    return "\n".join(lines)


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:160]
