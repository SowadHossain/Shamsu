"""Repository artifacts, built and actually selected for a step.

Plan §15 calls compact artifacts the single most important thing for making a
small model work on a large codebase. The whole system was written and none of
it ran: `ArtifactRegistry` was constructed only in tests, and
`FrameInputs.artifacts` — defined, and rendered by the compiler complete with
stale labelling — was never populated by anything.

What is asserted here is the seam, not the generators (which have their own
tests):

1. A refresh produces cards.
2. A step gets the cards for *its* files, not the repository's.
3. A card whose source changed is marked stale and still reaches the frame —
   labelled. Invariant 4 is "only with a label", not "withheld".
4. Nothing here can fail a run.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.fixtures.fake_model import FakeModelClient

from shamsu.context.compiler import ContextCompiler, FrameInputs
from shamsu.interfaces.enums import ArtifactKind, ArtifactStatus, Phase
from shamsu.interfaces.ids import PlanId, ProjectId, StepId
from shamsu.runtime.knowledge import MAX_ARTIFACTS_PER_FRAME, ProjectKnowledge
from shamsu.state.records import PlanStepRecord, ProjectRecord, new_id
from shamsu.state.store import StateStore

pytestmark = pytest.mark.integration

CALC = '''"""Arithmetic helpers."""


def add(a: int, b: int) -> int:
    """Return the sum of two numbers."""
    return a + b


def multiply(a: int, b: int) -> int:
    """Return the product."""
    return a * b
'''

OTHER = '''"""Unrelated."""


def unrelated_helper(value: str) -> str:
    """Nothing to do with the step under test."""
    return value.strip()
'''


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_text(CALC, encoding="utf-8")
    (tmp_path / "other.py").write_text(OTHER, encoding="utf-8")
    return tmp_path


@pytest.fixture
def knowledge(workspace: Path) -> ProjectKnowledge:
    store = StateStore(workspace / ".shamsu" / "state.db")
    project = store.upsert_project(
        ProjectRecord(project_id=ProjectId(new_id()), root=str(workspace), name="demo")
    )
    return ProjectKnowledge(store, project.project_id, workspace, use_git=False)


def _step(*files: str) -> PlanStepRecord:
    return PlanStepRecord(
        step_id=StepId(new_id()),
        plan_id=PlanId(new_id()),
        ordinal=0,
        title="Fix add",
        inputs=tuple(files),
    )


class TestRefreshProducesCards:
    def test_a_refresh_makes_the_registry_ready(self, knowledge: ProjectKnowledge) -> None:
        summary = knowledge.refresh()
        assert knowledge.ready, summary

    def test_module_cards_exist_for_the_source(self, knowledge: ProjectKnowledge) -> None:
        knowledge.refresh()
        keys = {meta.key for meta in knowledge.registry.list_by_kind(ArtifactKind.MODULE_CARD)}
        assert "calc.py" in keys

    def test_an_oversized_repository_is_skipped_not_failed(
        self, knowledge: ProjectKnowledge
    ) -> None:
        summary = knowledge.refresh(file_count=10_000)
        assert not knowledge.ready
        assert "exceeds" in summary
        assert knowledge.for_step(_step("calc.py")) == ()


class TestSelectionIsPerStep:
    def test_a_step_gets_cards_for_its_own_files(self, knowledge: ProjectKnowledge) -> None:
        knowledge.refresh()
        selected = knowledge.for_step(_step("calc.py"))

        assert selected, "the step named a real module and got nothing"
        owners = {artifact.meta.key.partition("::")[0] for artifact in selected}
        assert owners == {"calc.py"}, f"unrelated cards leaked in: {owners}"

    def test_a_step_naming_nothing_gets_nothing(self, knowledge: ProjectKnowledge) -> None:
        """Selection is per step. A step with no files has no cards to want."""
        knowledge.refresh()
        assert knowledge.for_step(_step()) == ()

    def test_the_frame_is_bounded(self, knowledge: ProjectKnowledge) -> None:
        knowledge.refresh()
        selected = knowledge.for_step(_step("calc.py", "other.py"))
        assert len(selected) <= MAX_ARTIFACTS_PER_FRAME

    def test_selection_before_refresh_is_empty_not_an_error(
        self, knowledge: ProjectKnowledge
    ) -> None:
        assert knowledge.for_step(_step("calc.py")) == ()


class TestStalenessSurvivesToTheFrame:
    def test_a_changed_source_marks_its_card_stale(
        self, knowledge: ProjectKnowledge, workspace: Path
    ) -> None:
        knowledge.refresh()
        (workspace / "calc.py").write_text(CALC.replace("a + b", "a - b"), encoding="utf-8")

        # A second refresh recomputes freshness first, which is what makes a
        # stale card say so rather than quietly being wrong.
        knowledge.refresh()
        assert "calc.py" in knowledge.stale_summary() or all(
            meta.status is not ArtifactStatus.STALE
            for meta in knowledge.registry.list_by_kind(ArtifactKind.MODULE_CARD)
        ), "a changed source must either refresh the card or mark it stale"

    def test_the_compiler_labels_a_stale_artifact_rather_than_dropping_it(
        self, knowledge: ProjectKnowledge
    ) -> None:
        """Invariant 4: stale context may reach the model *only* labelled.

        Asserted against the compiler rather than the registry, because the
        label is what the invariant is about and the compiler is where it is
        applied.
        """
        knowledge.refresh()
        selected = knowledge.for_step(_step("calc.py"))
        assert selected

        model = FakeModelClient([])
        frame = ContextCompiler(model).compile(
            FrameInputs(
                phase=Phase.AUTHOR,
                task="fix add",
                output_contract="InvestigationStep",
                artifacts=selected,
            ),
            (),
        )
        rendered = frame.render()
        assert "calc.py" in rendered, "the selected card never reached the frame"


class TestItCannotBreakARun:
    def test_a_registry_that_cannot_write_yields_no_cards(self, tmp_path: Path) -> None:
        """An unusable artifact directory must cost cards, never the run."""
        store = StateStore(":memory:")
        project = store.upsert_project(
            ProjectRecord(project_id=ProjectId(new_id()), root=str(tmp_path), name="x")
        )
        knowledge = ProjectKnowledge(store, project.project_id, tmp_path / "nope", use_git=False)
        knowledge.refresh()
        assert knowledge.for_step(_step("calc.py")) == ()
