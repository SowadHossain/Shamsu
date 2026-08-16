"""Running the §31.1 suite against a real agent, and scoring it honestly.

The scoring rule is the reason this exists. Two independent answers are
recorded per task:

* **claimed** — `SessionResult.completed`. The runtime's own verdict, from its
  evidence gate.
* **correct** — `EvalTask.check`. A deterministic inspection of the workspace
  that never reads anything the agent said.

Reporting only the first would measure the gate against itself. The interesting
cell is where they disagree: `claimed and not correct` is a **false success**,
and plan §31 makes it a headline metric because v1 incremented its counters at
the site that believed it had succeeded — so `false_success_rate` read zero
exactly when things were worst.

The inverse matters too. `correct and not claimed` means the agent did the job
and could not prove it, which is a gate problem rather than a model problem,
and the two need different fixes.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import statistics
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tests.evals.tasks import EvalTask, materialise

from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.cancellation import Cancelled
from shamsu.interfaces.ids import ProjectId
from shamsu.interfaces.models import ModelClient, ModelUnavailable
from shamsu.memory.store import MemoryStore
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession
from shamsu.state.records import ProjectRecord, new_id
from shamsu.state.store import StateStore
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git
from shamsu.ui.approval import AlwaysApprover

#: Per-task wall clock. Well under `ExecutionLimits`' 1800s default, because a
#: suite of seven that can each run half an hour is a suite nobody runs.
DEFAULT_WALL_CLOCK_SECONDS = 420.0


@dataclass(frozen=True)
class TaskResult:
    """What one task produced, from both sides."""

    task: str
    summary: str

    claimed: bool
    correct: bool
    detail: str

    state: str
    stopped_because: str
    seconds: float
    tool_calls: int
    failed_tool_calls: int
    files_changed: tuple[str, ...]
    error: str = ""

    @property
    def false_success(self) -> bool:
        """Claimed complete, and demonstrably not. The metric that matters."""
        return self.claimed and not self.correct

    @property
    def unproven_success(self) -> bool:
        """Did the job but could not prove it — a gate problem, not a model one."""
        return self.correct and not self.claimed

    def line(self) -> str:
        if self.correct and self.claimed:
            mark = "PASS"
        elif self.false_success:
            mark = "FALSE-OK"
        elif self.unproven_success:
            mark = "UNPROVEN"
        else:
            mark = "FAIL"
        return (
            f"  {mark:<9} {self.summary:<28} {self.seconds:6.1f}s  "
            f"{self.tool_calls:>2} calls  {self.detail or self.stopped_because}"[:200]
        )


@dataclass(frozen=True)
class SuiteResult:
    """The suite's score, as the numbers plan §31 asks for."""

    results: tuple[TaskResult, ...]
    model: str

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def correct(self) -> int:
        return sum(1 for result in self.results if result.correct)

    @property
    def claimed(self) -> int:
        return sum(1 for result in self.results if result.claimed)

    @property
    def false_successes(self) -> tuple[TaskResult, ...]:
        return tuple(result for result in self.results if result.false_success)

    @property
    def unproven(self) -> tuple[TaskResult, ...]:
        return tuple(result for result in self.results if result.unproven_success)

    @property
    def task_success_rate(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def false_success_rate(self) -> float:
        """Of the runs that claimed completion, how many were wrong."""
        return len(self.false_successes) / self.claimed if self.claimed else 0.0

    def render(self) -> str:
        lines = [
            f"§31.1 initial task suite — {self.model}",
            "",
            *[result.line() for result in self.results],
            "",
            f"  task success      {self.correct}/{self.total}  ({self.task_success_rate:.0%})",
            f"  claimed complete  {self.claimed}/{self.total}",
            f"  false successes   {len(self.false_successes)}"
            f"  ({self.false_success_rate:.0%} of claims)",
            f"  unproven wins     {len(self.unproven)}",
        ]
        if self.false_successes:
            lines.extend(
                [
                    "",
                    "  A false success is the worst outcome available: the runtime",
                    "  reported verified completion for work that was not done.",
                    *[f"    {r.summary}: {r.detail}" for r in self.false_successes],
                ]
            )
        return "\n".join(lines)


#: The instant every eval repository is committed at. Any fixed value works;
#: what matters is that it does not move between runs, because the commit
#: hash derived from it is printed into the model's frame by `git.inspect`.
FIXED_COMMIT_DATE = "2020-01-01T00:00:00+00:00"


def prepare(task: EvalTask, workspace: Path) -> Path:
    """Materialise the task's repository and commit it.

    A real git repository, because `git.inspect` produces `GIT_DIFF_REVIEWED`
    and a change step cannot pass its gate without it. An eval run in a
    non-repository would measure a gate that can never open.
    """
    materialise(task, workspace)
    subprocess.run(["git", "init", "-q", str(workspace)], check=True, capture_output=True)
    run_git(workspace, "config", "user.email", "eval@shamsu.local")
    run_git(workspace, "config", "user.name", "SHAMSU eval")
    run_git(workspace, "add", "-A")

    # Committed at a fixed instant so the commit hash is the same every run.
    #
    # This is the whole reason the suite was unrepeatable. `git.inspect` prints
    # recent commits into the frame, a commit hash is a function of its
    # timestamp, and so every run put a different seven-character string in
    # front of the model. Diffing two runs of the same task showed prompts that
    # were byte-identical apart from `c56ade7 initial` against `5b07935
    # initial` — and that was enough to send a temperature-0 loop down a
    # different path and swing the suite by two whole tasks.
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["git", "-C", str(workspace), "commit", "-m", "initial", "--no-verify", "-q"],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": FIXED_COMMIT_DATE,
            "GIT_COMMITTER_DATE": FIXED_COMMIT_DATE,
        },
    )
    return workspace


