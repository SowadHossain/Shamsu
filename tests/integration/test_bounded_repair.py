"""Milestone 7: fix a simple failure without uncontrolled edits.

The exit condition has two halves, and the second is the one v1 failed. It is
not enough that the agent fixes the bug — it must not touch anything else on
the way, and it must stop when it is not getting anywhere.

Real repository, real pytest, real git. The model is fake because this box has
no GPU; nothing in the repair path needs one.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from shamsu.agent.repair import RepairController, RepairScope
from shamsu.interfaces.cancellation import NullCancellationToken
from shamsu.interfaces.enums import EvidenceKind, Phase, StepOutcome
from shamsu.interfaces.ids import PlanId, ProjectId, RunId, StepId, TaskId
from shamsu.interfaces.tools import ToolPolicyViolation, ToolRequest
from shamsu.state import (
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
from shamsu.verification import EvidenceRecorder, check_completion

pytestmark = pytest.mark.integration

BROKEN = '''"""Calculator."""


def add(a: int, b: int) -> int:
    return a - b
'''

UNRELATED = '''"""Nothing to do with the failure."""

VALUE = 1
'''

TESTS = """from calc import add


def test_add() -> None:
    assert add(2, 3) == 5
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_text(BROKEN, encoding="utf-8")
    (root / "unrelated.py").write_text(UNRELATED, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_calc.py").write_text(TESTS, encoding="utf-8")
    (root / "conftest.py").write_text("import sys; sys.path.insert(0, '.')\n", encoding="utf-8")
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
def step_id(store: StateStore, task: TaskRecord) -> StepId:
    plan = PlanId(new_id())
    identifier = StepId(new_id())
    store.create_plan(
        PlanRecord(plan_id=plan, task_id=task.task_id, version=1, summary="fix it"),
        [
            PlanStepRecord(
                step_id=identifier,
                plan_id=plan,
                ordinal=0,
                title="Fix add()",
                inputs=("calc.py",),
                required_evidence=(EvidenceKind.FILE_CHANGED, EvidenceKind.TESTS_PASSED),
            )
        ],
    )
    return identifier


def _invoke(gateway: ToolGateway, tool: str, phase: Phase, **arguments: object):
    request = ToolRequest(tool=tool, arguments=arguments)
    with gateway.decision():
        result = asyncio.run(gateway.invoke(request, phase, NullCancellationToken()))
    return request, result


class TestRepairStaysInScope:
    def test_a_failure_becomes_a_capsule_and_a_scope(
        self, store: StateStore, task: TaskRecord, run: RunRecord, step_id: StepId, repo: Path
    ) -> None:
        gateway = ToolGateway(authoring_tools(repo))
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)

        request, result = _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")
        assert result.ok is False
        recorder.record(request, result, Phase.VERIFY, step_id=step_id)

        tool = gateway.get("test.run")
        assert tool is not None
        digest = tool.last_digest  # type: ignore[attr-defined]
        assert digest is not None

        step = store.get_step(step_id)
        assert step is not None
        decision = RepairController(store, task.task_id).consider(
            digest, step_id=step_id, related_files=step.inputs
        )

        assert decision.proceed is True
        assert decision.capsule is not None
        # The traceback names only the test: `add()` returned the wrong value
        # without raising, so no frame points at it. The step's declared files
        # are what put the buggy source in scope.
        assert decision.capsule.implicated_files == ("tests/test_calc.py",)
        assert "calc.py" in decision.capsule.editable()
        assert decision.scope is not None
        assert decision.scope.permits("calc.py") is True
        assert decision.scope.permits("unrelated.py") is False

    def test_the_gateway_enforces_the_scope_not_the_controller(
        self, store: StateStore, task: TaskRecord, repo: Path
    ) -> None:
        """A restriction that lives in the caller is one a different caller lacks."""
        gateway = ToolGateway(authoring_tools(repo))
        scope = RepairScope(allowed=frozenset({"calc.py"}), protected=frozenset({"tests/"}))

        with (
            gateway.restricted_to(scope),
            pytest.raises(ToolPolicyViolation, match="outside the permitted write scope"),
        ):
            _invoke(
                gateway,
                "file.patch",
                Phase.REPAIR,
                path="unrelated.py",
                find="VALUE = 1",
                replace="VALUE = 2",
            )

        # The refused write did not happen.
        assert (repo / "unrelated.py").read_text(encoding="utf-8") == UNRELATED

    def test_a_refused_write_does_not_spend_the_mutation_budget(
        self, store: StateStore, task: TaskRecord, repo: Path
    ) -> None:
        """Otherwise one out-of-scope attempt costs the decision its only edit."""
        gateway = ToolGateway(authoring_tools(repo))
        scope = RepairScope(allowed=frozenset({"calc.py"}))

        with gateway.restricted_to(scope), gateway.decision():
            with pytest.raises(ToolPolicyViolation):
                asyncio.run(
                    gateway.invoke(
                        ToolRequest(
                            tool="file.patch",
                            arguments={
                                "path": "unrelated.py",
                                "find": "VALUE = 1",
                                "replace": "VALUE = 2",
                            },
                        ),
                        Phase.REPAIR,
                        NullCancellationToken(),
                    )
                )
            assert gateway.mutations_remaining == 1

            result = asyncio.run(
                gateway.invoke(
                    ToolRequest(
                        tool="file.patch",
                        arguments={
                            "path": "calc.py",
                            "find": "return a - b",
                            "replace": "return a + b",
                        },
                    ),
                    Phase.REPAIR,
                    NullCancellationToken(),
                )
            )
            assert result.ok is True

    def test_the_scope_is_restored_when_the_block_ends(self, repo: Path) -> None:
        """Nesting narrows; it never widens."""
        gateway = ToolGateway(authoring_tools(repo))
        assert gateway.scope is None

        outer = RepairScope(allowed=frozenset({"calc.py"}))
        inner = RepairScope(allowed=frozenset())
        with gateway.restricted_to(outer):
            with gateway.restricted_to(inner):
                assert gateway.scope is inner
            assert gateway.scope is outer
        assert gateway.scope is None

    def test_reads_are_never_restricted(self, repo: Path) -> None:
        """Repair may read affected files; it is writing that is bounded."""
        gateway = ToolGateway(authoring_tools(repo))
        with gateway.restricted_to(RepairScope(allowed=frozenset())):
            _, result = _invoke(gateway, "file.read", Phase.REPAIR, path="unrelated.py")
        assert result.ok is True

    def test_checkpointing_is_not_blocked_by_a_write_scope(self, repo: Path) -> None:
        """`git.checkpoint` records what is already on disk; it writes no file."""
        gateway = ToolGateway(authoring_tools(repo))
        _invoke(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            find="return a - b",
            replace="return a + b",
        )
        with gateway.restricted_to(RepairScope(allowed=frozenset({"calc.py"}))):
            _, result = _invoke(gateway, "git.checkpoint", Phase.REPAIR, label="repair")
        assert result.ok is True


