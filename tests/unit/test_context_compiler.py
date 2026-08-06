"""The context compiler's budgeting and labelling rules.

Two properties everything downstream depends on: hot context is never silently
dropped, and a stale artifact never reaches the model unlabelled.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.fixtures.fake_model import FakeModelClient

from shamsu.context import ContextCompiler, ContextTooLarge, FrameInputs
from shamsu.interfaces.artifacts import Artifact, ArtifactMeta, SourceRef
from shamsu.interfaces.context import TokenBudget
from shamsu.interfaces.enums import ArtifactKind, ArtifactStatus, Phase, Risk
from shamsu.interfaces.ids import ArtifactId
from shamsu.interfaces.tools import ToolContract


def _compiler(budget: TokenBudget | None = None) -> ContextCompiler:
    return ContextCompiler(FakeModelClient(), budget)


def _inputs(**overrides: object) -> FrameInputs:
    base: dict[str, object] = {
        "phase": Phase.INSPECT,
        "task": "Find where login is defined.",
        "output_contract": "InvestigationStep",
    }
    base.update(overrides)
    return FrameInputs(**base)  # type: ignore[arg-type]


def _tool(name: str = "file.read") -> ToolContract:
    return ToolContract(
        name=name,
        purpose="Read a file.",
        allowed_phases=frozenset({Phase.INSPECT}),
        risk=Risk.LOW,
        reversible=True,
        timeout_seconds=10.0,
        max_output_bytes=4096,
    )


def _artifact(status: ArtifactStatus, key: str = "src/auth") -> Artifact:
    now = datetime.now(UTC)
    return Artifact(
        meta=ArtifactMeta(
            artifact_id=ArtifactId(key),
            kind=ArtifactKind.MODULE_CARD,
            key=key,
            sources=(SourceRef(path=f"{key}.py", content_hash="sha256:abc"),),
            artifact_version=1,
            generator_version="module-card/1",
            created_at=now,
            refreshed_at=now,
            status=status,
            confidence=1.0,
        ),
        content=f"# {key}\n\nexports login()",
    )


class TestFrameStructure:
    def test_the_task_is_always_present(self) -> None:
        frame = _compiler().compile(_inputs(), [])
        assert any(section.name == "current task" for section in frame.sections)

    def test_acceptance_criteria_ride_with_the_task(self) -> None:
        """What 'done' means must not be droppable separately from the task."""
        frame = _compiler().compile(
            _inputs(acceptance_criteria=["login rejects a wrong password"]), []
        )
        task = next(s for s in frame.sections if s.name == "current task")
        assert "login rejects a wrong password" in task.content

    def test_allowed_tools_are_rendered_by_name_and_purpose(self) -> None:
        frame = _compiler().compile(_inputs(), [_tool()])
        tools = next(s for s in frame.sections if s.name == "allowed tools")
        assert "file.read: Read a file." in tools.content

    def test_the_rendered_frame_uses_bracketed_headers(self) -> None:
        rendered = _compiler().compile(_inputs(), [_tool()]).render()
        assert "[CURRENT TASK]" in rendered
        assert "[ALLOWED TOOLS]" in rendered

    def test_empty_inputs_produce_no_empty_sections(self) -> None:
        """A heading with nothing under it wastes budget and reads as absence."""
        frame = _compiler().compile(_inputs(), [])
        assert all(section.content.strip() for section in frame.sections)

    def test_compilation_is_deterministic(self) -> None:
        """The same state must produce the same frame, or a bad decision is
        not reproducible."""
        inputs = _inputs(project_facts="Python project", latest_observation="found it")
        first = _compiler().compile(inputs, [_tool()])
        second = _compiler().compile(inputs, [_tool()])
        assert first.render() == second.render()
        assert first.tokens_used == second.tokens_used


class TestBudgeting:
    def test_cold_sections_are_dropped_under_pressure(self) -> None:
        tiny = TokenBudget(
            system_and_phase=0,
            task_and_criteria=40,
            step_and_plan=0,
            facts_and_artifacts=0,
            source_code=0,
            observations=0,
            tool_definitions=0,
        )
        frame = _compiler(tiny).compile(
            _inputs(
                project_facts="x" * 4000,
                previous_step_summary="y" * 4000,
            ),
            [],
        )
        assert "project facts" in frame.dropped_sections
        assert "previous step summary" in frame.dropped_sections

    def test_dropping_is_recorded_never_silent(self) -> None:
        """A decision made without the source code section is a different
        kind of decision, and telemetry should be able to say so."""
        tiny = TokenBudget(
            system_and_phase=0,
            task_and_criteria=40,
            step_and_plan=0,
            facts_and_artifacts=0,
            source_code=0,
            observations=0,
            tool_definitions=0,
        )
        frame = _compiler(tiny).compile(_inputs(source_excerpts=[("a.py", "z" * 4000)]), [])
        assert frame.dropped_sections != ()

    def test_hot_context_is_never_dropped(self) -> None:
        """It raises instead. A frame missing the task describes a different
        problem than the one being solved."""
        tiny = TokenBudget(
            system_and_phase=0,
            task_and_criteria=1,
            step_and_plan=0,
            facts_and_artifacts=0,
            source_code=0,
            observations=0,
            tool_definitions=0,
        )
        with pytest.raises(ContextTooLarge, match="current task"):
            _compiler(tiny).compile(_inputs(task="x" * 10_000), [])

    def test_the_error_says_what_did_not_fit(self) -> None:
        tiny = TokenBudget(
            system_and_phase=0,
            task_and_criteria=1,
            step_and_plan=0,
            facts_and_artifacts=0,
            source_code=0,
            observations=0,
            tool_definitions=0,
        )
        with pytest.raises(ContextTooLarge) as excinfo:
            _compiler(tiny).compile(_inputs(task="x" * 10_000), [])
        assert "tokens" in str(excinfo.value)

    def test_a_cold_section_cannot_starve_a_hot_one(self) -> None:
        """Hot sections are budgeted first regardless of authored order."""
        budget = TokenBudget(
            system_and_phase=0,
            task_and_criteria=100,
            step_and_plan=0,
            facts_and_artifacts=0,
            source_code=0,
            observations=0,
            tool_definitions=0,
        )
        frame = _compiler(budget).compile(
            _inputs(task="short task", project_facts="x" * 100_000), []
        )
        assert any(s.name == "current task" for s in frame.sections)
        assert "project facts" in frame.dropped_sections

    def test_tokens_used_stays_within_the_input_budget(self) -> None:
        frame = _compiler().compile(
            _inputs(project_facts="a" * 5_000, source_excerpts=[("x.py", "b" * 20_000)]),
            [_tool()],
        )
        assert frame.tokens_used <= frame.budget.input_total

    def test_sections_are_ordered_for_reading_not_for_budgeting(self) -> None:
        """Budgeting order is internal; the frame should read top to bottom."""
        frame = _compiler().compile(
            _inputs(
                system_rules="rules",
                project_facts="facts",
                latest_observation="observed",
            ),
            [_tool()],
        )
        names = [section.name for section in frame.sections]
        assert names.index("system and phase rules") < names.index("current task")
        assert names.index("current task") < names.index("project facts")


class TestArtifactHandling:
    def test_a_fresh_artifact_is_included_unlabelled(self) -> None:
        frame = _compiler().compile(_inputs(artifacts=[_artifact(ArtifactStatus.FRESH)]), [])
        section = next(s for s in frame.sections if s.name == "relevant artifacts")
        assert section.stale_warning is None
        assert "exports login()" in section.content

    def test_a_stale_artifact_is_included_but_labelled(self) -> None:
        """Plan §17.1: a stale structural claim must never arrive unlabelled."""
        frame = _compiler().compile(_inputs(artifacts=[_artifact(ArtifactStatus.STALE)]), [])
        section = next(s for s in frame.sections if s.name == "relevant artifacts")
        assert section.stale_warning is not None
        assert "src/auth" in section.stale_warning
        assert "[STALE]" in section.content

    def test_the_stale_warning_survives_rendering(self) -> None:
        """The label has to reach the model, not just the data structure."""
        rendered = (
            _compiler().compile(_inputs(artifacts=[_artifact(ArtifactStatus.STALE)]), []).render()
        )
        assert "STALE:" in rendered
        assert "Verify with a fresh tool call" in rendered

    @pytest.mark.parametrize(
        "status",
        [
            ArtifactStatus.INVALIDATED,
            ArtifactStatus.MISSING,
            ArtifactStatus.GENERATION_FAILED,
        ],
    )
    def test_unusable_artifacts_never_reach_the_frame(self, status: ArtifactStatus) -> None:
        """The last gate before the model sees content."""
        frame = _compiler().compile(_inputs(artifacts=[_artifact(status)]), [])
        assert all(section.name != "relevant artifacts" for section in frame.sections)

    def test_a_mixed_batch_keeps_only_the_usable_ones(self) -> None:
        frame = _compiler().compile(
            _inputs(
                artifacts=[
                    _artifact(ArtifactStatus.FRESH, "good"),
                    _artifact(ArtifactStatus.INVALIDATED, "bad"),
                ]
            ),
            [],
        )
        section = next(s for s in frame.sections if s.name == "relevant artifacts")
        assert "good" in section.content
        assert "bad" not in section.content
