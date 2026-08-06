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

import asyncio
import contextlib
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class CheckOutcome:
    passed: bool
    note: str = ""


# A check is deterministic: given the final workspace and the agent's final
# answer text, decide pass/fail. File-oriented cases ignore the text; QA cases
# ignore the workspace. Richer cases can return CheckOutcome to preserve a
# concise failure note in the benchmark report.
CheckFn = Callable[[Path, str], bool | CheckOutcome]
# A seed prepares the starting workspace (write files, etc.).
SeedFn = Callable[[Path], None]
# A driver runs a case's prompt against a workspace and returns the final answer.
Driver = Callable[[Path, "EvalCase"], Awaitable[str]]
# Progress events are intentionally plain dictionaries so CLI/reporting callers
# can add fields without migrating a serialized schema.
ProgressFn = Callable[[dict[str, object]], None]
_DEFAULT_PROGRESS_INTERVAL_S = 30.0


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
    note: str = ""
    workspace: str = ""
    attempt_workspaces: tuple[str, ...] = ()
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
    artifacts_dir: str = ""

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
    artifacts_dir: Path | None = None,
    progress: ProgressFn | None = None,
    progress_interval_s: float = _DEFAULT_PROGRESS_INTERVAL_S,
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
    sample_count = max(1, samples)
    run_artifacts_dir = _prepare_artifacts_dir(artifacts_dir) if artifacts_dir else None
    _emit_progress(
        progress,
        "run_start",
        cases=len(cases),
        samples=sample_count,
        total_attempts=len(cases) * sample_count,
        artifacts_dir=str(run_artifacts_dir) if run_artifacts_dir else "",
        tier=tier,
    )
    results: list[EvalResult] = []
    for case_index, case in enumerate(cases):
        run = driver or case.driver or chat_loop_driver
        results.append(
            await _run_case(
                case,
                run,
                sample_count,
                run_artifacts_dir,
                progress=progress,
                progress_interval_s=progress_interval_s,
                case_index=case_index,
                total_cases=len(cases),
                total_attempts=len(cases) * sample_count,
            )
        )
    report = EvalReport(
        results=results,
        tier=tier,
        artifacts_dir=str(run_artifacts_dir) if run_artifacts_dir else "",
    )
    _emit_progress(
        progress,
        "run_finish",
        passed=report.passed,
        total=report.total,
        pass_rate=report.pass_rate,
    )
    return report


async def _run_case(
    case: EvalCase,
    driver: Driver,
    samples: int,
    artifacts_dir: Path | None,
    *,
    progress: ProgressFn | None = None,
    progress_interval_s: float = _DEFAULT_PROGRESS_INTERVAL_S,
    case_index: int = 0,
    total_cases: int = 1,
    total_attempts: int = 1,
) -> EvalResult:
    _emit_progress(
        progress,
        "case_start",
        case=case.name,
        case_index=case_index + 1,
        total_cases=total_cases,
        samples=samples,
        long_running=case.long_running,
    )
    attempts: list[EvalResult] = []
    for index in range(samples):
        attempts.append(
            await _run_one(
                case,
                driver,
                sample_index=index,
                samples=samples,
                artifacts_dir=artifacts_dir,
                progress=progress,
                progress_interval_s=progress_interval_s,
                case_index=case_index,
                total_cases=total_cases,
                total_attempts=total_attempts,
            )
        )
    passes = sum(1 for attempt in attempts if attempt.passed)
    # Majority, so one unlucky roll doesn't condemn a good change and one lucky
    # roll doesn't bless a bad one. With samples=1 this is the old behavior.
    passed = passes * 2 > samples
    # Surface a failing attempt's detail over a passing one's: when a case is
    # flaky, the failure is the interesting half.
    representative = next((a for a in attempts if not a.passed), attempts[0])
    result = EvalResult(
        name=case.name,
        passed=passed,
        final=representative.final,
        error=representative.error,
        note=representative.note,
        workspace=representative.workspace,
        attempt_workspaces=tuple(
            attempt.workspace for attempt in attempts if attempt.workspace
        ),
        duration_s=sum(attempt.duration_s for attempt in attempts),
        tags=case.tags,
        passes=passes,
        runs=samples,
    )
    _emit_progress(
        progress,
        "case_finish",
        case=case.name,
        case_index=case_index + 1,
        total_cases=total_cases,
        passed=result.passed,
        passes=passes,
        runs=samples,
        flaky=result.flaky,
        duration_s=result.duration_s,
    )
    return result