def run_task(
    task: EvalTask,
    build_model: Callable[[], ModelClient],
    workspace: Path,
    *,
    limits: ExecutionLimits | None = None,
) -> TaskResult:
    """Run one task end to end and score it from both sides.

    Never raises for an ordinary failure: a suite that stops at the first
    exception produces no numbers, and the numbers are the point.
    """
    prepare(task, workspace)

    bounds = limits or ExecutionLimits(wall_clock_seconds=DEFAULT_WALL_CLOCK_SECONDS)
    store = StateStore(workspace / ".shamsu" / "state.db")
    started = time.monotonic()

    claimed = False
    state = "not-started"
    stopped_because = ""
    error = ""
    changed: tuple[str, ...] = ()

    try:
        model = build_model()
        project = store.upsert_project(
            ProjectRecord(project_id=ProjectId(new_id()), root=str(workspace), name=workspace.name)
        )
        session = AgentSession(
            store=store,
            runs=RunController(store),
            model=model,
            gateway=ToolGateway(authoring_tools(workspace), workspace=workspace),
            compiler=ContextCompiler(model),
            workspace=workspace,
            project_id=project.project_id,
            limits=bounds,
            memory=MemoryStore(store, project.project_id),
            # Capability is what is being measured here, not the approval gate,
            # which `tests/integration/test_approval_gate.py` covers on its own.
            approver=AlwaysApprover(),
        )

        result = asyncio.run(session.run(task.request))
        claimed = result.completed
        state = result.final_state.value
        stopped_because = result.stopped_because
        if result.report is not None:
            changed = result.report.changed_files

        events = store.tool_events_for(result.task_id)
        calls = len(events)
        failed = sum(1 for event in events if not event.ok)
    except (Cancelled, ModelUnavailable) as exc:
        error = f"{type(exc).__name__}: {exc}"
        calls = failed = 0
    except Exception as exc:  # noqa: BLE001 - one task must not end the suite
        error = f"{type(exc).__name__}: {exc}"
        calls = failed = 0
    finally:
        store.close()

    seconds = time.monotonic() - started

    # Scored even when the run errored: an agent that crashed may still have
    # left the workspace correct, and pretending otherwise would understate it.
    outcome = task.check(workspace)

    return TaskResult(
        task=task.name,
        summary=task.summary,
        claimed=claimed,
        correct=outcome.correct,
        detail=outcome.detail,
        state=state,
        stopped_because=stopped_because,
        seconds=seconds,
        tool_calls=calls,
        failed_tool_calls=failed,
        files_changed=changed,
        error=error,
    )


def run_suite(
    tasks: Sequence[EvalTask],
    build_model: Callable[[], ModelClient],
    root: Path,
    *,
    model_name: str = "unknown",
    limits: ExecutionLimits | None = None,
    on_result: Callable[[TaskResult], None] | None = None,
) -> SuiteResult:
    """Run every task in its own workspace and collect the score."""
    results: list[TaskResult] = []
    for task in tasks:
        result = run_task(task, build_model, root / task.name, limits=limits)
        results.append(result)
        if on_result is not None:
            on_result(result)
    return SuiteResult(results=tuple(results), model=model_name)


