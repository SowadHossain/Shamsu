"""An unreachable model is reported as one, not as a bad answer.

`_request_plan` used to catch `ModelUnavailable` alongside
`ModelContractError` and return `None`, which the runtime rendered as *"the
model did not produce a usable plan"*. True of a dead server, and useless: it
points the user at their request when the answer is `ollama serve`, or
`ollama pull` for a model that was never fetched.

That second case is the likely first experience of any new default model, which
is what made this worth fixing rather than noting.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from shamsu.context.compiler import ContextCompiler
from shamsu.interfaces.cancellation import CancellationToken
from shamsu.interfaces.enums import AgentState
from shamsu.interfaces.ids import ProjectId
from shamsu.interfaces.models import ModelRequest, ModelResponse, ModelUnavailable
from shamsu.runtime.controller import RunController
from shamsu.runtime.limits import ExecutionLimits
from shamsu.runtime.session import AgentSession, SessionResult
from shamsu.state import ProjectRecord, StateStore, new_id
from shamsu.tools import ToolGateway, authoring_tools
from shamsu.tools.git import run_git

pytestmark = pytest.mark.integration

#: What `OllamaClient` says when nothing answers on the port.
DOWN = "could not reach Ollama at http://localhost:11434: All connection attempts failed"

#: What it says when the server is up but the model was never pulled — the
#: case a changed default makes likely.
NOT_PULLED = (
    "Ollama has no model 'qwen2.5-coder:7b-instruct-q4_K_M'. "
    "Available: gemma3:4b. Pull it with: ollama pull qwen2.5-coder:7b-instruct-q4_K_M"
)


class _Unreachable:
    """Raises `ModelUnavailable` with a caller-chosen message.

    A local stub rather than `FakeModelClient(unavailable=True)`, whose message
    is fixed — and the message is the thing under test: an operator's next
    action is in it.
    """

    context_tokens = 8192

    def __init__(self, message: str) -> None:
        self._message = message

    @property
    def name(self) -> str:
        return "unreachable"

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    async def generate(self, request: ModelRequest, cancel: CancellationToken) -> ModelResponse:
        raise ModelUnavailable(self._message)

    async def generate_typed(
        self, request: ModelRequest, contract: object, cancel: CancellationToken
    ) -> object:
        raise ModelUnavailable(self._message)


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


def _run(store: StateStore, project: ProjectRecord, repo: Path, message: str) -> SessionResult:
    model = _Unreachable(message)
    session = AgentSession(
        store=store,
        runs=RunController(store),
        model=model,
        gateway=ToolGateway(authoring_tools(repo)),
        compiler=ContextCompiler(model),
        workspace=repo,
        project_id=project.project_id,
        limits=ExecutionLimits(actions_per_step=4),
    )
    return asyncio.run(session.run("fix add() so it sums"))


class TestAnUnreachableServerSaysSo:
    def test_the_reason_names_the_server(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        result = _run(store, project, repo, DOWN)
        assert result.final_state is AgentState.BLOCKED
        assert "could not reach Ollama" in result.stopped_because

    def test_it_does_not_blame_the_plan(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The old message sent users to their prompt instead of their server."""
        result = _run(store, project, repo, DOWN)
        assert "did not produce a usable plan" not in result.stopped_because

    def test_a_missing_model_keeps_its_pull_instruction(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """The likely first experience of a changed default."""
        result = _run(store, project, repo, NOT_PULLED)
        assert "ollama pull" in result.stopped_because

    def test_it_stops_at_inspection_rather_than_trying_three_phases(
        self, store: StateStore, project: ProjectRecord, repo: Path
    ) -> None:
        """Each later phase would report a different symptom of one cause."""
        result = _run(store, project, repo, DOWN)
        assert AgentState.CLASSIFY_TASK not in result.transitions
        assert AgentState.CREATE_PLAN not in result.transitions
