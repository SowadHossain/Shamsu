"""Milestone 4's exit condition: grounded plans without modifying files.

The first place the whole stack runs together — sandbox, gateway, tools,
compiler, contracts, limits, cancellation — against a real repository on disk
and a scripted model.

No live inference. The scripted model is what makes these assertions possible:
every decision is chosen by the test, so what is being verified is the
*runtime's* behaviour given a decision, not the model's ability to produce a
good one. Whether a small local model can actually emit these shapes is a
separate question that needs a GPU machine.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from tests.fixtures.fake_model import CancelAfter, FakeModelClient

from shamsu.agent import ReadOnlyAgent, is_grounded
from shamsu.context import ContextCompiler
from shamsu.interfaces.cancellation import Cancelled
from shamsu.interfaces.enums import Phase
from shamsu.models.contracts import ImplementationPlan
from shamsu.runtime.limits import ExecutionLimits
from shamsu.tools import ToolGateway, read_only_tools, summarise_manifest

pytestmark = pytest.mark.integration

AUTH = '''"""Authentication."""


def login(user: str, password: str) -> bool:
    """Check credentials."""
    return bool(user and password)
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "src" / "auth.py").write_text(AUTH, encoding="utf-8")
    (root / "src" / "__init__.py").write_text('"""App."""\n', encoding="utf-8")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\nname = "demo"\ndependencies = []\n\n'
        '[project.optional-dependencies]\ndev = ["pytest>=8"]\n\n'
        "[build-system]\nrequires = []\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


@pytest.fixture
def gateway(repo: Path) -> ToolGateway:
    return ToolGateway(read_only_tools(repo))


def _agent(repo: Path, gateway: ToolGateway, responses: list[str]) -> ReadOnlyAgent:
    model = FakeModelClient(responses)
    return ReadOnlyAgent(model, gateway, ContextCompiler(model))


def _call(tool: str, **arguments: object) -> str:
    return json.dumps({"action": "call_tool", "tool": {"tool": tool, "arguments": arguments}})


def _conclude(text: str) -> str:
    return json.dumps({"action": "conclude", "conclusion": text})


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------


class TestReadOnlyTools:
    def test_project_inspect_reports_deterministic_facts(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        import asyncio

        from shamsu.interfaces.cancellation import NullCancellationToken
        from shamsu.interfaces.tools import ToolRequest

        result = asyncio.run(
            gateway.invoke(
                ToolRequest(tool="project.inspect", arguments={}),
                Phase.INSPECT,
                NullCancellationToken(),
            )
        )
        assert result.ok
        payload = json.loads(result.output)
        assert payload["languages"] == ["Python"]
        assert "pytest" in payload["test_frameworks"]

    def test_code_search_reports_file_and_line(self, repo: Path, gateway: ToolGateway) -> None:
        import asyncio

        from shamsu.interfaces.cancellation import NullCancellationToken
        from shamsu.interfaces.tools import ToolRequest

        result = asyncio.run(
            gateway.invoke(
                ToolRequest(tool="code.search", arguments={"query": "def login"}),
                Phase.INSPECT,
                NullCancellationToken(),
            )
        )
        assert result.ok
        assert "src/auth.py:4" in result.output

    def test_code_search_says_so_when_nothing_matches(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        """'Searched, found nothing' must not look like 'the tool returned nothing'."""
        import asyncio

        from shamsu.interfaces.cancellation import NullCancellationToken
        from shamsu.interfaces.tools import ToolRequest

        result = asyncio.run(
            gateway.invoke(
                ToolRequest(tool="code.search", arguments={"query": "zzz_not_here"}),
                Phase.INSPECT,
                NullCancellationToken(),
            )
        )
        assert result.ok
        assert "No matches" in result.output

    def test_file_read_is_line_numbered(self, repo: Path, gateway: ToolGateway) -> None:
        """Everything downstream refers to code by line."""
        import asyncio

        from shamsu.interfaces.cancellation import NullCancellationToken
        from shamsu.interfaces.tools import ToolRequest

        result = asyncio.run(
            gateway.invoke(
                ToolRequest(tool="file.read", arguments={"path": "src/auth.py"}),
                Phase.INSPECT,
                NullCancellationToken(),
            )
        )
        assert result.ok
        assert "4  def login" in result.output
        assert "lines 1–6 of 6" in result.output

    def test_file_read_honours_a_range(self, repo: Path, gateway: ToolGateway) -> None:
        import asyncio

        from shamsu.interfaces.cancellation import NullCancellationToken
        from shamsu.interfaces.tools import ToolRequest

        result = asyncio.run(
            gateway.invoke(
                ToolRequest(
                    tool="file.read",
                    arguments={"path": "src/auth.py", "start_line": 4, "end_line": 5},
                ),
                Phase.INSPECT,
                NullCancellationToken(),
            )
        )
        assert result.ok
        assert "def login" in result.output
        assert "Authentication" not in result.output

    def test_a_path_escape_is_refused_by_the_tool(self, repo: Path, gateway: ToolGateway) -> None:
        """The sandbox is wired in, not merely available."""
        import asyncio

        from shamsu.interfaces.cancellation import NullCancellationToken
        from shamsu.interfaces.tools import ToolRequest

        result = asyncio.run(
            gateway.invoke(
                ToolRequest(tool="file.read", arguments={"path": "../../../etc/passwd"}),
                Phase.INSPECT,
                NullCancellationToken(),
            )
        )
        assert result.ok is False
        assert "escapes the workspace" in (result.error or "")


class TestReadOnlyIsEnforcedByPolicy:
    def test_no_read_only_tool_is_mutating(self, repo: Path) -> None:
        """Read-only is a property of the contracts, not of good intentions."""
        for tool in read_only_tools(repo):
            assert tool.contract.mutating is False
            assert tool.contract.reversible is True

    def test_no_write_tool_is_reachable_in_inspect(self, gateway: ToolGateway) -> None:
        """The property is "nothing here can write", not a fixed roster.

        Asserting the exact set made adding a read-only tool look like a policy
        regression: `file.list` is non-mutating and produces no evidence, and
        pinning the names failed on it while the safety property was untouched.
        """
        reachable = gateway.available(Phase.INSPECT)
        assert reachable, "the inspect phase must expose something"
        for contract in reachable:
            assert contract.mutating is False, contract.name
            assert not contract.produces_evidence, contract.name


# ---------------------------------------------------------------------------
# The investigation loop
# ---------------------------------------------------------------------------


class TestConcludingWithoutLooking:
    """An answer produced without reading anything is a guess.

    Nothing downstream catches it: a question has no evidence gate, so the
    fabrication reaches the user unchallenged. A live run described a Node
    project with `package.json` and Jest in a directory holding one Python
    file, having called no tool at all.
    """

    def test_a_conclusion_before_any_lookup_is_refused_once(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        import asyncio

        agent = _agent(
            repo,
            gateway,
            [
                _conclude("it is a Node project with Jest"),
                _call("project.inspect"),
                _conclude("it is a Python project"),
            ],
        )
        found = asyncio.run(agent.investigate("what is this project?", require_lookup=True))

        assert found.ok is True
        assert found.conclusion == "it is a Python project"
        assert found.observations, "the agent must have looked at something"

    def test_it_gives_up_rather_than_looping(self, repo: Path, gateway: ToolGateway) -> None:
        """Bounded, so a model that will not look still leaves."""
        import asyncio

        agent = _agent(repo, gateway, [_conclude(f"guess {n}") for n in range(8)])
        found = asyncio.run(
            agent.investigate(
                "what is this project?",
                require_lookup=True,
                max_actions=8,
            )
        )

        assert found.ok is True
        assert found.conclusion.startswith("guess ")
        assert not found.observations

    def test_a_conclusion_after_a_lookup_is_accepted_immediately(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        import asyncio

        agent = _agent(repo, gateway, [_call("project.inspect"), _conclude("a grounded answer")])
        found = asyncio.run(agent.investigate("what is this project?", require_lookup=True))

        assert found.conclusion == "a grounded answer"


class TestInvestigation:
    def test_it_runs_tools_and_concludes(self, repo: Path, gateway: ToolGateway) -> None:
        import asyncio

        agent = _agent(
            repo,
            gateway,
            [
                _call("code.search", query="def login"),
                _call("file.read", path="src/auth.py"),
                _conclude("login() is in src/auth.py and takes user and password."),
            ],
        )
        result = asyncio.run(agent.investigate("Where is login defined?"))

        assert result.ok is True
        assert result.stopped_because == "the agent concluded"
        assert result.actions_taken == 2
        assert "src/auth.py" in result.files_seen

    def test_the_action_budget_is_enforced_by_the_runtime(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        """The model never decides how long to keep going."""
        import asyncio

        agent = _agent(repo, gateway, [_call("code.search", query="def")] * 10)
        result = asyncio.run(agent.investigate("Look around", max_actions=3))

        assert result.ok is False
        assert result.actions_taken == 3
        assert "budget exhausted" in result.stopped_because

    def test_a_refused_call_is_reported_back_not_raised(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        """A refusal is information the next decision can act on."""
        import asyncio

        agent = _agent(
            repo,
            gateway,
            [_call("file.write", path="x"), _conclude("I cannot write.")],
        )
        result = asyncio.run(agent.investigate("Try to write"))

        assert result.ok is True
        assert result.observations[0].ok is False
        assert "Refused" in result.observations[0].summary
        assert "unknown tool" in result.observations[0].summary

    def test_repeated_failures_stop_the_loop(self, repo: Path, gateway: ToolGateway) -> None:
        import asyncio

        agent = _agent(
            repo,
            gateway,
            [_call("file.read", path="does/not/exist.py")] * 6,
        )
        result = asyncio.run(agent.investigate("Read a missing file", max_actions=6))
        assert "consecutive failed actions" in result.stopped_because

    def test_unparseable_responses_stop_the_loop(self, repo: Path, gateway: ToolGateway) -> None:
        """Malformed output is recorded, never creatively repaired."""
        import asyncio

        agent = _agent(repo, gateway, ["not json at all"] * 6)
        result = asyncio.run(agent.investigate("Do something", max_actions=6))

        assert result.ok is False
        assert "unparseable" in result.stopped_because

    def test_an_inconsistent_decision_is_rejected(self, repo: Path, gateway: ToolGateway) -> None:
        """`action: call_tool` with no tool is a contract violation, not a guess."""
        import asyncio

        agent = _agent(
            repo,
            gateway,
            [json.dumps({"action": "call_tool"}), _conclude("recovered")],
        )
        result = asyncio.run(agent.investigate("Do something"))
        assert result.ok is True
        assert result.actions_taken == 0

    def test_an_unavailable_model_stops_honestly(self, repo: Path, gateway: ToolGateway) -> None:
        import asyncio

        model = FakeModelClient(unavailable=True)
        agent = ReadOnlyAgent(model, gateway, ContextCompiler(model))
        result = asyncio.run(agent.investigate("anything"))

        assert result.ok is False
        assert "model unavailable" in result.stopped_because

    def test_the_investigation_is_cancellable(self, repo: Path, gateway: ToolGateway) -> None:
        """A cancelled run must not return something that looks like an answer."""
        import asyncio

        agent = _agent(repo, gateway, [_conclude("done")])
        with pytest.raises(Cancelled):
            asyncio.run(agent.investigate("anything", cancel=CancelAfter(checks=0)))


# ---------------------------------------------------------------------------
# Grounded planning — the exit condition
# ---------------------------------------------------------------------------


class TestGroundedPlanning:
    @staticmethod
    def _plan_json(grounded_in: list[str]) -> str:
        return json.dumps(
            {
                "summary": "Add password hashing to login().",
                "steps": [
                    {
                        "title": "Hash passwords in login()",
                        "intent": "Replace the plaintext comparison.",
                        "files": ["src/auth.py"],
                        "acceptance_criteria": ["login() rejects a wrong password"],
                        "required_evidence": ["targeted auth tests pass"],
                        "risk": "medium",
                    }
                ],
                "grounded_in": grounded_in,
                "open_questions": [],
            }
        )

    def test_a_plan_is_produced_from_evidence(self, repo: Path, gateway: ToolGateway) -> None:
        import asyncio

        agent = _agent(
            repo,
            gateway,
            [
                _call("file.read", path="src/auth.py"),
                _conclude("login() compares plaintext."),
                self._plan_json(["src/auth.py"]),
            ],
        )

        async def scenario() -> ImplementationPlan | None:
            investigation = await agent.investigate("Make login secure")
            return await agent.plan("Make login secure", investigation)

        plan = asyncio.run(scenario())
        assert plan is not None
        assert len(plan.steps) == 1
        assert plan.steps[0].risk == "medium"

    def test_a_plan_citing_unread_files_is_not_grounded(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        """The check that makes 'grounded' mean something.

        A plan naming files the agent never opened is the confident
        fabrication v2 exists to prevent, and the runtime knows what was read.
        """
        import asyncio

        agent = _agent(
            repo,
            gateway,
            [
                _call("file.read", path="src/auth.py"),
                _conclude("looked at auth"),
                self._plan_json(["src/auth.py", "src/billing.py"]),
            ],
        )

        async def scenario() -> tuple[ImplementationPlan | None, object]:
            investigation = await agent.investigate("Make login secure")
            plan = await agent.plan("Make login secure", investigation)
            return plan, investigation

        plan, investigation = asyncio.run(scenario())
        assert plan is not None

        grounded, reason = is_grounded(plan, investigation)  # type: ignore[arg-type]
        assert grounded is False
        assert "src/billing.py" in reason

    def test_a_plan_citing_only_read_files_is_grounded(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        import asyncio

        agent = _agent(
            repo,
            gateway,
            [
                _call("file.read", path="src/auth.py"),
                _conclude("looked at auth"),
                self._plan_json(["src/auth.py"]),
            ],
        )

        async def scenario() -> tuple[ImplementationPlan | None, object]:
            investigation = await agent.investigate("Make login secure")
            plan = await agent.plan("Make login secure", investigation)
            return plan, investigation

        plan, investigation = asyncio.run(scenario())
        grounded, reason = is_grounded(plan, investigation)  # type: ignore[arg-type]
        assert grounded is True, reason

    def test_an_unparseable_plan_returns_none_not_a_fabrication(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        import asyncio

        agent = _agent(repo, gateway, [_conclude("done"), "this is not a plan"])

        async def scenario() -> ImplementationPlan | None:
            investigation = await agent.investigate("anything")
            return await agent.plan("anything", investigation)

        assert asyncio.run(scenario()) is None


class TestNothingIsModified:
    def test_the_repository_is_byte_identical_afterwards(
        self, repo: Path, gateway: ToolGateway
    ) -> None:
        """Milestone 4's headline promise, checked rather than asserted."""
        import asyncio

        from shamsu.artifacts.hashing import scan_repository

        before = scan_repository(repo)

        agent = _agent(
            repo,
            gateway,
            [
                _call("project.inspect"),
                _call("code.search", query="login"),
                _call("file.read", path="src/auth.py"),
                _conclude("done"),
            ],
        )
        asyncio.run(agent.investigate("Investigate thoroughly"))

        assert scan_repository(repo) == before