async def _run_one(
    case: EvalCase,
    driver: Driver,
    *,
    sample_index: int = 0,
    samples: int = 1,
    artifacts_dir: Path | None = None,
    progress: ProgressFn | None = None,
    progress_interval_s: float = _DEFAULT_PROGRESS_INTERVAL_S,
    case_index: int = 0,
    total_cases: int = 1,
    total_attempts: int = 1,
) -> EvalResult:
    attempt_index = case_index * samples + sample_index + 1
    if artifacts_dir is not None:
        workspace = artifacts_dir / _safe_name(case.name) / f"sample_{sample_index + 1}"
        workspace.mkdir(parents=True)
        return await _run_one_in_workspace(
            case,
            driver,
            workspace,
            keep_workspace=True,
            sample_index=sample_index,
            samples=samples,
            progress=progress,
            progress_interval_s=progress_interval_s,
            case_index=case_index,
            total_cases=total_cases,
            attempt_index=attempt_index,
            total_attempts=total_attempts,
        )

    # ignore_cleanup_errors: on Windows the agent may leave a handle briefly open.
    with tempfile.TemporaryDirectory(prefix="shamsu_eval_", ignore_cleanup_errors=True) as tmp:
        workspace = Path(tmp)
        return await _run_one_in_workspace(
            case,
            driver,
            workspace,
            keep_workspace=False,
            sample_index=sample_index,
            samples=samples,
            progress=progress,
            progress_interval_s=progress_interval_s,
            case_index=case_index,
            total_cases=total_cases,
            attempt_index=attempt_index,
            total_attempts=total_attempts,
        )


async def _run_one_in_workspace(
    case: EvalCase,
    driver: Driver,
    workspace: Path,
    *,
    keep_workspace: bool,
    sample_index: int = 0,
    samples: int = 1,
    progress: ProgressFn | None = None,
    progress_interval_s: float = _DEFAULT_PROGRESS_INTERVAL_S,
    case_index: int = 0,
    total_cases: int = 1,
    attempt_index: int = 1,
    total_attempts: int = 1,
) -> EvalResult:
    started = time.perf_counter()
    _emit_progress(
        progress,
        "sample_start",
        case=case.name,
        case_index=case_index + 1,
        total_cases=total_cases,
        sample=sample_index + 1,
        samples=samples,
        attempt=attempt_index,
        total_attempts=total_attempts,
        workspace=str(workspace),
        long_running=case.long_running,
    )
    try:
        if case.seed is not None:
            case.seed(workspace)
        final = await _run_driver_with_heartbeat(
            case,
            driver,
            workspace,
            progress=progress,
            progress_interval_s=progress_interval_s,
            started=started,
            sample_index=sample_index,
            samples=samples,
            case_index=case_index,
            total_cases=total_cases,
            attempt_index=attempt_index,
            total_attempts=total_attempts,
        )
        outcome = case.check(workspace, final or "")
        if isinstance(outcome, CheckOutcome):
            passed = bool(outcome.passed)
            note = outcome.note
        else:
            passed = bool(outcome)
            note = ""
        error = ""
    except Exception as exc:  # a broken case must not sink the whole run
        final = ""
        passed = False
        error = f"{type(exc).__name__}: {exc}"
        note = ""
    duration = time.perf_counter() - started
    result = EvalResult(
        name=case.name,
        passed=passed,
        final=final or "",
        error=error,
        note=note,
        workspace=str(workspace) if keep_workspace else "",
        duration_s=duration,
        tags=case.tags,
    )
    _emit_progress(
        progress,
        "sample_finish",
        case=case.name,
        case_index=case_index + 1,
        total_cases=total_cases,
        sample=sample_index + 1,
        samples=samples,
        attempt=attempt_index,
        total_attempts=total_attempts,
        passed=result.passed,
        error=result.error,
        note=result.note,
        duration_s=result.duration_s,
        workspace=result.workspace or str(workspace),
    )
    return result


