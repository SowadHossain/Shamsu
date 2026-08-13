"""The tool gateway's policy enforcement.

The property under test throughout: **every refusal happens before the side
effect.** A `ToolPolicyViolation` from `invoke` must always mean nothing ran.
Each refusal test therefore asserts on a spy that records execution, not just
on the exception.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, Field
from tests.fixtures.fake_model import CancelAfter

from shamsu.interfaces.cancellation import Cancelled, NullCancellationToken
from shamsu.interfaces.enums import ArtifactKind, EvidenceKind, Phase, Risk
from shamsu.interfaces.tools import (
    ToolContract,
    ToolPolicyViolation,
    ToolRequest,
    ToolResult,
)
from shamsu.tools import Tool, ToolGateway, deny_all


class EchoInput(BaseModel):
    text: str
    repeat: int = Field(default=1, ge=1, le=10)


def _contract(name: str = "demo.echo", **overrides: object) -> ToolContract:
    base: dict[str, object] = {
        "name": name,
        "purpose": "Echo text back.",
        "allowed_phases": frozenset({Phase.INSPECT}),
        "risk": Risk.LOW,
        "reversible": True,
        "timeout_seconds": 5.0,
        "max_output_bytes": 1024,
    }
    base.update(overrides)
    return ToolContract(**base)  # type: ignore[arg-type]


class EchoTool(Tool[EchoInput]):
    """Records every execution so refusal tests can prove nothing ran."""

    input_model = EchoInput

    def __init__(self, contract: ToolContract | None = None) -> None:
        self.contract = contract or _contract()
        self.calls: list[EchoInput] = []

    async def run(self, arguments: EchoInput, cancel: CancelAfter) -> ToolResult:  # type: ignore[override]
        self.calls.append(arguments)
        return self.ok(arguments.text * arguments.repeat)


class SlowTool(Tool[EchoInput]):
    input_model = EchoInput

    def __init__(self, delay: float, contract: ToolContract | None = None) -> None:
        self.contract = contract or _contract("demo.slow", timeout_seconds=0.05)
        self.delay = delay
        self.finished = False

    async def run(self, arguments: EchoInput, cancel: CancelAfter) -> ToolResult:  # type: ignore[override]
        await asyncio.sleep(self.delay)
        self.finished = True
        return self.ok("done")


class ExplodingTool(Tool[EchoInput]):
    input_model = EchoInput

    def __init__(self) -> None:
        self.contract = _contract("demo.boom")

    async def run(self, arguments: EchoInput, cancel: CancelAfter) -> ToolResult:  # type: ignore[override]
        raise RuntimeError("something unexpected")


class MutatingTool(Tool[EchoInput]):
    """A tool that writes the file named in `text`."""

    input_model = EchoInput

    def __init__(self) -> None:
        self.contract = _contract(
            "demo.mutate",
            allowed_phases=frozenset({Phase.AUTHOR, Phase.REPAIR}),
            mutating=True,
        )
        self.calls: list[EchoInput] = []

    def write_targets(self, arguments: EchoInput) -> tuple[str, ...]:
        return (arguments.text,)

    async def run(self, arguments: EchoInput, cancel: CancelAfter) -> ToolResult:  # type: ignore[override]
        self.calls.append(arguments)
        return self.ok(f"wrote {arguments.text}")


class _AllowList:
    """A minimal `WriteScope` for gateway tests."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed

    def permits(self, path: str) -> bool:
        return path in self.allowed

    def describe(self) -> str:
        return f"This step may write only {', '.join(sorted(self.allowed))}."


def _scope(allowed: set[str]) -> _AllowList:
    return _AllowList(allowed)


def _invoke(
    gateway: ToolGateway,
    tool: str,
    phase: Phase = Phase.INSPECT,
    cancel: object | None = None,
    **arguments: object,
) -> ToolResult:
    return asyncio.run(
        gateway.invoke(
            ToolRequest(tool=tool, arguments=arguments),
            phase,
            cancel or NullCancellationToken(),  # type: ignore[arg-type]
        )
    )