# ---------------------------------------------------------------------------
# Repetition
# ---------------------------------------------------------------------------
#
# A single run of this suite is not a measurement. Nine runs against identical
# code scored anywhere from 1 to 5 of 7, and every change worth making is worth
# one or two tasks — so a before/after pair of single runs cannot distinguish a
# real improvement from resampling. Effort has already gone into changes that
# could not be evaluated.
#
# Two deliberate choices below.
#
# **Every repetition runs at the same absolute path.** The workspace is wiped
# and rebuilt rather than given a per-repetition name. Absolute paths reach the
# model — through error text, through git output, through anything that echoes a
# location — so a `rep-1`/`rep-2` suffix would inject a fresh input difference
# into exactly the experiment that exists to hold inputs constant. What is left
# varying is the model, which is what is being measured.
#
# **A failed repetition is moved aside, not deleted.** The next repetition needs
# the canonical path free; the person debugging needs the workspace that failed.


@dataclass(frozen=True)
class TaskStats:
    """One task's record across repetitions."""

    name: str
    summary: str
    runs: int
    passes: int
    claims: int
    false_successes: int
    unproven: int
    seconds: tuple[float, ...]

    @property
    def pass_rate(self) -> float:
        return self.passes / self.runs if self.runs else 0.0

    @property
    def flaky(self) -> bool:
        """Passed sometimes and failed sometimes.

        Worth naming separately from "fails": a task that passes 3 of 5 is not a
        capability the agent lacks, it is one it does not hold reliably, and the
        two want different fixes.
        """
        return 0 < self.passes < self.runs

    @property
    def median_seconds(self) -> float:
        return statistics.median(self.seconds) if self.seconds else 0.0

    def line(self) -> str:
        mark = "flaky" if self.flaky else ("ok" if self.passes == self.runs else "")
        return (
            f"  {self.summary:<30} {self.passes}/{self.runs:<5} "
            f"{self.claims}/{self.runs:<8} {self.false_successes:<9} "
            f"{self.unproven:<9} {self.median_seconds:5.0f}s  {mark}"
        )


