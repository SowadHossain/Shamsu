"""A step cannot earn the change floor off another step's file.

Reproduces the run that made this necessary. Asked to build the system in
`OpenBazaar_Marketplace_PRD.docx`, SHAMSU reported:

    COMPLETE — All 4 step(s) passed with verified evidence.
      [✓] 1. Design System Architecture
      [✓] 2. Develop Frontend
      [✓] 3. Develop Backend
      [✓] 4. Integrate Frontend and Backend

and produced **one 34-line markdown file** containing
`[Insert flowchart image here]`. Steps 2, 3 and 4 each earned `file_changed`
and `git_diff_reviewed` by appending a bullet list to the document step 1 had
created. `file_changed` meant "some bytes moved somewhere", and that is
satisfiable without doing any of the work.

Two rules close it, and both are deterministic:

* A step earns `FILE_CHANGED` for a file it **declared**, or for one **no
  earlier step has claimed**. Undeclared-and-already-claimed earns nothing.
* The runtime's diff review is a review of *this step's* change, so a step that
  owns no change gets no `GIT_DIFF_REVIEWED` either.

The write still happens and the event is still recorded. What is withheld is
credit — the ledger should say what the agent did, and the gate should not
mistake it for the work.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.fake_model import FakeModelClient

from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.enums import EvidenceKind
from shamsu.interfaces.ids import ProjectId
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession, SessionResult
from shamsu.state import ProjectRecord, StateStore, new_id
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git
from shamsu.ui.approval import AlwaysApprover

pytestmark = pytest.mark.integration


def _say(**payload: object) -> str:
    return json.dumps(payload)


def _tool(name: str, **arguments: object) -> str:
    return _say(action="call_tool", tool={"tool": name, "arguments": arguments})


#: Four steps that each name a *different* real target — an honest-looking plan.
#: The model then ignores all four and writes one document instead, which is
#: exactly the shape the PRD build produced.
FOUR_STEPS = _say(
    summary="Build the marketplace.",
    steps=[
        {"title": "Design System Architecture", "kind": "change", "files": ["design.py"]},
        {"title": "Develop Frontend", "kind": "change", "files": ["frontend.py"]},
        {"title": "Develop Backend", "kind": "change", "files": ["backend.py"]},
        {"title": "Integrate Frontend and Backend", "kind": "change", "files": ["wiring.py"]},
    ],
    grounded_in=[],
)


def _script() -> list[str]:
    """Investigate, classify, plan — then write one document, four times."""
    return [
        _say(action="conclude", conclusion="read the spec"),
        _say(kind="planned", reason="several steps"),
        FOUR_STEPS,
        # Step 1 creates it. Legitimate: nothing has claimed it.
        _tool("file.patch", path="architecture_design.md", mode="create", content="# Design\n"),
        _say(action="conclude", conclusion="designed"),
        # Steps 2-4 append to the same file, which none of them declared.
        *[
            response
            for _ in range(3)
            for response in (
                _tool(
                    "file.patch",
                    path="architecture_design.md",
                    mode="append",
                    content="\n- a bullet point\n",
                ),
                _say(action="conclude", conclusion="done"),
            )
        ],
        # Slack, so the run ends on its own logic rather than on an exhausted
        # script — the script running out would prove nothing.
        *[_say(action="conclude", conclusion="done") for _ in range(30)],
    ]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "build"
    root.mkdir()
    (root / "SPEC.md").write_text("# Spec\nBuild a marketplace.\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    run_git(root, "config", "user.email", "t@example.com")
    run_git(root, "config", "user.name", "T")
    run_git(root, "add", "-A")
    run_git(root, "commit", "-m", "initial", "--no-verify")
    return root


def _run(repo: Path) -> tuple[SessionResult, StateStore]:
    store = StateStore(":memory:")
    project = store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root=str(repo), name="build")
    )
    model = FakeModelClient(_script())
    session = AgentSession(
        store=store,
        runs=RunController(store),
        model=model,
        gateway=ToolGateway(authoring_tools(repo), workspace=repo, require_read_before_edit=False),
        compiler=ContextCompiler(model),
        workspace=repo,
        project_id=project.project_id,
        limits=ExecutionLimits(actions_per_step=4, repair_attempts_per_step=1),
        approver=AlwaysApprover(),
    )
    return asyncio.run(session.run("Build the marketplace")), store


class TestOneFileCannotFinishFourSteps:
    def test_the_task_is_not_reported_complete(self, repo: Path) -> None:
        """The headline result: this exact shape used to say COMPLETE."""
        result, _store = _run(repo)
        assert result.completed is False, result.render()

    def test_only_the_step_that_created_the_file_earns_the_change(self, repo: Path) -> None:
        result, store = _run(repo)
        assert result.report is not None

        earned = [
            step
            for step in result.report.verdict.steps
            if EvidenceKind.FILE_CHANGED in step.verified
        ]
        assert len(earned) == 1, (
            "only the step that first wrote the file may be credited for it; "
            f"credited: {[step.title for step in earned]}"
        )
        assert earned[0].ordinal == 0

    def test_the_later_steps_report_what_is_missing(self, repo: Path) -> None:
        result, _store = _run(repo)
        assert result.report is not None

        later = [step for step in result.report.verdict.steps if step.ordinal > 0]
        assert later, "the plan should still have four steps"
        for step in later:
            assert EvidenceKind.FILE_CHANGED in step.missing, (
                f"step {step.ordinal + 1} should be missing file_changed"
            )

    def test_the_write_still_happened_and_is_recorded(self, repo: Path) -> None:
        """Credit is withheld; history is not rewritten.

        The ledger has to say what the agent did, or a failure cannot be
        explained afterwards.
        """
        result, store = _run(repo)
        patches = [
            event
            for event in store.tool_events_for(result.task_id)
            if event.tool == "file.patch" and event.ok
        ]
        assert len(patches) >= 2, "the appends should still be recorded as executed"
        assert (repo / "architecture_design.md").exists()


class TestADeclaredFileIsStillFine:
    def test_a_step_that_declares_the_file_keeps_its_credit(self, repo: Path) -> None:
        """The rule must not punish a plan that says what it will touch."""
        store = StateStore(":memory:")
        project = store.upsert_project(
            ProjectRecord(project_id=ProjectId(new_id()), root=str(repo), name="build")
        )
        plan = _say(
            summary="Two steps, both honest about the file.",
            steps=[
                {"title": "Create the module", "kind": "change", "files": ["app.py"]},
                {"title": "Extend the module", "kind": "change", "files": ["app.py"]},
            ],
            grounded_in=[],
        )
        model = FakeModelClient(
            [
                _say(action="conclude", conclusion="looked"),
                _say(kind="planned", reason="two steps"),
                plan,
                _tool("file.patch", path="app.py", mode="create", content="x = 1\n"),
                _say(action="conclude", conclusion="created"),
                _tool("file.patch", path="app.py", mode="append", content="y = 2\n"),
                _say(action="conclude", conclusion="extended"),
                *[_say(action="conclude", conclusion="done") for _ in range(20)],
            ]
        )
        session = AgentSession(
            store=store,
            runs=RunController(store),
            model=model,
            gateway=ToolGateway(
                authoring_tools(repo), workspace=repo, require_read_before_edit=False
            ),
            compiler=ContextCompiler(model),
            workspace=repo,
            project_id=project.project_id,
            limits=ExecutionLimits(actions_per_step=4),
            approver=AlwaysApprover(),
        )
        result = asyncio.run(session.run("build app.py"))

        assert result.report is not None
        # `coalesce_by_file` merges these two adjacent same-file steps into one,
        # which is the right answer and the reason the rule is safe: a plan that
        # names its file honestly never reaches the ownership check.
        earned = [
            step
            for step in result.report.verdict.steps
            if EvidenceKind.FILE_CHANGED in step.verified
        ]
        assert earned, "a step that declared its file must keep its credit"