# ---------------------------------------------------------------------------
# Registration and discovery
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_a_registered_tool_runs(self) -> None:
        gateway = ToolGateway([EchoTool()])
        result = _invoke(gateway, "demo.echo", text="hi")
        assert result.ok is True
        assert result.output == "hi"

    def test_duplicate_names_are_rejected(self) -> None:
        """Silently replacing would let a later registration change an earlier
        contract's promise."""
        gateway = ToolGateway([EchoTool()])
        with pytest.raises(ValueError, match="already registered"):
            gateway.register(EchoTool())

    def test_an_unknown_tool_is_refused_with_the_alternatives(self) -> None:
        gateway = ToolGateway([EchoTool()])
        with pytest.raises(ToolPolicyViolation) as excinfo:
            _invoke(gateway, "demo.nope", text="x")
        assert "demo.echo" in str(excinfo.value)


class TestPhaseVisibility:
    def test_the_model_only_sees_reachable_tools(self) -> None:
        """A wrong-phase call should be a runtime bug, not a model mistake."""
        gateway = ToolGateway(
            [
                EchoTool(_contract("read.only", allowed_phases=frozenset({Phase.INSPECT}))),
                EchoTool(_contract("write.only", allowed_phases=frozenset({Phase.AUTHOR}))),
            ]
        )
        assert [c.name for c in gateway.available(Phase.INSPECT)] == ["read.only"]
        assert [c.name for c in gateway.available(Phase.AUTHOR)] == ["write.only"]

    def test_schemas_are_scoped_to_the_phase_too(self) -> None:
        gateway = ToolGateway(
            [EchoTool(_contract("read.only", allowed_phases=frozenset({Phase.INSPECT})))]
        )
        assert gateway.schemas(Phase.AUTHOR) == ()
        assert [s["name"] for s in gateway.schemas(Phase.INSPECT)] == ["read.only"]

    def test_the_schema_shown_is_the_schema_enforced(self) -> None:
        """If these drift, the model 'keeps calling it wrong' forever."""
        tool = EchoTool()
        gateway = ToolGateway([tool])
        shown = gateway.schemas(Phase.INSPECT)[0]["parameters"]
        assert shown == tool.input_model.model_json_schema()

    def test_a_wrong_phase_call_is_refused_without_executing(self) -> None:
        tool = EchoTool(_contract(allowed_phases=frozenset({Phase.INSPECT})))
        gateway = ToolGateway([tool])
        with pytest.raises(ToolPolicyViolation, match="not allowed in phase author"):
            _invoke(gateway, "demo.echo", phase=Phase.AUTHOR, text="x")
        assert tool.calls == []


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