@dataclass(frozen=True)
class RepeatedResult:
    """The suite run several times, scored per task rather than in aggregate.

    `render` deliberately leads with the per-task table and reports the suite
    score as a *range*. A single headline number is what made the previous
    instrument unusable: "5 of 7" reads as a fact, and it was a sample.
    """

    suites: tuple[SuiteResult, ...]
    model: str

    #: Where the task repositories were built. Recorded because it is an input
    #: that reaches the model — through tracebacks, through git output, through
    #: anything that echoes a location — and a comparison across two different
    #: roots is therefore not a comparison of two code versions.
    #:
    #: This is not hypothetical. A before/after pair was run at
    #: `…/shamsu-evals-baseline` and `…/shamsu-evals-after`, and the "after"
    #: numbers moved on tasks whose behaviour no change could plausibly touch.
    #: The instrument now refuses to read such a pair as a result.
    root: str = ""

    @property
    def repetitions(self) -> int:
        return len(self.suites)

    @property
    def stats(self) -> tuple[TaskStats, ...]:
        """Per-task totals, in the order the tasks were run."""
        names: list[str] = []
        for suite in self.suites:
            for result in suite.results:
                if result.task not in names:
                    names.append(result.task)

        collected: list[TaskStats] = []
        for name in names:
            runs = [r for suite in self.suites for r in suite.results if r.task == name]
            if not runs:  # pragma: no cover - names came from these very runs
                continue
            collected.append(
                TaskStats(
                    name=name,
                    summary=runs[0].summary,
                    runs=len(runs),
                    passes=sum(1 for r in runs if r.correct),
                    claims=sum(1 for r in runs if r.claimed),
                    false_successes=sum(1 for r in runs if r.false_success),
                    unproven=sum(1 for r in runs if r.unproven_success),
                    seconds=tuple(r.seconds for r in runs),
                )
            )
        return tuple(collected)

    @property
    def per_run_scores(self) -> tuple[int, ...]:
        """How many tasks each individual repetition got right."""
        return tuple(suite.correct for suite in self.suites)

    @property
    def score_range(self) -> tuple[int, int]:
        scores = self.per_run_scores
        return (min(scores), max(scores)) if scores else (0, 0)

    @property
    def tasks_per_run(self) -> int:
        return max((suite.total for suite in self.suites), default=0)

    @property
    def total_passes(self) -> int:
        return sum(suite.correct for suite in self.suites)

    @property
    def total_runs(self) -> int:
        return sum(suite.total for suite in self.suites)

    @property
    def false_successes(self) -> int:
        return sum(len(suite.false_successes) for suite in self.suites)

    @property
    def flaky(self) -> tuple[TaskStats, ...]:
        return tuple(stat for stat in self.stats if stat.flaky)

    def render(self) -> str:
        low, high = self.score_range
        scores = self.per_run_scores
        spread = f"{low}" if low == high else f"{low}..{high}"

        lines = [
            f"§31.1 task suite — {self.model} — {self.repetitions} repetition(s)",
            "",
            f"  {'task':<30} {'pass':<6} {'claimed':<9} {'false-ok':<9} "
            f"{'unproven':<9} {'median':<6}",
            f"  {'-' * 76}",
            *[stat.line() for stat in self.stats],
            "",
            f"  per-run score     {spread} of {self.tasks_per_run}"
            + (f"   (runs: {', '.join(str(s) for s in scores)})" if self.repetitions > 1 else ""),
            f"  task success      {self.total_passes}/{self.total_runs}"
            f"  ({self.total_passes / self.total_runs:.0%})"
            if self.total_runs
            else "  task success      n/a",
            f"  false successes   {self.false_successes}",
        ]

        if self.repetitions > 1 and low != high:
            lines += [
                "",
                f"  This suite spans {high - low} task(s) between repetitions of identical",
                "  code. Any change smaller than that cannot be read from these numbers.",
            ]
        if self.flaky:
            lines += [
                "",
                "  Flaky — passes sometimes, on the same input:",
                *[f"    {s.summary}: {s.passes}/{s.runs}" for s in self.flaky],
            ]
        if self.repetitions == 1:
            lines += [
                "",
                "  One repetition is a sample, not a measurement. Use --repeat 5 before",
                "  concluding that a change helped.",
            ]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """A baseline document: enough to compare against, and nothing derived."""
        return {
            "model": self.model,
            "root": self.root,
            "repetitions": self.repetitions,
            "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "per_run_scores": list(self.per_run_scores),
            "tasks_per_run": self.tasks_per_run,
            "tasks": {
                stat.name: {
                    "summary": stat.summary,
                    "runs": stat.runs,
                    "passes": stat.passes,
                    "claims": stat.claims,
                    "false_successes": stat.false_successes,
                    "unproven": stat.unproven,
                }
                for stat in self.stats
            },
        }


def run_repeated(
    tasks: Sequence[EvalTask],
    build_model: Callable[[], ModelClient],
    root: Path,
    *,
    repeat: int = 1,
    model_name: str = "unknown",
    limits: ExecutionLimits | None = None,
    on_result: Callable[[int, TaskResult], None] | None = None,
    on_suite: Callable[[int, SuiteResult], None] | None = None,
) -> RepeatedResult:
    """Run the suite `repeat` times at identical paths and collect every run."""
    suites: list[SuiteResult] = []

    for index in range(1, max(1, repeat) + 1):
        for task in tasks:
            _reset_workspace(root / task.name, root, task.name, index)

        suite = run_suite(
            tasks,
            build_model,
            root,
            model_name=model_name,
            limits=limits,
            on_result=(lambda result, n=index: on_result(n, result)) if on_result else None,
        )
        suites.append(suite)
        if on_suite is not None:
            on_suite(index, suite)

    return RepeatedResult(suites=tuple(suites), model=model_name, root=str(root))


