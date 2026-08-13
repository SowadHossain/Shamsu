"""Milestone 5: controlled editing with verified evidence.

Real files, real git, real subprocesses. The property under test is that a
change is only ever *claimed* complete when the evidence table says so — and
that evidence table cannot be written to by anything except an observed tool
execution.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.fake_model import CancelAfter

from shamsu.interfaces.cancellation import Cancelled, NullCancellationToken
from shamsu.interfaces.enums import EvidenceKind, Phase
from shamsu.interfaces.ids import ProjectId, RunId, TaskId
from shamsu.interfaces.tools import ToolPolicyViolation, ToolRequest
from shamsu.state import ProjectRecord, RunRecord, StateStore, TaskRecord
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git
from shamsu.verification import EvidenceRecorder, check_completion

pytestmark = pytest.mark.integration

CALC = '''"""Calculator."""


def add(a: int, b: int) -> int:
    return a - b
'''

TEST_CALC = """from calc import add


def test_add() -> None:
    assert add(2, 3) == 5
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_text(CALC, encoding="utf-8")
    (root / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    run_git(root, "config", "user.email", "agent@shamsu.local")
    run_git(root, "config", "user.name", "SHAMSU")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "initial", "--no-verify")
    return root


@pytest.fixture
def gateway(repo: Path) -> ToolGateway:
    return ToolGateway(authoring_tools(repo), require_read_before_edit=False)


def _run(gateway: ToolGateway, tool: str, phase: Phase, **arguments: object):
    return asyncio.run(
        gateway.invoke(ToolRequest(tool=tool, arguments=arguments), phase, NullCancellationToken())
    )


# ---------------------------------------------------------------------------
# file.patch
# ---------------------------------------------------------------------------


