"""`WAIT_APPROVAL`, driven by the runtime.

The state used to be a dead end: it returned STOPPED with *"a step requires
approval and no approver is configured"*, so a HIGH-risk step could not proceed
at all. These assert the four ways out of it — approved, denied, timed out, and
no approver — and that each one leaves the right row behind.

The `approvals` table is the record of what a human was actually asked to
authorise, so "the run stopped" and "the run stopped because someone said no"
have to be distinguishable after the fact.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.fake_model import FakeModelClient

from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.enums import AgentState, ApprovalDecision, StepOutcome
from shamsu.interfaces.ids import ProjectId
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession, SessionResult
from shamsu.state import ProjectRecord, StateStore, new_id
from shamsu.state.records import PlanStepRecord
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git
from shamsu.ui.approval import AlwaysApprover, DenyingApprover

pytestmark = pytest.mark.integration


class _Recording:
    """An approver that returns a fixed decision and remembers being asked."""

    def __init__(self, decision: ApprovalDecision) -> None:
        self._decision = decision
        self.asked: list[str] = []

    async def decide(
        self, step: PlanStepRecord, *, reason: str, cancel: CancellationToken
    ) -> ApprovalDecision:
        self.asked.append(reason)
        return self._decision


class _Exploding:
    """An approver that raises. A broken approver must not grant permission."""

    async def decide(
        self, step: PlanStepRecord, *, reason: str, cancel: CancellationToken
    ) -> ApprovalDecision:
        raise RuntimeError("the prompt died")


def _say(**payload: object) -> str:
    return json.dumps(payload)


def _risky_plan() -> str:
    """A plan whose only step is HIGH risk, so approval is required."""
    return _say(
        summary="Remove the legacy module",
        steps=[
            {
                "title": "Delete the legacy module",
                "intent": "remove it",
                "kind": "change",
                "files": ["legacy.py"],
                "acceptance_criteria": ["the file is gone"],
                "required_evidence": ["the file is changed"],
                "risk": "high",
            }
        ],
        grounded_in=[],
    )


def _tool(name: str, **arguments: object) -> str:
    return _say(action="call_tool", tool={"tool": name, "arguments": arguments})


def _script(*after_approval: str) -> list[str]:
    """Investigate, classify, plan — then whatever the approved path needs.

    A denied run never reaches `after_approval`, which is exactly why the two
    cases can share a script prefix: the gate is the only thing that decides
    whether those responses are ever asked for.
    """
    return [
        _say(action="conclude", conclusion="one legacy module, unreferenced"),
        _say(kind="planned", reason="a deletion needs care"),
        _risky_plan(),
        *after_approval,
    ]


#: What an approved step does once it is allowed to run: edit the file it
#: declared, review the diff, and stop. Together these produce the change
#: floor's `file_changed` and `git_diff_reviewed`.
_CARRIES_OUT_THE_EDIT = (
    _tool("file.patch", path="legacy.py", mode="replace_text", find="VALUE = 1", replace=""),
    _tool("git.inspect", subcommand="diff"),
    _say(action="conclude", conclusion="the module is emptied and the diff is reviewed"),
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "legacy.py").write_text("VALUE = 1\n", encoding="utf-8")
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
def project(store: StateStore, repo: Path) -> ProjectRecord:
    return store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root=str(repo), name="demo")
    )


def _run(
    store: StateStore,
    project: ProjectRecord,
    repo: Path,
    approver: object | None,
    *after_approval: str,
) -> SessionResult:
    model = FakeModelClient(_script(*after_approval))
    session = AgentSession(
        store=store,
        runs=RunController(store),
        model=model,
        gateway=ToolGateway(authoring_tools(repo), require_read_before_edit=False),
        compiler=ContextCompiler(model),
        workspace=repo,
        project_id=project.project_id,
        limits=ExecutionLimits(actions_per_step=4),
        approver=approver,  # type: ignore[arg-type]
    )
    return asyncio.run(session.run("delete the legacy module"))


def _approvals(store: StateStore, result: SessionResult) -> list[ApprovalDecision]:
    rows = store._connection.execute(  # noqa: SLF001 - asserting persistence directly
        "SELECT decision FROM approvals WHERE task_id = ?", (result.task_id,)
    ).fetchall()
    return [ApprovalDecision(row["decision"]) for row in rows]


class TestNoApprover:
    def test_the_run_stops_rather_than_proceeding(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        result = _run(store, project, repo, None)
        assert result.final_state is AgentState.STOPPED
        assert "no approver" in result.stopped_because

    def test_nothing_was_edited(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        _run(store, project, repo, None)
        assert (repo / "legacy.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    def test_no_approval_row_is_written(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """Nobody was asked, so there is no request to record."""
        result = _run(store, project, repo, None)
        assert _approvals(store, result) == []


class TestDenial:
    def test_a_denied_step_stops_the_run(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        result = _run(store, project, repo, DenyingApprover())
        assert result.final_state is AgentState.STOPPED
        assert result.completed is False

    def test_the_denial_is_recorded(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """A refusal and an abandonment must not look the same afterwards."""
        result = _run(store, project, repo, DenyingApprover())
        assert _approvals(store, result) == [ApprovalDecision.DENIED]

    def test_the_step_is_closed_not_left_pending(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """A step a human refused is not a step that needs repairing."""
        result = _run(store, project, repo, DenyingApprover())

        plan = store.latest_plan(result.task_id)
        assert plan is not None
        steps = store.get_steps(plan.plan_id)
        assert [step.outcome for step in steps] == [StepOutcome.APPROVAL_REQUIRED]

    def test_nothing_was_edited(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        _run(store, project, repo, DenyingApprover())
        assert (repo / "legacy.py").read_text(encoding="utf-8") == "VALUE = 1\n"


class TestTimeout:
    def test_silence_blocks_exactly_like_a_denial(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        result = _run(store, project, repo, _Recording(ApprovalDecision.TIMED_OUT))
        assert result.final_state is AgentState.STOPPED
        assert result.completed is False

    def test_it_is_recorded_as_timed_out_not_denied(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The distinction is the whole point: silence is not a refusal either."""
        result = _run(store, project, repo, _Recording(ApprovalDecision.TIMED_OUT))
        assert _approvals(store, result) == [ApprovalDecision.TIMED_OUT]

    def test_a_pending_answer_is_treated_as_a_timeout(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The Protocol forbids PENDING; the runtime enforces rather than trusts."""
        result = _run(store, project, repo, _Recording(ApprovalDecision.PENDING))
        assert _approvals(store, result) == [ApprovalDecision.TIMED_OUT]
        assert result.final_state is AgentState.STOPPED


class TestABrokenApproverCannotGrantPermission:
    def test_an_exception_stops_the_run(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        result = _run(store, project, repo, _Exploding())
        assert result.final_state is AgentState.STOPPED
        assert "approver failed" in result.stopped_because

    def test_it_records_a_missing_decision_not_a_refusal(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        result = _run(store, project, repo, _Exploding())
        assert _approvals(store, result) == [ApprovalDecision.TIMED_OUT]


class TestApproval:
    def test_an_approved_step_proceeds_to_execution(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The state the dead end used to make unreachable."""
        result = _run(store, project, repo, AlwaysApprover(), *_CARRIES_OUT_THE_EDIT)
        assert AgentState.EXECUTE_CURRENT_STEP in result.transitions

    def test_the_approval_is_recorded(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        result = _run(store, project, repo, AlwaysApprover(), *_CARRIES_OUT_THE_EDIT)
        assert _approvals(store, result) == [ApprovalDecision.APPROVED]

    def test_the_authorised_edit_actually_happens(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """End to end: approval is what stood between the plan and the file."""
        _run(store, project, repo, AlwaysApprover(), *_CARRIES_OUT_THE_EDIT)
        assert "VALUE = 1" not in (repo / "legacy.py").read_text(encoding="utf-8")

    def test_the_same_script_changes_nothing_when_denied(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The controlled comparison: only the approver differs."""
        _run(store, project, repo, DenyingApprover(), *_CARRIES_OUT_THE_EDIT)
        assert (repo / "legacy.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    def test_the_approver_is_told_what_it_is_authorising(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """ "a high-risk step" is not something a person can consent to."""
        approver = _Recording(ApprovalDecision.DENIED)
        _run(store, project, repo, approver)

        assert len(approver.asked) == 1
        reason = approver.asked[0]
        assert "high" in reason
        assert "legacy.py" in reason
