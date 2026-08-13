"""Attempts to get a task marked complete without doing the work.

Every test here plays the part of a model that wants the run over. None of
these are hypothetical: confident, well-phrased, entirely fabricated completion
claims were the single most common v1 failure, and "the prompt tells it not to"
is not a control.

The claim under test is structural: there is no path from an assertion to a
completed task, only from an *observed tool execution* to a row, and from rows
to a verdict.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from shamsu.interfaces.cancellation import NullCancellationToken
from shamsu.interfaces.enums import EvidenceKind, Phase, StepOutcome
from shamsu.interfaces.ids import EvidenceId, PlanId, ProjectId, RunId, StepId, TaskId, ToolEventId
from shamsu.interfaces.tools import ToolPolicyViolation, ToolRequest
from shamsu.models.contracts import CompletionClaim
from shamsu.state import (
    EvidenceRecord,
    PlanRecord,
    PlanStepRecord,
    ProjectRecord,
    RunRecord,
    StateStore,
    TaskRecord,
    new_id,
)
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git
from shamsu.verification import CompletionGate, EvidenceRecorder, build_report, validate_claim

pytestmark = pytest.mark.adversarial

FLOOR = frozenset({EvidenceKind.FILE_CHANGED, EvidenceKind.GIT_DIFF_REVIEWED})

BROKEN = '''"""Calculator."""


def add(a: int, b: int) -> int:
    return a - b
'''

TEST = """from calc import add


def test_add() -> None:
    assert add(2, 3) == 5
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_text(BROKEN, encoding="utf-8")
    (root / "test_calc.py").write_text(TEST, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    run_git(root, "config", "user.email", "agent@shamsu.local")
    run_git(root, "config", "user.name", "SHAMSU")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "initial", "--no-verify")
    return root


@pytest.fixture
def store() -> StateStore:
    return StateStore(":memory:")


@pytest.fixture
def task(store: StateStore, repo: Path) -> TaskRecord:
    project = store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root=str(repo), name="demo")
    )
    return store.create_task(
        TaskRecord(task_id=TaskId(new_id()), project_id=project.project_id, request="fix add()")
    )


@pytest.fixture
def run(store: StateStore, task: TaskRecord) -> RunRecord:
    return store.create_run(
        RunRecord(run_id=RunId(new_id()), project_id=task.project_id, task_id=task.task_id)
    )


@pytest.fixture
def plan_id(store: StateStore, task: TaskRecord) -> PlanId:
    identifier = PlanId(new_id())
    store.create_plan(
        PlanRecord(plan_id=identifier, task_id=task.task_id, version=1, summary="fix it"),
        [
            PlanStepRecord(
                step_id=StepId(new_id()),
                plan_id=identifier,
                ordinal=0,
                title="Fix add()",
                required_evidence=(EvidenceKind.FILE_CHANGED, EvidenceKind.TESTS_PASSED),
            )
        ],
    )
    return identifier


