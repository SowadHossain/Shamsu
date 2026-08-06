"""Contract tests for the interface layer.

These assert the properties the rest of the runtime is entitled to rely on. If
one of these breaks, something downstream is about to break silently.
"""

from __future__ import annotations

import asyncio

import pytest
from pydantic import BaseModel, ValidationError
from tests.fixtures.fake_model import CancelAfter, FakeModelClient

from shamsu.interfaces import (
    AgentState,
    ArtifactStatus,
    Cancelled,
    ContextFrame,
    ContextSection,
    ModelClient,
    ModelContractError,
    ModelMessage,
    ModelRequest,
    NullCancellationToken,
    Phase,
    Risk,
    TokenBudget,
    ToolContract,
    ToolResult,
)
from shamsu.interfaces.artifacts import ArtifactMeta, SourceRef
from shamsu.interfaces.enums import ArtifactKind, EvidenceKind
from shamsu.interfaces.ids import ArtifactId

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------


class TestEnums:
    def test_enums_are_string_valued_for_sqlite_round_tripping(self) -> None:
        """State is persisted to SQLite as text; the value must be the wire form."""
        assert Phase.AUTHOR == "author"
        assert AgentState.EXECUTE_CURRENT_STEP == "execute_current_step"
        assert Risk.HIGH == "high"
        assert ArtifactStatus.STALE == "stale"

    def test_every_state_machine_node_from_the_plan_exists(self) -> None:
        """Plan section 10 defines the graph; a missing node is an unreachable path."""
        required = {
            "receive_task",
            "load_project_state",
            "inspect_project",
            "classify_task",
            "create_plan",
            "validate_plan",
            "approval_check",
            "execute_current_step",
            "verify_current_step",
            "create_checkpoint",
            "repair",
            "replan",
            "check_remaining_steps",
            "final_verification",
            "completion_gate",
            "final_report",
        }
        assert required <= {state.value for state in AgentState}

    def test_every_phase_from_the_plan_exists(self) -> None:
        assert {phase.value for phase in Phase} == {
            "inspect",
            "plan",
            "author",
            "verify",
            "repair",
            "deploy",
            "complete",
        }


# --------------------------------------------------------------------------
# Cancellation -- the defect v2 exists to fix
# --------------------------------------------------------------------------


class TestCancellation:
    def test_null_token_is_never_cancelled(self) -> None:
        token = NullCancellationToken()
        assert token.cancelled is False
        assert token.reason is None
        token.raise_if_cancelled()

    def test_awaiting_the_null_token_never_resolves(self) -> None:
        """It is never cancelled, so awaiting it must never complete.

        This method exists to be *raced* against real work. An implementation
        that returns or raises promptly would be read by the race as
        "cancelled", which is how a timeout gets misreported as a user
        interrupt. The caller's timeout bounds the wait.
        """

        async def scenario() -> None:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(NullCancellationToken().wait_cancelled(), timeout=0.05)

        asyncio.run(scenario())

    def test_raise_if_cancelled_raises_the_shamsu_type(self) -> None:
        """Distinct from asyncio.CancelledError, so cancel and feedback never blur."""
        token = CancelAfter(checks=0, reason="user pressed ctrl-c")
        with pytest.raises(Cancelled) as excinfo:
            token.raise_if_cancelled()
        assert excinfo.value.reason == "user pressed ctrl-c"

    def test_cancellation_can_be_placed_at_a_precise_checkpoint(self) -> None:
        token = CancelAfter(checks=2)
        token.raise_if_cancelled()
        token.raise_if_cancelled()
        with pytest.raises(Cancelled):
            token.raise_if_cancelled()


# --------------------------------------------------------------------------
# Tool contracts
# --------------------------------------------------------------------------


def _contract(**overrides: object) -> ToolContract:
    base: dict[str, object] = {
        "name": "file.patch",
        "purpose": "Apply a patch to a file.",
        "allowed_phases": frozenset({Phase.AUTHOR, Phase.REPAIR}),
        "risk": Risk.MEDIUM,
        "reversible": True,
        "timeout_seconds": 30.0,
        "max_output_bytes": 8192,
        "produces_evidence": frozenset({EvidenceKind.FILE_CHANGED}),
        "invalidates": frozenset({ArtifactKind.MODULE_CARD, ArtifactKind.SYMBOL_CARD}),
        "mutating": True,
    }
    base.update(overrides)
    return ToolContract(**base)  # type: ignore[arg-type]


