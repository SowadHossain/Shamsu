"""Plan §32's chain, run end to end by the runtime.

    open repository → inspect → plan → patch → test → diff → evidence
    → checkpoint → verified report

Every component of this existed before `AgentSession`. Nothing composed them,
so the other integration tests call the pieces in the right order *themselves* —
which proves the pieces fit, not that the runtime can drive them. These tests
hand the runtime a request and a scripted model, and assert on what came out
the other end.

The model is fake because this box has no GPU. Everything else is real: a real
git repository, real pytest subprocesses, real SQLite.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.fake_model import FakeModelClient

from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.enums import AgentState, EvidenceKind, FactKind, FactOrigin
from shamsu.interfaces.ids import ProjectId
from shamsu.memory.store import MemoryStore
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession, SessionResult
from shamsu.state import ProjectRecord, StateStore, new_id
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git

pytestmark = pytest.mark.integration

BROKEN = '''"""Arithmetic."""


def add(a: int, b: int) -> int:
    return a - b
'''

TESTS = """from calc import add


def test_add() -> None:
    assert add(2, 3) == 5
"""


def _say(**payload: object) -> str:
    return json.dumps(payload)


def _tool(name: str, **arguments: object) -> str:
    return _say(action="call_tool", tool={"tool": name, "arguments": arguments})


def _conclude(text: str = "done") -> str:
    return _say(action="conclude", conclusion=text)


#: A model that inspects, plans one step, fixes the bug, verifies, and stops.
def _competent_script() -> list[str]:
    return [
        # INSPECT: read the file, then conclude.
        _tool("file.read", path="calc.py"),
        _conclude("add() subtracts instead of adding."),
        # CLASSIFY
        _say(kind="planned", reason="one file, but verify with tests"),
        # CREATE_PLAN
        _say(
            summary="Fix add() so it sums.",
            steps=[
                {
                    "title": "Correct add() in calc.py",
                    "intent": "add() subtracts; it must sum.",
                    "files": ["calc.py"],
                    "acceptance_criteria": ["add(2, 3) == 5"],
                    "required_evidence": ["targeted tests pass"],
                }
            ],
            grounded_in=["calc.py"],
        ),
        # EXECUTE: patch, test, inspect the diff, conclude.
        _tool("file.patch", path="calc.py", find="return a - b", replace="return a + b"),
        _tool("test.run", command="pytest"),
        _tool("git.inspect", what="diff"),
        _conclude("add() now sums and the tests pass."),
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / "calc.py").write_text(BROKEN, encoding="utf-8")
    (root / "test_calc.py").write_text(TESTS, encoding="utf-8")
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


def _session(
    store: StateStore,
    project: ProjectRecord,
    repo: Path,
    script: list[str],
    *,
    limits: ExecutionLimits | None = None,
    memory: MemoryStore | None = None,
) -> tuple[AgentSession, FakeModelClient, RunController]:
    model = FakeModelClient(script)
    runs = RunController(store)
    session = AgentSession(
        store=store,
        runs=runs,
        model=model,
        gateway=ToolGateway(authoring_tools(repo)),
        compiler=ContextCompiler(model),
        workspace=repo,
        project_id=project.project_id,
        limits=limits or ExecutionLimits(actions_per_step=6),
        memory=memory,
    )
    return session, model, runs


def _run(session: AgentSession, request: str) -> SessionResult:
    return asyncio.run(session.run(request))


class TestTheChainRuns:
    def test_a_task_runs_from_request_to_verified_report(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """Plan §32, end to end, driven by the runtime."""
        session, _, _ = _session(store, project, repo, _competent_script())

        result = _run(session, "fix add() so it sums")

        assert result.completed is True, result.render()
        assert result.final_state is AgentState.FINAL_REPORT
        assert (repo / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")

        assert result.report is not None
        assert result.report.verdict.satisfied is True
        assert result.report.changed_files == ("calc.py",)
        assert "COMPLETE" in result.report.render()

    def test_the_machine_visits_the_states_the_plan_specifies(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        session, _, _ = _session(store, project, repo, _competent_script())
        result = _run(session, "fix add()")

        assert result.transitions[:5] == (
            AgentState.RECEIVE_TASK,
            AgentState.LOAD_PROJECT_STATE,
            AgentState.INSPECT_PROJECT,
            AgentState.CLASSIFY_TASK,
            AgentState.CREATE_PLAN,
        )
        for state in (
            AgentState.VALIDATE_PLAN,
            AgentState.APPROVAL_CHECK,
            AgentState.EXECUTE_CURRENT_STEP,
            AgentState.VERIFY_CURRENT_STEP,
            AgentState.CREATE_CHECKPOINT,
            AgentState.CHECK_REMAINING_STEPS,
            AgentState.FINAL_VERIFICATION,
            AgentState.COMPLETION_GATE,
            AgentState.FINAL_REPORT,
        ):
            assert state in result.transitions, state

    def test_evidence_and_a_checkpoint_are_persisted(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        session, _, _ = _session(store, project, repo, _competent_script())
        result = _run(session, "fix add()")

        verified = store.verified_evidence(result.task_id)
        assert {
            EvidenceKind.FILE_CHANGED,
            EvidenceKind.TESTS_PASSED,
            EvidenceKind.GIT_DIFF_REVIEWED,
        } <= verified
        assert store.latest_checkpoint(result.task_id) is not None

    def test_the_run_is_recorded_as_completed(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        session, _, runs = _session(store, project, repo, _competent_script())
        result = _run(session, "fix add()")

        run = store.get_run(result.run_id)
        assert run is not None and run.is_terminal
        assert runs.active() == ()


class TestTheGateStillGoverns:
    def test_concluding_without_doing_the_work_does_not_complete(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The model says it is finished. Nothing was patched or tested."""
        script = [
            _conclude("nothing to investigate"),
            _say(kind="planned"),
            _say(
                summary="Fix add().",
                steps=[
                    {
                        "title": "Correct add()",
                        "files": ["calc.py"],
                        "required_evidence": ["tests pass"],
                    }
                ],
                grounded_in=[],
            ),
            _conclude("I have fixed it, all tests pass."),
        ]
        session, _, _ = _session(store, project, repo, script)

        result = _run(session, "fix add()")

        assert result.completed is False
        assert result.blocked is True
        assert (repo / "calc.py").read_text(encoding="utf-8") == BROKEN
        assert result.report is not None
        assert "NOT COMPLETE" in result.report.render()

    def test_a_step_cannot_use_a_tool_outside_its_allowlist(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """An `investigate` step has no mutating tools, and the runtime says so."""
        script = [
            _conclude("read enough"),
            _say(kind="planned"),
            _say(
                summary="Look first.",
                steps=[
                    {
                        "title": "Read the calculator",
                        "kind": "investigate",
                        "files": ["calc.py"],
                    }
                ],
                grounded_in=[],
            ),
            _tool("file.patch", path="calc.py", find="return a - b", replace="return a + b"),
            _conclude("looked"),
        ]
        session, _, _ = _session(store, project, repo, script)

        result = _run(session, "look at add()")

        # The step had no evidence to require, so it completes — but the patch
        # was refused and the file is untouched.
        assert (repo / "calc.py").read_text(encoding="utf-8") == BROKEN
        assert result.completed is True

    def test_an_unusable_plan_blocks_rather_than_improvising(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        script = [_conclude("read"), _say(kind="planned"), "not json at all"]
        session, _, _ = _session(store, project, repo, script)

        result = _run(session, "fix add()")
        assert result.blocked is True
        assert "did not produce a usable plan" in result.stopped_because


class TestBoundsAndControl:
    def test_a_cancelled_run_stops_and_says_so(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """Cancellation must not return something shaped like an answer."""
        session, _, runs = _session(store, project, repo, _competent_script())

        async def scenario() -> SessionResult:
            task = asyncio.ensure_future(session.run("fix add()"))
            await asyncio.sleep(0)
            for run_id in runs.active():
                runs.cancel(run_id, "user interrupt")
            return await task

        result = asyncio.run(scenario())

        assert result.cancelled is True
        assert result.completed is False
        assert "user interrupt" in result.stopped_because

    def test_the_action_budget_bounds_a_step(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """A model that never concludes does not run forever."""
        script = [
            _conclude("read"),
            _say(kind="planned"),
            _say(
                summary="Fix add().",
                steps=[{"title": "Correct add()", "files": ["calc.py"]}],
                grounded_in=[],
            ),
            *[_tool("file.read", path="calc.py") for _ in range(10)],
        ]
        session, model, _ = _session(
            store, project, repo, script, limits=ExecutionLimits(actions_per_step=2)
        )

        result = _run(session, "fix add()")

        assert result.completed is False
        assert model.calls <= 8, "the step loop kept asking past its budget"

    def test_an_unavailable_model_ends_the_run_honestly(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        model = FakeModelClient([], unavailable=True)
        session = AgentSession(
            store=store,
            runs=RunController(store),
            model=model,
            gateway=ToolGateway(authoring_tools(repo)),
            compiler=ContextCompiler(FakeModelClient([])),
            workspace=repo,
            project_id=project.project_id,
        )

        result = asyncio.run(session.run("fix add()"))
        assert result.completed is False
        assert result.blocked is True

    def test_a_session_can_run_a_second_task_without_leaking_state(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """Per-run context lives in `_Context`, not on the session."""
        session, _, _ = _session(store, project, repo, _competent_script())
        first = _run(session, "fix add()")

        session.model = FakeModelClient([_conclude("read"), _say(kind="planned"), "not json"])
        second = _run(session, "do something else")

        assert first.completed is True
        assert second.completed is False
        assert second.task_id != first.task_id
        assert second.transitions[0] is AgentState.RECEIVE_TASK


class TestMemoryAcrossRuns:
    def test_a_lesson_from_one_run_is_available_to_the_next(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The end-to-end form of Milestone 9's exit condition."""
        memory = MemoryStore(store, project.project_id)
        session, _, _ = _session(store, project, repo, _competent_script(), memory=memory)
        _run(session, "fix add()")

        # A clean run records no lesson, so this asserts the other half: a fact
        # learned during one run reaches the next run's execution frame.
        memory.learn(FactKind.STACK, "runner", "pytest -q", origin=FactOrigin.USER)

        second, model, _ = _session(
            store,
            project,
            repo,
            [
                _conclude("already fixed"),
                _say(kind="planned"),
                _say(
                    summary="Confirm the fix holds.",
                    steps=[{"title": "Re-read calc.py", "kind": "investigate"}],
                    grounded_in=[],
                ),
                _conclude("confirmed"),
            ],
            memory=memory,
        )
        _run(second, "confirm add() is correct")

        prompts = [message.content for request in model.requests for message in request.messages]
        assert any("pytest -q" in prompt for prompt in prompts), (
            "a recalled project fact never reached a frame"
        )
