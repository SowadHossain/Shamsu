"""A step that did the work but not the proof gets another attempt.

The scenario is taken verbatim from a §31.1 evaluation run: the agent fixed
`add()`, registered `file_changed`, and concluded without calling `git.inspect`
— so the gate refused on `git_diff_reviewed` alone. Before this, that ended the
run: `REPAIRABLE` required `context.last_digest`, which only `test.run` sets.

What is asserted here is the recovery, not the model: the same first three
responses in both tests, and only what comes after the refusal differs.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.fake_model import FakeModelClient

from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.enums import AgentState, EvidenceKind, FailureKind
from shamsu.interfaces.ids import ProjectId
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession, SessionResult
from shamsu.state import ProjectRecord, StateStore, new_id
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git

pytestmark = pytest.mark.integration

BROKEN = '"""Arithmetic."""\n\n\ndef add(a: int, b: int) -> int:\n    return a - b\n'


def _say(**payload: object) -> str:
    return json.dumps(payload)


def _tool(name: str, **arguments: object) -> str:
    return _say(action="call_tool", tool={"tool": name, "arguments": arguments})


def _plan() -> str:
    return _say(
        summary="Fix the adder.",
        steps=[
            {
                "title": "Fix the add function",
                "intent": "make it sum",
                "kind": "change",
                "files": ["calc.py"],
                "acceptance_criteria": ["add(2, 3) == 5"],
                "required_evidence": [],
                "risk": "low",
            }
        ],
        grounded_in=[],
    )


#: Investigate, classify, plan — then patch and conclude *without* the diff
#: review. This is what the live model did.
PREFIX = [
    _say(action="conclude", conclusion="add() subtracts"),
    # `planned`, not `direct`: DIRECT builds its plan without a model call, so
    # the plan below would be consumed as the step's first decision instead.
    _say(kind="planned", reason="one file, but plan it explicitly"),
    _plan(),
    _tool("file.patch", path="calc.py", find="return a - b", replace="return a + b"),
    _say(action="conclude", conclusion="fixed it"),
]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text(BROKEN, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    run_git(root, "config", "user.email", "eval@shamsu.local")
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


def _run(store: StateStore, project: ProjectRecord, repo: Path, script: list[str]) -> SessionResult:
    model = FakeModelClient(script)
    session = AgentSession(
        store=store,
        runs=RunController(store),
        model=model,
        gateway=ToolGateway(authoring_tools(repo), require_read_before_edit=False),
        compiler=ContextCompiler(model),
        workspace=repo,
        project_id=project.project_id,
        limits=ExecutionLimits(actions_per_step=4, repair_attempts_per_step=2),
    )
    return asyncio.run(session.run("fix add() so it sums"))


#: A model that will not do anything but insist it is finished. Enough of them
#: to exhaust the in-loop refusals *and* the repair attempts — a premature
#: conclusion is now sent back within the step before the step is ever failed,
#: so reaching REPAIR takes more than one.
INSISTS = [_say(action="conclude", conclusion="done") for _ in range(12)]


class TestTheStepGetsAnotherAttempt:
    def test_the_run_reaches_repair(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """Previously impossible: only a failing test could reach REPAIR."""
        result = _run(store, project, repo, [*PREFIX, *INSISTS])
        assert AgentState.REPAIR in result.transitions

    def test_the_shortfall_is_recorded_as_incomplete_evidence(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        result = _run(store, project, repo, [*PREFIX, *INSISTS])
        failures = store.failures_for(result.task_id)

        # More than one: the retry concluded without the diff again, which is
        # recorded and is what the stuck detector then acts on.
        assert failures
        assert {failure.kind for failure in failures} == {FailureKind.INCOMPLETE_EVIDENCE}
        assert all("git_diff_reviewed" in failure.expected for failure in failures)

    def test_a_second_attempt_that_reviews_the_diff_completes(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The whole point: one more call was all the run needed."""
        result = _run(
            store,
            project,
            repo,
            [
                *PREFIX,
                _tool("git.inspect", subcommand="diff"),
                _say(action="conclude", conclusion="reviewed"),
            ],
        )

        assert result.completed is True, result.render()
        assert (repo / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")

        verified = store.verified_evidence(result.task_id)
        assert EvidenceKind.FILE_CHANGED in verified
        assert EvidenceKind.GIT_DIFF_REVIEWED in verified

    def test_the_repair_prompt_names_the_missing_evidence(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """A retry that does not say what is outstanding is just a rerun."""
        model = FakeModelClient(
            [
                *PREFIX,
                _tool("git.inspect", subcommand="diff"),
                _say(action="conclude", conclusion="ok"),
            ]
        )
        session = AgentSession(
            store=store,
            runs=RunController(store),
            model=model,
            gateway=ToolGateway(authoring_tools(repo), require_read_before_edit=False),
            compiler=ContextCompiler(model),
            workspace=repo,
            project_id=project.project_id,
            limits=ExecutionLimits(actions_per_step=4),
        )
        asyncio.run(session.run("fix add() so it sums"))

        prompts = [message.content for request in model.requests for message in request.messages]
        assert any("git_diff_reviewed" in prompt and "git.inspect" in prompt for prompt in prompts)


class TestTheRetryRemembersTheFirstAttempt:
    def test_the_history_survives_a_repair(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """A retry that forgets is where the loop restarts.

        Observed live: the agent patched the file, was sent back for a missing
        diff review, and spent the whole retry re-running the same
        `code.search` — because `attempted` was rebuilt empty on every entry to
        the step.
        """
        model = FakeModelClient(
            [
                *PREFIX,
                _tool("git.inspect", subcommand="diff"),
                _say(action="conclude", conclusion="reviewed"),
            ]
        )
        session = AgentSession(
            store=store,
            runs=RunController(store),
            model=model,
            gateway=ToolGateway(authoring_tools(repo), require_read_before_edit=False),
            compiler=ContextCompiler(model),
            workspace=repo,
            project_id=project.project_id,
            limits=ExecutionLimits(actions_per_step=4),
        )
        asyncio.run(session.run("fix add() so it sums"))

        # The last execute frame must still name the patch from attempt one.
        prompts = [message.content for request in model.requests for message in request.messages]
        # Section names are upper-cased by `ContextFrame.render`.
        retries = [prompt for prompt in prompts if "ALREADY TRIED IN THIS STEP" in prompt]
        assert retries, "no frame carried an attempt history"
        assert any("file.patch" in prompt for prompt in retries[-2:])


class TestItStillStopsWhenItShould:
    def test_an_unrepeatable_gap_does_not_loop_forever(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """Concluding without the diff every time must terminate, not spin."""
        result = _run(store, project, repo, [*PREFIX, *INSISTS])

        assert result.completed is False
        assert result.final_state in (AgentState.BLOCKED, AgentState.STOPPED)