class TestToolContract:
    def test_matches_the_plan_example(self) -> None:
        """Plan section 23's worked example must be expressible verbatim."""
        contract = _contract()
        assert contract.name == "file.patch"
        assert contract.allowed_phases == frozenset({Phase.AUTHOR, Phase.REPAIR})
        assert contract.requires_approval is False
        assert contract.reversible is True
        assert EvidenceKind.FILE_CHANGED in contract.produces_evidence
        assert ArtifactKind.TEST_MAP not in contract.invalidates

    def test_is_immutable(self) -> None:
        """A tool must not be able to widen its own permissions at runtime."""
        contract = _contract()
        with pytest.raises(ValidationError):
            contract.risk = Risk.LOW  # type: ignore[misc]

    def test_rejects_a_nonpositive_timeout(self) -> None:
        with pytest.raises(ValidationError):
            _contract(timeout_seconds=0.0)

    def test_rejects_an_unbounded_timeout(self) -> None:
        with pytest.raises(ValidationError):
            _contract(timeout_seconds=100_000.0)

    def test_rejects_an_unbounded_output_cap(self) -> None:
        """Output is capped before entering context; there is no 'no limit'."""
        with pytest.raises(ValidationError):
            _contract(max_output_bytes=0)

    def test_defaults_to_non_mutating(self) -> None:
        """Mutation must be opt-in. A forgotten flag should fail closed."""
        contract = ToolContract(
            name="file.read",
            purpose="Read a file.",
            allowed_phases=frozenset({Phase.INSPECT}),
            risk=Risk.LOW,
            reversible=True,
            timeout_seconds=10.0,
            max_output_bytes=4096,
        )
        assert contract.mutating is False
        assert contract.requires_approval is False
        assert contract.produces_evidence == frozenset()


class TestToolResult:
    def test_honest_failure_is_representable(self) -> None:
        result = ToolResult(tool="test.run", ok=False, error="2 tests failed")
        assert result.ok is False
        assert result.error == "2 tests failed"
        assert result.evidence == frozenset()

    def test_truncation_is_recorded_with_the_original_size(self) -> None:
        result = ToolResult(
            tool="file.read",
            ok=True,
            output="x" * 100,
            truncated=True,
            original_bytes=50_000,
        )
        assert result.truncated is True
        assert result.original_bytes == 50_000


# --------------------------------------------------------------------------
# Artifact freshness
# --------------------------------------------------------------------------


def _meta(status: ArtifactStatus) -> ArtifactMeta:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return ArtifactMeta(
        artifact_id=ArtifactId("a1"),
        kind=ArtifactKind.MODULE_CARD,
        key="src/shamsu/runtime",
        sources=(SourceRef(path="src/shamsu/runtime/__init__.py", content_hash="abc123"),),
        artifact_version=1,
        generator_version="module-card/1",
        created_at=now,
        refreshed_at=now,
        status=status,
        confidence=1.0,
    )


class TestArtifactFreshness:
    @pytest.mark.parametrize("status", [ArtifactStatus.FRESH, ArtifactStatus.STALE])
    def test_fresh_and_stale_may_reach_the_model(self, status: ArtifactStatus) -> None:
        """Stale is usable but must be labelled; that labelling is the compiler's job."""
        assert _meta(status).usable is True

    @pytest.mark.parametrize(
        "status",
        [
            ArtifactStatus.INVALIDATED,
            ArtifactStatus.MISSING,
            ArtifactStatus.GENERATION_FAILED,
        ],
    )
    def test_invalidated_missing_and_failed_must_not(self, status: ArtifactStatus) -> None:
        assert _meta(status).usable is False

    def test_confidence_is_bounded(self) -> None:
        fields = _meta(ArtifactStatus.FRESH).model_dump()
        with pytest.raises(ValidationError):
            ArtifactMeta.model_validate(fields | {"confidence": 1.5})

    def test_model_written_content_can_be_ranked_below_parsed_facts(self) -> None:
        """Confidence exists so a prose summary never outranks a parsed symbol."""
        fields = _meta(ArtifactStatus.FRESH).model_dump()
        summarised = ArtifactMeta.model_validate(fields | {"confidence": 0.6})
        assert summarised.confidence < _meta(ArtifactStatus.FRESH).confidence

    def test_generator_version_is_tracked_separately_from_artifact_version(self) -> None:
        """A generator change invalidates artifacts whose sources never changed."""
        meta = _meta(ArtifactStatus.FRESH)
        assert meta.artifact_version == 1
        assert meta.generator_version == "module-card/1"