def _reset_workspace(workspace: Path, root: Path, task: str, repetition: int) -> None:
    """Free the canonical path for the next repetition, keeping what was there.

    Moved rather than removed: the workspace a repetition failed in is the only
    place its state survives, and a suite that deletes it makes a flaky task
    undebuggable. Only the *previous* repetition's directory is kept per task,
    which is enough to look at and not enough to fill a disk.
    """
    if not workspace.exists():
        return

    keep = root / "previous" / f"{task}-rep{repetition - 1}"
    shutil.rmtree(keep, ignore_errors=True)
    keep.parent.mkdir(parents=True, exist_ok=True)
    try:
        workspace.replace(keep)
    except OSError:
        # A file still held open (a stray handle on Windows) must not end the
        # suite; losing the previous workspace is a debugging inconvenience,
        # and refusing to run the next repetition is a lost measurement.
        shutil.rmtree(workspace, ignore_errors=True)


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Comparison:
    """A current result read against a recorded baseline.

    The verdict rule is deliberately conservative, and says so in its output.
    With five repetitions, 3/5 against 4/5 is not evidence of anything: the
    honest report is *inconclusive*, and a harness that rounds that up to
    "improved" is how a project convinces itself a change worked.

    So a direction is only claimed when the two sets of observations do not
    overlap at all — every current run beat every baseline run, or the reverse.
    Anything else is inconclusive and the fix has to be judged some other way,
    or measured with more repetitions.
    """

    baseline: Mapping[str, Any]
    current: RepeatedResult

    @property
    def conclusive(self) -> bool:
        return self.verdict in ("better", "worse")

    @property
    def comparable(self) -> bool:
        """Whether the two runs differ only in the code under test.

        A different workspace root means a different absolute path in every
        traceback the model reads, which is an input change wearing the costume
        of a code change.
        """
        recorded = str(self.baseline.get("root") or "")
        if not recorded or not self.current.root:
            # An older baseline predates this field. Unknown is not the same as
            # equal, and the honest reading of unknown is "cannot be sure".
            return False
        try:
            return Path(recorded).resolve() == Path(self.current.root).resolve()
        except OSError:  # pragma: no cover - defensive
            return recorded == self.current.root

    @property
    def verdict(self) -> str:
        if not self.comparable:
            return "incomparable"

        base = [int(score) for score in self.baseline.get("per_run_scores", [])]
        now = list(self.current.per_run_scores)
        if not base or not now:
            return "inconclusive"
        if min(now) > max(base):
            return "better"
        if max(now) < min(base):
            return "worse"
        return "inconclusive"

    def task_lines(self) -> list[str]:
        recorded = self.baseline.get("tasks", {})
        lines: list[str] = []
        for stat in self.current.stats:
            was = recorded.get(stat.name)
            if not isinstance(was, Mapping):
                lines.append(f"  {stat.summary:<30} {stat.passes}/{stat.runs}   (new)")
                continue
            before = f"{was.get('passes', 0)}/{was.get('runs', 0)}"
            after = f"{stat.passes}/{stat.runs}"
            moved = "" if before == after else "  <-- moved"
            lines.append(f"  {stat.summary:<30} {before:>7} -> {after:<7}{moved}")
        return lines

    def render(self) -> str:
        base = [int(score) for score in self.baseline.get("per_run_scores", [])]
        now = list(self.current.per_run_scores)
        base_span = f"{min(base)}..{max(base)}" if base else "n/a"
        now_span = f"{min(now)}..{max(now)}" if now else "n/a"

        lines = [
            f"compared against baseline recorded {self.baseline.get('recorded_at', 'unknown')}"
            f" ({self.baseline.get('model', 'unknown model')})",
            "",
            *self.task_lines(),
            "",
            f"  baseline per-run  {base_span}   (runs: {', '.join(str(s) for s in base)})",
            f"  current per-run   {now_span}   (runs: {', '.join(str(s) for s in now)})",
            "",
            f"  VERDICT: {self.verdict.upper()}",
        ]
        if self.verdict == "incomparable":
            lines += [
                "",
                f"  baseline root  {self.baseline.get('root') or '(not recorded)'}",
                f"  current root   {self.current.root or '(not recorded)'}",
                "",
                "  These runs used different workspace roots, so they differ in an",
                "  input the model sees — absolute paths reach it through tracebacks",
                "  and git output. Re-run with --workspaces set to the baseline's root.",
            ]
        elif not self.conclusive:
            lines += [
                "",
                "  The two ranges overlap, so these runs do not establish a direction.",
                "  This is not a failure of the change; it is the resolution of the",
                "  instrument. Raise --repeat, or judge the change on something other",
                "  than this suite.",
            ]
        return "\n".join(lines)


def load_baseline(path: Path) -> dict[str, Any]:
    """Read a recorded baseline document."""
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):  # pragma: no cover - defensive
        raise ValueError(f"{path} does not contain a baseline object")
    return loaded


def save_baseline(result: RepeatedResult, path: Path) -> None:
    """Write a baseline document, creating the directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")


__all__ = [
    "DEFAULT_WALL_CLOCK_SECONDS",
    "Comparison",
    "RepeatedResult",
    "SuiteResult",
    "TaskResult",
    "TaskStats",
    "load_baseline",
    "prepare",
    "run_repeated",
    "run_suite",
    "run_task",
    "save_baseline",
]
