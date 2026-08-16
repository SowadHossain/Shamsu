"""A refused plan is asked for again — with the refusal attached.

`context.replan_reason` was set only by the REPLAN *state*, so a plan rejected
at validation was re-requested from an identical prompt. At temperature 0 that
is a guaranteed identical plan: a §31.1 task proposed an investigation-only
plan, was refused, proposed it again, was refused again, and blocked having
persisted no plan and run no tool at all — 15 seconds and zero work.

Also covers the other way a run started with nothing to do: a repair whose
scope was empty because the step's only patch had failed, even though that
failed patch named the file the task was about.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.fake_model import FakeModelClient

from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.enums import AgentState
from shamsu.interfaces.ids import ProjectId
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession, SessionResult
from shamsu.state import ProjectRecord, StateStore, new_id
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git

pytestmark = pytest.mark.integration

README = "# widget\n\nA small widget library.\n"


def _say(**payload: object) -> str:
    return json.dumps(payload)


def _tool(name: str, **arguments: object) -> str:
    return _say(action="call_tool", tool={"tool": name, "arguments": arguments})


def _plan(*titles: str, files: list[str] | None = None) -> str:
    return _say(
        summary="A plan.",
        steps=[
            {"title": title, "kind": "change", "files": files or [], "required_evidence": []}
            for title in titles
        ],
        grounded_in=[],
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text(README, encoding="utf-8")
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


def _session(
    store: StateStore, project: ProjectRecord, repo: Path, script: list[str]
) -> tuple[AgentSession, FakeModelClient]:
    model = FakeModelClient(script)
    return (
        AgentSession(
            store=store,
            runs=RunController(store),
            model=model,
            gateway=ToolGateway(authoring_tools(repo), require_read_before_edit=False),
            compiler=ContextCompiler(model),
            workspace=repo,
            project_id=project.project_id,
            limits=ExecutionLimits(actions_per_step=4),
        ),
        model,
    )


def _run(
    session: AgentSession, request: str = "add an Installation section to README.md"
) -> SessionResult:
    return asyncio.run(session.run(request))


#: The plan `validate_plan` refuses: every step is investigation, so it cannot
#: carry out a change request. Named rather than indexed out of `PREFIX` — it is
#: re-sent to test repeated refusal, and a positional reference silently became
#: the classification when a step was added ahead of it.
INVESTIGATION_ONLY = _plan("Inspect the project structure", "Review the README")

PREFIX = [
    # The request names README.md, so the investigation is required to read it
    # before concluding — a plan for a file nobody opened is a guess. Scripting
    # the read here rather than letting the nudge fire keeps these tests about
    # plan rejection instead of about investigation.
    _tool("file.read", path="README.md"),
    _say(action="conclude", conclusion="a README and a module"),
    _say(kind="planned", reason="needs care"),
    INVESTIGATION_ONLY,
]


class TestTheRefusalReachesTheModel:
    def test_the_second_request_says_why_the_first_was_refused(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """Otherwise the retry is a rerun and produces the same plan."""
        session, model = _session(
            store,
            project,
            repo,
            [
                *PREFIX,
                _plan("Add an Installation section", files=["README.md"]),
                _tool(
                    "file.patch",
                    path="README.md",
                    mode="replace_text",
                    find="A small widget library.",
                    replace="A small widget library.\n\n## Installation\n\npip install .",
                ),
                _tool("git.inspect", subcommand="diff"),
                _say(action="conclude", conclusion="added"),
            ],
        )
        _run(session)

        prompts = [message.content for request in model.requests for message in request.messages]
        assert any("only of investigation" in prompt for prompt in prompts), (
            "the model was never told why its plan was refused"
        )

    def test_a_corrected_plan_is_accepted_and_runs(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The whole point: a refusal should cost one plan, not the run."""
        session, _ = _session(
            store,
            project,
            repo,
            [
                *PREFIX,
                _plan("Add an Installation section", files=["README.md"]),
                _tool(
                    "file.patch",
                    path="README.md",
                    mode="replace_text",
                    find="A small widget library.",
                    replace="A small widget library.\n\n## Installation\n\npip install .",
                ),
                _tool("git.inspect", subcommand="diff"),
                _say(action="conclude", conclusion="added"),
            ],
        )
        result = _run(session)

        assert result.completed is True, result.render()
        assert "## Installation" in (repo / "README.md").read_text(encoding="utf-8")

    def test_repeated_refusal_still_blocks(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """Telling the model why must not become an unbounded retry loop."""
        session, _ = _session(
            store,
            project,
            repo,
            [*PREFIX, INVESTIGATION_ONLY, INVESTIGATION_ONLY, INVESTIGATION_ONLY],
        )
        result = _run(session)

        assert result.completed is False
        assert result.final_state is AgentState.BLOCKED
        assert "rejected repeatedly" in result.stopped_because


class TestAFailedPatchStillNamesItsFile:
    def test_repair_can_edit_the_file_the_patch_aimed_at(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """A mis-anchored patch is the clearest statement of intent available.

        Without it, `changed_files` stays empty, and a plan that named no files
        left repair refusing with "the failure implicates no editable file" —
        about the very file the task asked it to edit.
        """
        session, _ = _session(
            store,
            project,
            repo,
            [
                # README.md is named in the request, so it is read first.
                _tool("file.read", path="README.md"),
                _say(action="conclude", conclusion="a README"),
                _say(kind="planned", reason="one edit"),
                # The step names its file, as validation now requires — but
                # the first patch mis-anchors, so `changed_files` stays empty
                # and the repair scope has to come from the failed attempt.
                _plan("Add an Installation section to README.md", files=["README.md"]),
                # The anchor does not exist, so this fails.
                _tool(
                    "file.patch", path="README.md", mode="replace_text", find="NOPE", replace="x"
                ),
                _say(action="conclude", conclusion="done"),
                # The retry, now that repair is reachable and in scope.
                _tool(
                    "file.patch",
                    path="README.md",
                    mode="replace_text",
                    find="A small widget library.",
                    replace="A small widget library.\n\n## Installation\n\npip install .",
                ),
                _tool("git.inspect", subcommand="diff"),
                _say(action="conclude", conclusion="added"),
            ],
        )
        result = _run(session)

        assert "implicates no editable file" not in result.stopped_because
        assert "## Installation" in (repo / "README.md").read_text(encoding="utf-8")