# --------------------------------------------------------------------------
# Context frames
# --------------------------------------------------------------------------


class TestTokenBudget:
    def test_default_budget_matches_the_plan(self) -> None:
        """Plan section 19.2 specifies an 8K frame. Drift here silently blows context."""
        budget = TokenBudget()
        assert budget.input_total == 6300
        assert budget.output_reserve == 1700
        assert budget.total == 8000


class TestContextFrame:
    def test_render_labels_stale_sections(self) -> None:
        """A stale structural claim must never reach the model unlabelled."""
        frame = ContextFrame(
            phase=Phase.AUTHOR,
            sections=(
                ContextSection(name="current task", content="Add a login endpoint", tokens=6),
                ContextSection(
                    name="relevant artifacts",
                    content="auth module exposes login()",
                    tokens=7,
                    stale_warning="auth.py changed since this card was built",
                ),
            ),
            allowed_tools=(),
            output_contract="StepDecision",
            budget=TokenBudget(),
            tokens_used=13,
        )
        rendered = frame.render()
        assert "[CURRENT TASK]" in rendered
        assert "[RELEVANT ARTIFACTS]" in rendered
        assert "STALE: auth.py changed since this card was built" in rendered

    def test_dropped_sections_are_recorded_not_silent(self) -> None:
        frame = ContextFrame(
            phase=Phase.INSPECT,
            sections=(),
            allowed_tools=(),
            output_contract="Inspection",
            budget=TokenBudget(),
            tokens_used=0,
            dropped_sections=("relevant source code",),
        )
        assert frame.dropped_sections == ("relevant source code",)


# --------------------------------------------------------------------------
# The model seam
# --------------------------------------------------------------------------


class _Decision(BaseModel):
    action: str
    target: str


def _request(text: str = "decide") -> ModelRequest:
    return ModelRequest(
        messages=(ModelMessage(role="user", content=text),),
        max_output_tokens=256,
    )


class TestFakeModelSatisfiesTheProtocol:
    def test_structurally_matches_model_client(self) -> None:
        """The fake must be substitutable, or the tests prove nothing."""
        assert isinstance(FakeModelClient(), ModelClient)

    def test_replays_scripted_responses_in_order(self) -> None:
        client = FakeModelClient(["first", "second"])

        async def run() -> list[str]:
            token = NullCancellationToken()
            return [
                (await client.generate(_request(), token)).text,
                (await client.generate(_request(), token)).text,
            ]

        assert asyncio.run(run()) == ["first", "second"]
        assert client.calls == 2

    def test_parses_a_satisfied_contract(self) -> None:
        client = FakeModelClient(['{"action": "read", "target": "auth.py"}'])
        decision = asyncio.run(
            client.generate_typed(_request(), _Decision, NullCancellationToken())
        )
        assert decision.action == "read"
        assert decision.target == "auth.py"

    def test_contract_violation_carries_the_raw_text(self) -> None:
        """A failure capsule needs what the model actually said, not a summary."""
        client = FakeModelClient(["I think you should read auth.py"])
        with pytest.raises(ModelContractError) as excinfo:
            asyncio.run(client.generate_typed(_request(), _Decision, NullCancellationToken()))
        assert excinfo.value.raw_text == "I think you should read auth.py"

    def test_a_wrong_shaped_json_response_is_a_failure_not_a_guess(self) -> None:
        client = FakeModelClient(['{"action": "read"}'])
        with pytest.raises(ModelContractError):
            asyncio.run(client.generate_typed(_request(), _Decision, NullCancellationToken()))

    def test_generate_observes_cancellation_before_doing_work(self) -> None:
        client = FakeModelClient(["never returned"])
        with pytest.raises(Cancelled):
            asyncio.run(client.generate(_request(), CancelAfter(checks=0)))
        assert client.calls == 0

    def test_unavailable_is_distinct_from_a_timeout(self) -> None:
        from shamsu.interfaces import ModelUnavailable

        client = FakeModelClient(unavailable=True)
        with pytest.raises(ModelUnavailable):
            asyncio.run(client.generate(_request(), NullCancellationToken()))


class TestModelRequestValidation:
    def test_rejects_an_unknown_role(self) -> None:
        with pytest.raises(ValidationError):
            ModelMessage(role="tool", content="x")

    def test_rejects_a_nonpositive_output_budget(self) -> None:
        with pytest.raises(ValidationError):
            ModelRequest(messages=(), max_output_tokens=0)