async def _run_driver_with_heartbeat(
    case: EvalCase,
    driver: Driver,
    workspace: Path,
    *,
    progress: ProgressFn | None,
    progress_interval_s: float,
    started: float,
    sample_index: int,
    samples: int,
    case_index: int,
    total_cases: int,
    attempt_index: int,
    total_attempts: int,
) -> str:
    if progress is None or progress_interval_s <= 0 or not case.long_running:
        return await driver(workspace, case)

    task = asyncio.create_task(driver(workspace, case))
    try:
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(task), timeout=progress_interval_s)
            except TimeoutError:
                _emit_progress(
                    progress,
                    "sample_heartbeat",
                    case=case.name,
                    case_index=case_index + 1,
                    total_cases=total_cases,
                    sample=sample_index + 1,
                    samples=samples,
                    attempt=attempt_index,
                    total_attempts=total_attempts,
                    elapsed_s=time.perf_counter() - started,
                    workspace=str(workspace),
                )
    except asyncio.CancelledError:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise


def _emit_progress(progress: ProgressFn | None, event: str, **payload: object) -> None:
    if progress is None:
        return
    progress({"event": event, **payload})


async def full_request_driver(workspace: Path, case: EvalCase) -> str:
    """Drive the complete user request path, including routing and logging."""
    from shamsu.cli.noninteractive import run_prompt

    result = await run_prompt(workspace, case.prompt, approval="allow")
    return result.final_response


# Compatibility for callers that imported the old name. The implementation is
# intentionally the full dispatcher now, not AgentChatLoop in isolation.
chat_loop_driver = full_request_driver


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
    if report.artifacts_dir:
        lines.append(f"- **Artifacts:** `{report.artifacts_dir}`")
    lines.append("")
    lines.append("| Case | Result | Time | Notes |")
    lines.append("|------|--------|------|-------|")
    for result in report.results:
        note = result.error or result.note or ("" if result.passed else "check failed")
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
    # Standing footer, part of render() on purpose: BENCHMARK files are
    # regenerated wholesale every baseline, so a hand-appended caveat section
    # survives exactly one run before being clobbered and forgotten.
    lines.append("## Reading these numbers")
    lines.append("")
    lines.append(
        "Local models are stochastic. A case flagged flaky passed some attempts\n"
        "and failed others on identical code - treat its row as noise until\n"
        "re-measured with more samples. Compare baselines only at equal\n"
        "`--samples`; a delta that lives entirely inside the flaky set is no\n"
        "delta. Tier-specific findings (root causes of consistent failures)\n"
        "live in `agent context/SHAMSU_agent_gap_analysis.md` under I3."
    )
    lines.append("")
    lines.append("## Deterministic release metrics")
    lines.append("")
    lines.append(
        "Harness startup, first-answer/task time, peak memory, log growth, and "
        "Python/Django/Node/React/mixed dogfood results are recorded separately "
        "in `RELEASE_VALIDATION.md` so model variance is not mixed with runtime reliability."
    )
    lines.append("")
    return "\n".join(lines)


def _escape_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:160]


def _prepare_artifacts_dir(root: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    path = Path(root).resolve() / f"eval_{stamp}_{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _safe_name(name: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in name)
    return safe.strip("._") or "case"
