"""Refusing a repeated call must not become a lifetime ban on a tool.

Two changes made separately combined into a deadlock. `context.attempted`
survives a repair so the model can see what it already did; a repeated call is
refused outright. Enforcing the refusal against that surviving history meant a
tool called once could never be called again for the rest of the step — and
`project.inspect` takes no arguments, so its signature never varies.

Observed live: "can you plan on how to build this project?" called
`project.inspect` once, was refused on every later call *and through both
repair attempts*, and blocked having done nothing.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.fake_model import FakeModelClient

from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.ids import ProjectId
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession, SessionResult
from shamsu.state import ProjectRecord, StateStore, new_id
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git

pytestmark = pytest.mark.integration


def _say(**payload: object) -> str:
    return json.dumps(payload)


def _tool(name: str, **arguments: object) -> str:
    return _say(action="call_tool", tool={"tool": name, "arguments": arguments})


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    run_git(root, "config", "user.email", "a@b.c")
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


PLAN = _say(
    summary="Fix the adder.",
    steps=[
        {
            "title": "Fix the add function",
            "kind": "change",
            "files": ["calc.py"],
            "required_evidence": [],
        }
    ],
    grounded_in=[],
)


class TestARepairMayReuseATool:
    def test_a_tool_called_in_the_first_attempt_works_again_after_repair(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The deadlock: `project.inspect` banned for the rest of the step."""
        result = _run(
            store,
            project,
            repo,
            [
                _say(action="conclude", conclusion="add subtracts"),
                _say(kind="planned", reason="one file"),
                PLAN,
                # Attempt one, four actions: look, then insist it is finished
                # until the premature-conclusion guard sends it to verify.
                _tool("project.inspect"),
                *[_say(action="conclude", conclusion="done") for _ in range(3)],
                # The repair. Re-reading the project first is legitimate, and
                # used to be refused outright — which left nothing else to do.
                _tool("project.inspect"),
                _tool(
                    "file.patch",
                    path="calc.py",
                    mode="replace_text",
                    find="return a - b",
                    replace="return a + b",
                ),
                _tool("git.inspect"),
                _say(action="conclude", conclusion="fixed and reviewed"),
            ],
        )

        assert result.completed is True, result.render()
        assert (repo / "calc.py").read_text(encoding="utf-8").endswith("return a + b\n")

    def test_a_repeat_within_one_attempt_is_still_refused(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The loop breaker still breaks loops — that is what it is for."""
        result = _run(
            store,
            project,
            repo,
            [
                _say(action="conclude", conclusion="add subtracts"),
                _say(kind="planned", reason="one file"),
                PLAN,
                *[_tool("project.inspect") for _ in range(10)],
            ],
        )

        events = store.tool_events_for(result.task_id)
        inspections = [event for event in events if event.tool == "project.inspect"]

        # Counted on `ok`, because refusals are now recorded too. The property
        # here has always been about *execution* — the loop breaker stops the
        # tool running again — and the ledger previously dropped the refused
        # calls entirely, so "rows" and "executions" happened to be the same
        # number. Making refusals visible separated them.
        assert sum(1 for event in inspections if event.ok) <= 2, (
            "an identical call ran more than once per attempt"
        )
        assert any(not event.ok for event in inspections), (
            "the refused repeats must appear in the ledger, or a step that "
            "looped on one call is indistinguishable from one that did nothing"
        )