class TestFabricatedClaims:
    def test_asserting_completion_changes_nothing(
        self, store: StateStore, task: TaskRecord, plan_id: PlanId
    ) -> None:
        """The most confident possible claim, with nothing behind it."""
        claim = CompletionClaim(
            claim="tests_pass",
            summary="I have fixed the function and all tests are passing.",
            evidence_cited=("ran pytest, 4 passed", "reviewed the diff carefully"),
        )

        gate = CompletionGate(store, task.task_id)
        assert gate.check_claim(claim.claim).accepted is False
        assert gate.check_task(plan_id).satisfied is False

    def test_citing_evidence_in_prose_is_not_citing_evidence(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """`evidence_cited` is never consulted. It exists to explain refusals."""
        verified = frozenset()
        for cited in (
            ("file_changed", "tests_passed"),
            ("EvidenceKind.FILE_CHANGED",),
            ("all of them",),
        ):
            claim = CompletionClaim(claim="file_modified", evidence_cited=cited)
            assert validate_claim(claim.claim, verified).accepted is False

    def test_a_claim_naming_a_kind_directly_is_still_a_claim(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """Emitting an enum value where a claim name goes buys nothing."""
        assert validate_claim("file_changed", frozenset(EvidenceKind)).accepted is False
        assert validate_claim("FILE_CHANGED", frozenset(EvidenceKind)).accepted is False


class TestEvidenceCannotBeManufactured:
    def test_a_failing_test_run_registers_nothing(
        self, store: StateStore, task: TaskRecord, run: RunRecord, plan_id: PlanId, repo: Path
    ) -> None:
        """The bug is still in the file. The gate must know."""
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)
        step = store.get_steps(plan_id)[0]

        request = ToolRequest(tool="test.run", arguments={"command": "pytest"})
        with gateway.decision():
            result = asyncio.run(gateway.invoke(request, Phase.VERIFY, NullCancellationToken()))

        assert result.ok is False
        recorded = recorder.record(request, result, Phase.VERIFY, step_id=step.step_id)

        assert recorded.produced_evidence is False
        assert EvidenceKind.TESTS_PASSED not in store.verified_evidence(task.task_id)

    def test_evidence_cannot_exist_without_a_tool_event(
        self, store: StateStore, task: TaskRecord
    ) -> None:
        """The foreign key is the control, not a convention."""
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError):
            store.record_evidence(
                EvidenceRecord(
                    evidence_id=EvidenceId(new_id()),
                    task_id=task.task_id,
                    step_id=None,
                    kind=EvidenceKind.TESTS_PASSED,
                    source_event_id=ToolEventId("invented-event-id"),
                    detail="trust me",
                )
            )

    def test_a_no_op_patch_does_not_earn_file_changed(
        self, store: StateStore, task: TaskRecord, run: RunRecord, repo: Path
    ) -> None:
        """Writing a file's existing content back is not a change."""
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)

        request = ToolRequest(
            tool="file.patch",
            arguments={
                "path": "calc.py",
                "mode": "replace_file",
                "content": BROKEN,
                "acknowledge_overwrite": True,
            },
        )
        with gateway.decision():
            result = asyncio.run(gateway.invoke(request, Phase.AUTHOR, NullCancellationToken()))

        assert result.ok is False
        assert recorder.record(request, result, Phase.AUTHOR).produced_evidence is False

    def test_nothing_can_run_in_the_complete_phase(self, repo: Path) -> None:
        """Plan §20.7 allows one action, and it is not a tool call.

        Enforced by the phase allowlist rather than by the completion code
        remembering to behave: no tool declares COMPLETE, so the surface is
        empty and every call is refused.
        """
        gateway = ToolGateway(authoring_tools(repo), require_read_before_edit=False)
        assert gateway.available(Phase.COMPLETE) == ()

        with pytest.raises(ToolPolicyViolation), gateway.decision():
            asyncio.run(
                gateway.invoke(
                    ToolRequest(tool="file.read", arguments={"path": "calc.py"}),
                    Phase.COMPLETE,
                    NullCancellationToken(),
                )
            )


class TestBorrowedEvidence:
    def test_another_steps_proof_does_not_finish_this_step(
        self, store: StateStore, task: TaskRecord, run: RunRecord
    ) -> None:
        plan = PlanId(new_id())
        steps = [
            PlanStepRecord(
                step_id=StepId(new_id()),
                plan_id=plan,
                ordinal=index,
                title=f"step {index + 1}",
                required_evidence=tuple(sorted(FLOOR, key=lambda kind: kind.value)),
            )
            for index in range(2)
        ]
        store.create_plan(
            PlanRecord(plan_id=plan, task_id=task.task_id, version=1, summary="s"), steps
        )

        event = store.record_tool_event(
            _event(run, task, steps[0].step_id, tool="file.patch", ok=True)
        )
        for kind in FLOOR:
            store.record_evidence(
                EvidenceRecord(
                    evidence_id=EvidenceId(new_id()),
                    task_id=task.task_id,
                    step_id=steps[0].step_id,
                    kind=kind,
                    source_event_id=event.event_id,
                )
            )
        for step in steps:
            store.save_step(step.model_copy(update={"outcome": StepOutcome.PASS}))

        verdict = CompletionGate(store, task.task_id).check_task(plan)
        assert verdict.satisfied is False
        assert "step 2" in verdict.reason

    def test_a_report_of_a_refused_run_says_so_plainly(
        self, store: StateStore, task: TaskRecord, plan_id: PlanId
    ) -> None:
        """No hedging: the artifact a user reads must not imply success."""
        rendered = build_report(store, task.task_id, plan_id).render()
        assert rendered.startswith(f"Task: {task.request}")
        assert "NOT COMPLETE" in rendered
        assert "COMPLETE —" not in rendered.replace("NOT COMPLETE —", "")


def _event(run: RunRecord, task: TaskRecord, step_id: StepId, *, tool: str, ok: bool):
    from shamsu.state import ToolEventRecord

    return ToolEventRecord(
        event_id=ToolEventId(new_id()),
        run_id=run.run_id,
        task_id=task.task_id,
        step_id=step_id,
        tool=tool,
        phase=Phase.AUTHOR,
        arguments_json=json.dumps({"path": "calc.py"}),
        ok=ok,
        output="ok",
    )