class TestApproval:
    def test_the_default_policy_denies(self) -> None:
        """An unconfigured gateway must not approve everything."""
        assert deny_all(_contract(), ToolRequest(tool="demo.echo", arguments={})) is False

    def test_an_unapproved_call_never_executes(self) -> None:
        tool = EchoTool(_contract(requires_approval=True, risk=Risk.HIGH))
        gateway = ToolGateway([tool])
        with pytest.raises(ToolPolicyViolation, match="requires approval"):
            _invoke(gateway, "demo.echo", text="x")
        assert tool.calls == []

    def test_an_approved_call_proceeds(self) -> None:
        tool = EchoTool(_contract(requires_approval=True, risk=Risk.HIGH))
        gateway = ToolGateway([tool], approval=lambda contract, request: True)
        assert _invoke(gateway, "demo.echo", text="x").ok is True
        assert len(tool.calls) == 1

    def test_approval_is_asked_before_argument_validation(self) -> None:
        """A high-risk call should not be dismissed as a typo when the real
        answer is 'a human must decide'."""
        asked: list[str] = []
        tool = EchoTool(_contract(requires_approval=True))
        gateway = ToolGateway(
            [tool],
            approval=lambda contract, request: asked.append(contract.name) or False,  # type: ignore[func-returns-value]
        )
        with pytest.raises(ToolPolicyViolation, match="requires approval"):
            _invoke(gateway, "demo.echo", nonsense=True)
        assert asked == ["demo.echo"]

    def test_the_approver_sees_the_contract_and_the_request(self) -> None:
        seen: list[tuple[str, dict[str, object]]] = []

        def approve(contract: ToolContract, request: ToolRequest) -> bool:
            seen.append((contract.name, dict(request.arguments)))
            return True

        gateway = ToolGateway([EchoTool(_contract(requires_approval=True))], approval=approve)
        _invoke(gateway, "demo.echo", text="x")
        assert seen == [("demo.echo", {"text": "x"})]


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_a_missing_field_is_refused_without_executing(self) -> None:
        tool = EchoTool()
        gateway = ToolGateway([tool])
        with pytest.raises(ToolPolicyViolation, match="invalid arguments"):
            _invoke(gateway, "demo.echo")
        assert tool.calls == []

    def test_an_out_of_range_value_is_refused(self) -> None:
        gateway = ToolGateway([EchoTool()])
        with pytest.raises(ToolPolicyViolation):
            _invoke(gateway, "demo.echo", text="x", repeat=99)

    def test_the_error_names_the_offending_field(self) -> None:
        """The model reads this and has to act on it."""
        gateway = ToolGateway([EchoTool()])
        with pytest.raises(ToolPolicyViolation) as excinfo:
            _invoke(gateway, "demo.echo", repeat=99)
        message = str(excinfo.value)
        assert "text" in message
        assert "repeat" in message

    def test_validated_arguments_reach_the_implementation(self) -> None:
        """`run` never sees raw arguments, so it cannot skip validation."""
        tool = EchoTool()
        gateway = ToolGateway([tool])
        _invoke(gateway, "demo.echo", text="ab", repeat=2)
        assert isinstance(tool.calls[0], EchoInput)
        assert tool.calls[0].repeat == 2


# ---------------------------------------------------------------------------
# The mutation budget
# ---------------------------------------------------------------------------


class TestMutationBudget:
    @staticmethod
    def _mutating_gateway(budget: int = 1) -> tuple[ToolGateway, EchoTool]:
        tool = EchoTool(
            _contract(
                "file.patch",
                allowed_phases=frozenset({Phase.AUTHOR}),
                mutating=True,
                risk=Risk.MEDIUM,
                produces_evidence=frozenset({EvidenceKind.FILE_CHANGED}),
                invalidates=frozenset({ArtifactKind.MODULE_CARD}),
            )
        )
        return ToolGateway([tool], mutating_calls_per_decision=budget), tool

    def test_one_mutation_per_decision_is_allowed(self) -> None:
        gateway, tool = self._mutating_gateway()
        with gateway.decision():
            _invoke(gateway, "file.patch", phase=Phase.AUTHOR, text="x")
        assert len(tool.calls) == 1

    def test_a_second_mutation_in_one_decision_is_refused(self) -> None:
        """One misunderstood instruction must not cascade into a sequence of
        writes before anything verifies the first."""
        gateway, tool = self._mutating_gateway()
        with gateway.decision():
            _invoke(gateway, "file.patch", phase=Phase.AUTHOR, text="x")
            with pytest.raises(ToolPolicyViolation, match="mutation budget"):
                _invoke(gateway, "file.patch", phase=Phase.AUTHOR, text="y")
        assert len(tool.calls) == 1

    def test_the_budget_resets_per_decision(self) -> None:
        gateway, tool = self._mutating_gateway()
        for _ in range(3):
            with gateway.decision():
                _invoke(gateway, "file.patch", phase=Phase.AUTHOR, text="x")
        assert len(tool.calls) == 3

    def test_non_mutating_calls_are_unbounded(self) -> None:
        """Reads are free; only side effects are rationed."""
        tool = EchoTool()
        gateway = ToolGateway([tool])
        with gateway.decision():
            for _ in range(5):
                _invoke(gateway, "demo.echo", text="x")
        assert len(tool.calls) == 5

    def test_remaining_budget_is_observable(self) -> None:
        gateway, _ = self._mutating_gateway()
        with gateway.decision():
            assert gateway.mutations_remaining == 1
            _invoke(gateway, "file.patch", phase=Phase.AUTHOR, text="x")
            assert gateway.mutations_remaining == 0