class TestFilePatch:
    def test_an_anchored_edit_applies(self, repo: Path, gateway: ToolGateway) -> None:
        result = _run(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            find="return a - b",
            replace="return a + b",
        )
        assert result.ok is True
        assert "return a + b" in (repo / "calc.py").read_text()
        assert "Patched calc.py" in result.output

    def test_the_result_is_a_diff_not_the_whole_file(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        result = _run(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            find="return a - b",
            replace="return a + b",
        )
        assert "-    return a - b" in result.output
        assert "+    return a + b" in result.output

    def test_a_missing_anchor_fails_honestly(self, repo: Path, gateway: ToolGateway) -> None:
        """A stale anchor must fail rather than guess where to apply."""
        result = _run(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            find="return a * b",
            replace="return a + b",
        )
        assert result.ok is False
        assert "does not appear" in (result.error or "")
        assert (repo / "calc.py").read_text() == CALC

    def test_an_ambiguous_anchor_is_refused(self, repo: Path, gateway: ToolGateway) -> None:
        """'It edited the wrong one' is far worse than 'it asked again'."""
        (repo / "dup.py").write_text("x = 1\ny = 2\nx = 1\n", encoding="utf-8")
        result = _run(
            gateway, "file.patch", Phase.AUTHOR, path="dup.py", find="x = 1", replace="x = 9"
        )
        assert result.ok is False
        assert "appears 2 times" in (result.error or "")
        assert (repo / "dup.py").read_text() == "x = 1\ny = 2\nx = 1\n"

    def test_a_no_op_patch_does_not_claim_a_change(self, repo: Path, gateway: ToolGateway) -> None:
        """Otherwise a no-op could register FILE_CHANGED evidence."""
        result = _run(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            find="return a - b",
            replace="return a - b",
        )
        assert result.ok is False
        assert result.evidence == frozenset()

    def test_create_makes_a_new_file(self, repo: Path, gateway: ToolGateway) -> None:
        result = _run(
            gateway, "file.patch", Phase.AUTHOR, path="new.py", mode="create", content="X = 1\n"
        )
        assert result.ok is True
        assert (repo / "new.py").read_text() == "X = 1\n"

    def test_create_refuses_to_clobber(self, repo: Path, gateway: ToolGateway) -> None:
        result = _run(
            gateway, "file.patch", Phase.AUTHOR, path="calc.py", mode="create", content="X = 1\n"
        )
        assert result.ok is False
        assert "already exists" in (result.error or "")
        assert (repo / "calc.py").read_text() == CALC

    def test_whole_file_overwrite_needs_acknowledgement(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        """v1 defaulted to whole-file writes and lost work; this is opt-in."""
        with pytest.raises(ToolPolicyViolation, match="acknowledge_overwrite"):
            _run(
                gateway,
                "file.patch",
                Phase.AUTHOR,
                path="calc.py",
                mode="replace_file",
                content="wiped\n",
            )
        assert (repo / "calc.py").read_text() == CALC

    def test_acknowledged_overwrite_proceeds(self, repo: Path, gateway: ToolGateway) -> None:
        result = _run(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            mode="replace_file",
            content="wiped\n",
            acknowledge_overwrite=True,
        )
        assert result.ok is True
        assert (repo / "calc.py").read_text() == "wiped\n"

    def test_a_path_escape_is_refused(self, repo: Path, gateway: ToolGateway) -> None:
        result = _run(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="../outside.py",
            mode="create",
            content="x",
        )
        assert result.ok is False
        assert "escapes the workspace" in (result.error or "")
        assert not (repo.parent / "outside.py").exists()

    def test_patching_is_blocked_outside_author_and_repair(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        for phase in (Phase.INSPECT, Phase.PLAN, Phase.VERIFY):
            with pytest.raises(ToolPolicyViolation, match="not allowed in phase"):
                _run(
                    gateway,
                    "file.patch",
                    phase,
                    path="calc.py",
                    find="return a - b",
                    replace="return a + b",
                )
        assert (repo / "calc.py").read_text() == CALC


class TestRollback:
    def test_an_edit_can_be_undone(self, repo: Path, gateway: ToolGateway) -> None:
        """`reversible=True` is a claim the runtime relies on; it must be true."""
        patcher = gateway.get("file.patch")
        assert patcher is not None

        _run(
            gateway,
            "file.patch",
            Phase.AUTHOR,
            path="calc.py",
            find="return a - b",
            replace="return a + b",
        )
        assert "a + b" in (repo / "calc.py").read_text()

        patcher.rollback_last()  # type: ignore[attr-defined]
        assert (repo / "calc.py").read_text() == CALC

    def test_creating_a_file_undoes_to_absence(self, repo: Path, gateway: ToolGateway) -> None:
        """Restoring empty content where there was no file is not a rollback."""
        patcher = gateway.get("file.patch")
        assert patcher is not None

        _run(gateway, "file.patch", Phase.AUTHOR, path="new.py", mode="create", content="X\n")
        assert (repo / "new.py").exists()

        patcher.rollback_last()  # type: ignore[attr-defined]
        assert not (repo / "new.py").exists()

    def test_multiple_edits_unwind_in_reverse(self, repo: Path, gateway: ToolGateway) -> None:
        """Two patches to one file must unwind newest-first or work is lost."""
        patcher = gateway.get("file.patch")
        assert patcher is not None

        for old, new in [("return a - b", "return a + b"), ("return a + b", "return a * b")]:
            with gateway.decision():
                _run(gateway, "file.patch", Phase.AUTHOR, path="calc.py", find=old, replace=new)

        assert "a * b" in (repo / "calc.py").read_text()
        patcher.rollback_all()  # type: ignore[attr-defined]
        assert (repo / "calc.py").read_text() == CALC


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------


class TestGitTools:
    def test_inspect_reports_state_in_one_call(self, repo: Path, gateway: ToolGateway) -> None:
        """The model should not be picking among low-level git commands."""
        (repo / "calc.py").write_text(CALC.replace("a - b", "a + b"), encoding="utf-8")
        result = _run(gateway, "git.inspect", Phase.VERIFY)

        assert result.ok is True
        assert "Branch:" in result.output
        assert "calc.py" in result.output
        assert "Diff:" in result.output
        assert "Recent commits:" in result.output

    def test_inspect_reports_a_clean_tree(self, repo: Path, gateway: ToolGateway) -> None:
        result = _run(gateway, "git.inspect", Phase.VERIFY)
        assert "Working tree is clean." in result.output

    def test_checkpoint_commits_and_is_labelled(self, repo: Path, gateway: ToolGateway) -> None:
        (repo / "calc.py").write_text(CALC.replace("a - b", "a + b"), encoding="utf-8")
        result = _run(gateway, "git.checkpoint", Phase.AUTHOR, label="fixed add()")

        assert result.ok is True
        log = run_git(repo, "log", "--oneline", "-1")
        assert "shamsu-checkpoint: fixed add()" in log.stdout

    def test_checkpoint_on_a_clean_tree_produces_no_evidence(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        """Nothing was checkpointed, so CHECKPOINT_CREATED must not be claimed."""
        result = _run(gateway, "git.checkpoint", Phase.AUTHOR, label="nothing")
        assert result.ok is False
        assert result.evidence == frozenset()

    def test_a_checkpoint_can_be_rolled_back_to(self, repo: Path, gateway: ToolGateway) -> None:
        from shamsu.tools.git import rollback_to

        before = run_git(repo, "rev-parse", "HEAD").stdout.strip()
        (repo / "calc.py").write_text("broken\n", encoding="utf-8")
        _run(gateway, "git.checkpoint", Phase.AUTHOR, label="bad change")

        rollback_to(repo, before)
        assert (repo / "calc.py").read_text() == CALC

    def test_git_arguments_are_never_interpolated(self, repo: Path) -> None:
        """A hostile label is an argument, not a command fragment."""
        outcome = run_git(repo, "log", "--oneline", "-1; touch /tmp/pwned")
        assert not Path("/tmp/pwned").exists()
        assert outcome.ok is False


# ---------------------------------------------------------------------------
# test.run
# ---------------------------------------------------------------------------


class TestTestRunner:
    def test_a_failing_suite_is_reported_without_evidence(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        """add() is broken, so TESTS_PASSED must not be produced."""
        result = _run(gateway, "test.run", Phase.VERIFY, command="pytest")
        assert result.ok is False
        assert result.evidence == frozenset()
        assert "test_add" in result.output or "test_add" in (result.error or "")

    def test_a_passing_suite_produces_evidence(self, repo: Path, gateway: ToolGateway) -> None:
        (repo / "calc.py").write_text(CALC.replace("a - b", "a + b"), encoding="utf-8")
        result = _run(gateway, "test.run", Phase.VERIFY, command="pytest")
        assert result.ok is True, result.error
        assert EvidenceKind.TESTS_PASSED in result.evidence

    def test_an_unknown_command_key_is_refused(self, repo: Path, gateway: ToolGateway) -> None:
        """The model picks a key from an allowlist, never a command line."""
        result = _run(gateway, "test.run", Phase.VERIFY, command="rm -rf /")
        assert result.ok is False
        assert "unknown test command" in (result.error or "")

    def test_tests_cannot_run_during_author(self, repo: Path, gateway: ToolGateway) -> None:
        """Verifying mid-edit would let a step claim success before it is done."""
        with pytest.raises(ToolPolicyViolation, match="not allowed in phase author"):
            _run(gateway, "test.run", Phase.AUTHOR, command="pytest")

    def test_a_running_suite_is_cancellable(self, repo: Path, gateway: ToolGateway) -> None:
        """An abandoned pytest keeps writing to the workspace."""
        with pytest.raises(Cancelled):
            asyncio.run(
                gateway.invoke(
                    ToolRequest(tool="test.run", arguments={"command": "pytest"}),
                    Phase.VERIFY,
                    CancelAfter(checks=0),
                )
            )


# ---------------------------------------------------------------------------
# Evidence and the completion gate — Milestone 5's exit condition
# ---------------------------------------------------------------------------


@pytest.fixture
def recorder(tmp_path: Path) -> EvidenceRecorder:
    store = StateStore(tmp_path / "state.db")
    store.upsert_project(ProjectRecord(project_id=ProjectId("p1"), root="/w", name="demo"))
    store.create_task(
        TaskRecord(task_id=TaskId("t1"), project_id=ProjectId("p1"), request="fix add()")
    )
    store.create_run(
        RunRecord(run_id=RunId("r1"), project_id=ProjectId("p1"), task_id=TaskId("t1"))
    )
    return EvidenceRecorder(store, RunId("r1"), TaskId("t1"))


class TestEvidenceRecording:
    def test_a_successful_call_registers_its_evidence(
        self, repo: Path, gateway: ToolGateway, recorder: EvidenceRecorder
    ) -> None:
        request = ToolRequest(
            tool="file.patch",
            arguments={"path": "calc.py", "find": "return a - b", "replace": "return a + b"},
        )
        result = asyncio.run(gateway.invoke(request, Phase.AUTHOR, NullCancellationToken()))
        recorded = recorder.record(request, result, Phase.AUTHOR)

        assert EvidenceKind.FILE_CHANGED in recorded.evidence
        assert EvidenceKind.FILE_CHANGED in recorder.verified()

    def test_a_failed_call_is_logged_but_registers_nothing(
        self, repo: Path, gateway: ToolGateway, recorder: EvidenceRecorder
    ) -> None:
        """A ledger that only remembers successes cannot explain a failure —
        but a failure must not advance the gate."""
        request = ToolRequest(
            tool="file.patch",
            arguments={"path": "calc.py", "find": "not present", "replace": "x"},
        )
        result = asyncio.run(gateway.invoke(request, Phase.AUTHOR, NullCancellationToken()))
        recorded = recorder.record(request, result, Phase.AUTHOR)

        assert recorded.produced_evidence is False
        assert recorder.verified() == frozenset()
        assert recorded.event_id  # the event was still written


class TestCompletionGate:
    def test_the_gate_refuses_without_evidence(self) -> None:
        gate = check_completion([EvidenceKind.TESTS_PASSED], frozenset())
        assert gate.satisfied is False
        assert EvidenceKind.TESTS_PASSED in gate.missing

    def test_the_gate_names_what_is_missing(self) -> None:
        """'Missing 2 of 3' tells a user nothing they can act on."""
        gate = check_completion(
            [EvidenceKind.TESTS_PASSED, EvidenceKind.FILE_CHANGED],
            frozenset({EvidenceKind.FILE_CHANGED}),
        )
        assert "tests_passed" in gate.explain()
        assert "file_changed" not in gate.explain()

    def test_the_gate_opens_when_evidence_is_complete(self) -> None:
        gate = check_completion(
            [EvidenceKind.FILE_CHANGED],
            frozenset({EvidenceKind.FILE_CHANGED, EvidenceKind.TESTS_PASSED}),
        )
        assert gate.satisfied is True

    def test_no_requirement_means_no_gate(self) -> None:
        assert check_completion([], frozenset()).satisfied is True

    def test_the_full_edit_verify_cycle_gates_correctly(
        self, repo: Path, gateway: ToolGateway, recorder: EvidenceRecorder
    ) -> None:
        """Milestone 5's exit condition, end to end.

        A broken function is fixed, verified, and checkpointed — and the gate
        only opens once every required piece of evidence exists as a row keyed
        to a real tool execution.
        """
        required = [
            EvidenceKind.FILE_CHANGED,
            EvidenceKind.TESTS_PASSED,
            EvidenceKind.GIT_DIFF_REVIEWED,
            EvidenceKind.CHECKPOINT_CREATED,
        ]

        assert check_completion(required, recorder.verified()).satisfied is False

        steps = [
            (
                Phase.AUTHOR,
                ToolRequest(
                    tool="file.patch",
                    arguments={
                        "path": "calc.py",
                        "find": "return a - b",
                        "replace": "return a + b",
                    },
                ),
            ),
            (Phase.VERIFY, ToolRequest(tool="test.run", arguments={"command": "pytest"})),
            (Phase.VERIFY, ToolRequest(tool="git.inspect", arguments={})),
            (
                Phase.AUTHOR,
                ToolRequest(tool="git.checkpoint", arguments={"label": "fixed add()"}),
            ),
        ]

        for phase, request in steps:
            with gateway.decision():
                result = asyncio.run(gateway.invoke(request, phase, NullCancellationToken()))
            assert result.ok is True, f"{request.tool}: {result.error}"
            recorder.record(request, result, phase)

        gate = check_completion(required, recorder.verified())
        assert gate.satisfied is True, gate.explain()
        assert "return a + b" in (repo / "calc.py").read_text()