class TestManifestSummary:
    def test_it_compresses_json_into_project_facts(self, repo: Path, gateway: ToolGateway) -> None:
        """A frame has ~900 tokens for facts; the raw manifest is far larger."""
        import asyncio

        from shamsu.interfaces.cancellation import NullCancellationToken
        from shamsu.interfaces.tools import ToolRequest

        result = asyncio.run(
            gateway.invoke(
                ToolRequest(tool="project.inspect", arguments={}),
                Phase.INSPECT,
                NullCancellationToken(),
            )
        )
        summary = summarise_manifest(result.output)
        assert "Languages: Python" in summary
        assert len(summary) < len(result.output)

    def test_malformed_json_summarises_to_nothing(self) -> None:
        assert summarise_manifest("not json") == ""


class TestLimitsAreShared:
    def test_the_agent_uses_the_runtime_limits(self, repo: Path, gateway: ToolGateway) -> None:
        """Bounds live in one object, not scattered per component."""
        import asyncio

        model = FakeModelClient([_call("code.search", query="x")] * 10)
        agent = ReadOnlyAgent(
            model,
            gateway,
            ContextCompiler(model),
            ExecutionLimits(actions_per_step=2),
        )
        result = asyncio.run(agent.investigate("look"))
        assert result.actions_taken == 2