# ---------------------------------------------------------------------------
# Execution: evidence, truncation, timeout, cancellation, crashes
# ---------------------------------------------------------------------------


class TestResults:
    def test_a_successful_result_carries_the_declared_evidence(self) -> None:
        tool = EchoTool(_contract(produces_evidence=frozenset({EvidenceKind.TESTS_PASSED})))
        result = _invoke(ToolGateway([tool]), "demo.echo", text="x")
        assert result.evidence == frozenset({EvidenceKind.TESTS_PASSED})

    def test_a_failed_result_carries_no_evidence(self) -> None:
        """A tool that did not do its job did not produce proof of doing it.

        This is what stops `ok=False` from advancing the completion gate.
        """
        tool = ExplodingTool()
        result = _invoke(ToolGateway([tool]), "demo.boom", text="x")
        assert result.ok is False
        assert result.evidence == frozenset()

    def test_an_unexpected_crash_becomes_an_honest_failure(self) -> None:
        """A tool bug must not escape into the runtime as an exception."""
        result = _invoke(ToolGateway([ExplodingTool()]), "demo.boom", text="x")
        assert result.ok is False
        assert "RuntimeError" in (result.error or "")
        assert "something unexpected" in (result.error or "")


class TestOutputCapping:
    def test_oversized_output_is_truncated(self) -> None:
        tool = EchoTool(_contract(max_output_bytes=50))
        result = _invoke(ToolGateway([tool]), "demo.echo", text="x" * 100)
        assert result.truncated is True
        assert result.original_bytes == 100

    def test_the_notice_tells_the_model_how_to_see_more(self) -> None:
        """Truncation must be recoverable, not a silent hole."""
        tool = EchoTool(_contract(max_output_bytes=50))
        result = _invoke(ToolGateway([tool]), "demo.echo", text="x" * 100)
        assert "truncated" in result.output
        assert "Narrow the range or query" in result.output

    def test_output_within_the_cap_is_untouched(self) -> None:
        result = _invoke(ToolGateway([EchoTool()]), "demo.echo", text="small")
        assert result.truncated is False
        assert result.output == "small"
        assert result.original_bytes is None

    def test_multibyte_output_does_not_produce_broken_text(self) -> None:
        """Cutting UTF-8 at a byte boundary can split a character."""
        tool = EchoTool(_contract(max_output_bytes=11))
        result = _invoke(ToolGateway([tool]), "demo.echo", text="日本語" * 10)
        assert result.truncated is True
        result.output.encode("utf-8")  # must not raise


class TestTimeoutAndCancellation:
    def test_a_slow_tool_times_out_honestly(self) -> None:
        tool = SlowTool(delay=5.0)
        result = _invoke(ToolGateway([tool]), "demo.slow", text="x")
        assert result.ok is False
        assert "timed out" in (result.error or "")

    def test_a_timed_out_tool_is_not_left_running(self) -> None:
        """An abandoned task is how a 'stopped' run keeps touching the workspace."""
        tool = SlowTool(delay=5.0)
        _invoke(ToolGateway([tool]), "demo.slow", text="x")
        assert tool.finished is False

    def test_cancellation_before_execution_refuses_immediately(self) -> None:
        tool = EchoTool()
        gateway = ToolGateway([tool])
        with pytest.raises(Cancelled):
            _invoke(gateway, "demo.echo", cancel=CancelAfter(checks=0), text="x")
        assert tool.calls == []

    def test_cancellation_during_execution_stops_the_tool(self) -> None:
        """The tool is raced against the token, not merely checked after."""
        from shamsu.runtime import RunToken

        tool = SlowTool(delay=5.0, contract=_contract("demo.slow", timeout_seconds=30.0))
        gateway = ToolGateway([tool])

        async def scenario() -> None:
            token = RunToken()
            asyncio.get_running_loop().call_later(0.02, token.request, "user interrupt")
            with pytest.raises(Cancelled, match="user interrupt"):
                await gateway.invoke(
                    ToolRequest(tool="demo.slow", arguments={"text": "x"}),
                    Phase.INSPECT,
                    token,
                )

        asyncio.run(scenario())
        assert tool.finished is False


