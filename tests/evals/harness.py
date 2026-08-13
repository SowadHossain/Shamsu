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
import subprocess
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

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
    run_git(workspace, "commit", "-m", "initial", "--no-verify")
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
            gateway=ToolGateway(authoring_tools(workspace)),
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


__all__ = [
    "DEFAULT_WALL_CLOCK_SECONDS",
    "SuiteResult",
    "TaskResult",
    "prepare",
    "run_suite",
    "run_task",
]