class TestVerificationReadsTheCurrentSource:
    def test_a_same_size_edit_in_the_same_second_is_not_masked_by_bytecode(
        self, repo: Path
    ) -> None:
        """A stale `.pyc` would report a fixed bug as still broken.

        CPython validates cached bytecode against the source's (mtime in whole
        seconds, size). `return a - b` → `return a + b` changes neither, and a
        repair lands within a second of the run that motivated it. Without a
        per-run cache prefix the second run executes the *old* bytecode, the
        agent repairs a bug that no longer exists, and same-failure detection
        eventually blocks a task that was already finished.
        """
        gateway = ToolGateway(authoring_tools(repo))

        _, first = _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")
        assert first.ok is False

        _invoke(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            find="return a - b",
            replace="return a + b",
        )
        assert len((repo / "calc.py").read_bytes()) == len(BROKEN.encode())

        _, second = _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")
        assert second.ok is True, second.error

    def test_running_tests_leaves_no_bytecode_in_the_workspace(self, repo: Path) -> None:
        """A checkpoint diff should show real changes, not compilation debris."""
        gateway = ToolGateway(authoring_tools(repo))
        _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")

        assert list(repo.rglob("__pycache__")) == []


class TestRepairFixesTheBug:
    def test_a_simple_failure_is_fixed_within_scope(
        self, store: StateStore, task: TaskRecord, run: RunRecord, step_id: StepId, repo: Path
    ) -> None:
        """Milestone 7's exit condition, end to end."""
        gateway = ToolGateway(authoring_tools(repo))
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)
        controller = RepairController(store, task.task_id)

        request, result = _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")
        recorder.record(request, result, Phase.VERIFY, step_id=step_id)
        assert result.ok is False

        tool = gateway.get("test.run")
        assert tool is not None
        step = store.get_step(step_id)
        assert step is not None
        decision = controller.consider(
            tool.last_digest,  # type: ignore[attr-defined]
            step_id=step_id,
            related_files=step.inputs,
        )
        assert decision.proceed is True and decision.scope is not None

        with gateway.restricted_to(decision.scope):
            request, result = _invoke(
                gateway,
                "file.patch",
                Phase.REPAIR,
                path="calc.py",
                find="return a - b",
                replace="return a + b",
            )
            assert result.ok is True
            recorder.record(request, result, Phase.REPAIR, step_id=step_id)

            request, result = _invoke(gateway, "test.run", Phase.REPAIR, command="pytest")
            assert result.ok is True, result.error
            recorder.record(request, result, Phase.REPAIR, step_id=step_id)

        gate = check_completion(
            (EvidenceKind.FILE_CHANGED, EvidenceKind.TESTS_PASSED),
            recorder.verified(step_id),
        )
        assert gate.satisfied is True

        # Nothing outside the failure was touched.
        assert (repo / "unrelated.py").read_text(encoding="utf-8") == UNRELATED
        assert (repo / "tests" / "test_calc.py").read_text(encoding="utf-8") == TESTS

    def test_a_repair_cannot_delete_the_evidence_that_it_failed(
        self, store: StateStore, task: TaskRecord, run: RunRecord, step_id: StepId, repo: Path
    ) -> None:
        """The most attractive wrong move: edit the test until it passes."""
        gateway = ToolGateway(authoring_tools(repo))
        recorder = EvidenceRecorder(store, run.run_id, task.task_id)

        request, result = _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")
        recorder.record(request, result, Phase.VERIFY, step_id=step_id)

        tool = gateway.get("test.run")
        assert tool is not None
        step = store.get_step(step_id)
        assert step is not None
        decision = RepairController(store, task.task_id).consider(
            tool.last_digest,  # type: ignore[attr-defined]
            step_id=step_id,
            related_files=step.inputs,
        )
        assert decision.scope is not None

        with (
            gateway.restricted_to(decision.scope),
            pytest.raises(ToolPolicyViolation, match="outside the permitted write scope"),
        ):
            _invoke(
                gateway,
                "file.patch",
                Phase.REPAIR,
                path="tests/test_calc.py",
                find="assert add(2, 3) == 5",
                replace="assert True",
            )

        assert (repo / "tests" / "test_calc.py").read_text(encoding="utf-8") == TESTS

    def test_grinding_on_the_same_failure_stops(
        self, store: StateStore, task: TaskRecord, run: RunRecord, step_id: StepId, repo: Path
    ) -> None:
        """A repair that changes nothing must not consume the whole budget."""
        gateway = ToolGateway(authoring_tools(repo))
        controller = RepairController(store, task.task_id)
        tool = gateway.get("test.run")
        assert tool is not None

        step = store.get_step(step_id)
        assert step is not None

        _invoke(gateway, "test.run", Phase.VERIFY, command="pytest")
        first = controller.consider(
            tool.last_digest,  # type: ignore[attr-defined]
            step_id=step_id,
            related_files=step.inputs,
        )
        assert first.proceed is True

        # An ineffective "repair": a comment. The tests fail identically.
        with gateway.restricted_to(RepairScope(allowed=frozenset({"calc.py"}))):
            _invoke(
                gateway,
                "file.patch",
                Phase.REPAIR,
                path="calc.py",
                find='"""Calculator."""',
                replace='"""Calculator. Now with a comment."""',
            )

        _invoke(gateway, "test.run", Phase.REPAIR, command="pytest")
        second = controller.consider(
            tool.last_digest,  # type: ignore[attr-defined]
            step_id=step_id,
            related_files=step.inputs,
        )

        assert second.proceed is False
        assert second.outcome is StepOutcome.BLOCKED
        assert "will not change it" in second.reason
        assert len(store.failures_for(task.task_id, step_id=step_id)) == 2