class TestArgumentsAreShown:
    """A tool the model can call but whose parameters it must guess.

    That was the state before this: `available()` returned name and purpose
    only, so the model guessed `file_path` at a tool wanting `path`, was
    refused, and burned its action budget on rejected calls.
    """

    def test_available_names_each_tools_arguments(self) -> None:
        from pathlib import Path

        from shamsu.tools import authoring_tools

        gateway = ToolGateway(authoring_tools(Path(".")))
        by_name = {c.name: c for c in gateway.available(Phase.AUTHOR)}

        assert "path*" in by_name["file.read"].arguments
        assert "path*" in by_name["file.patch"].arguments

    def test_required_arguments_come_first_and_are_marked(self) -> None:
        from pathlib import Path

        from shamsu.tools import authoring_tools

        gateway = ToolGateway(authoring_tools(Path(".")))
        arguments = {c.name: c.arguments for c in gateway.available(Phase.AUTHOR)}["file.read"]

        assert arguments[0].endswith("*"), "a required argument leads"
        optional = [name for name in arguments if not name.endswith("*")]
        required = [name for name in arguments if name.endswith("*")]
        assert list(arguments) == required + optional

    def test_the_names_come_from_the_schema_the_gateway_validates_against(self) -> None:
        """Derived, not declared — so the two cannot describe different tools."""
        from pathlib import Path

        from shamsu.tools import authoring_tools

        tools = {tool.name: tool for tool in authoring_tools(Path("."))}
        gateway = ToolGateway(list(tools.values()))

        for contract in gateway.available(Phase.AUTHOR):
            declared = {name.rstrip("*") for name in contract.arguments}
            actual = set(tools[contract.name].input_schema().get("properties", {}))
            assert declared == actual, contract.name


class TestWriteScope:
    """Restricting *which* files may be written, not just whether."""

    def test_every_mutating_tool_declares_its_write_targets(self) -> None:
        """A `WriteScope` cannot constrain a tool that does not say what it writes.

        The exemption list is the point: a new mutating tool must either
        declare its targets or be added here deliberately, with a reason. It
        cannot become unconstrained by omission.
        """
        from shamsu.tools import authoring_tools

        # `git.checkpoint` records what is already on disk; it writes no file
        # the scope would have anything to say about.
        exempt = {"git.checkpoint"}

        for tool in authoring_tools(Path(".")):
            if not tool.contract.mutating or tool.contract.name in exempt:
                continue
            declared = type(tool).write_targets is not Tool.write_targets
            assert declared, (
                f"{tool.contract.name} mutates but does not override write_targets, "
                "so no write scope can constrain it"
            )

    def test_a_scope_refuses_a_write_outside_it(self) -> None:
        gateway = ToolGateway([MutatingTool()])
        with (
            gateway.restricted_to(_scope({"allowed.py"})),
            pytest.raises(ToolPolicyViolation, match="outside the permitted write scope"),
        ):
            _invoke(gateway, "demo.mutate", phase=Phase.AUTHOR, text="other.py")

    def test_a_scope_permits_a_write_inside_it(self) -> None:
        gateway = ToolGateway([MutatingTool()])
        with gateway.restricted_to(_scope({"allowed.py"})):
            result = _invoke(gateway, "demo.mutate", phase=Phase.AUTHOR, text="allowed.py")
        assert result.ok is True

    def test_no_scope_means_no_restriction(self) -> None:
        """The gateway is not a write-blocker by default; phases handle that."""
        gateway = ToolGateway([MutatingTool()])
        assert _invoke(gateway, "demo.mutate", phase=Phase.AUTHOR, text="anything.py").ok is True

    def test_the_refusal_explains_the_restriction(self) -> None:
        """The model has to act on this text."""
        gateway = ToolGateway([MutatingTool()])
        with (
            gateway.restricted_to(_scope({"allowed.py"})),
            pytest.raises(ToolPolicyViolation) as caught,
        ):
            _invoke(gateway, "demo.mutate", phase=Phase.AUTHOR, text="other.py")
        assert "only allowed.py" in str(caught.value)
