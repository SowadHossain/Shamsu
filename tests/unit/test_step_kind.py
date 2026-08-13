"""Re-reading a step's kind, so a gate is never built without a key.

`PlanStepProposal.kind` defaults to `change`, which carries a mandatory floor
of `file_changed` and `git_diff_reviewed`. For a step that was never going to
write anything, that floor is not a guard — it is a requirement no honest
execution can satisfy, so the gate refuses forever.

The observed failure: a model asked to plan the greeting "hi" proposed one step
titled "Understand the task", omitted `kind`, and the run ended BLOCKED on
missing `file_changed`.
"""

from __future__ import annotations

from shamsu.agent.planning import (
    CHANGE_FLOOR,
    CHANGE_TOOLS,
    READ_ONLY_TOOLS,
    effective_kind,
    materialise,
    validate_plan,
)
from shamsu.interfaces.enums import EvidenceKind
from shamsu.interfaces.ids import TaskId
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal


def proposal(title: str, **overrides: object) -> PlanStepProposal:
    return PlanStepProposal(title=title, **overrides)  # type: ignore[arg-type]


class TestInvestigativeStepsAreRecognised:
    def test_the_greeting_case(self) -> None:
        """The exact step that produced a BLOCKED run."""
        assert effective_kind(proposal("Understand the task")) == "investigate"

    def test_investigative_openers(self) -> None:
        for title in (
            "Understand the current authentication flow",
            "Review the existing tests",
            "Analyse how the planner builds a frame",
            "Examine the database schema",
            "Identify where sessions are persisted",
            "Locate the login handler",
            "Determine which module owns routing",
            "Investigate the failing assertion",
            "Read the configuration",
        ):
            assert effective_kind(proposal(title)) == "investigate", title


class TestTheVerbOutranksTheFileList:
    """Naming a file you will read is not naming a file you will write.

    Found by the §31.1 evaluation, not by reading the code. qwen2.5-coder:7b
    planned "Locate the add function" with `files: ["calc.py"]` — the file is
    where it intends to *look*. Under the old ordering that became a change
    step carrying `file_changed` + `git_diff_reviewed`, so a plan's opening
    orientation step demanded a patch and a diff review; and because evidence
    is scoped per step, every later step had to earn the pair again.
    """

    def test_the_observed_planner_output(self) -> None:
        for title, files in (
            ("Locate the add function", ("calc.py",)),
            ("Locate the slugify function", ("slug.py",)),
            ("Review the login handler", ("auth/views.py",)),
            ("Understand the failing assertion", ("test_temperature.py",)),
        ):
            assert effective_kind(proposal(title, files=files)) == "investigate", title

    def test_a_mutating_verb_still_wins_over_everything(self) -> None:
        """The same eval run: these were correctly left as change steps."""
        for title, files in (
            ("Create a new test file", ("test_slug.py",)),
            ("Write unit tests for the slugify function", ("test_slug.py",)),
            ("Fix the add function", ("calc.py",)),
        ):
            assert effective_kind(proposal(title, files=files)) == "change", title

    def test_files_still_decide_when_the_title_says_nothing(self) -> None:
        assert effective_kind(proposal("Step two", files=("calc.py",))) == "change"


class TestChangeStepsSurvive:
    def test_a_titleless_step_naming_files_is_a_change(self) -> None:
        assert effective_kind(proposal("Milestone 2", files=("auth/views.py",))) == "change"

    def test_a_mutating_verb_anywhere_settles_it(self) -> None:
        for title in (
            "Add a health check endpoint",
            "Fix the off-by-one in the parser",
            "Understand and fix the login bug",
            "Review the tests, then update the assertions",
            "Refactor the planner",
            "Create the migration",
            "Remove the dead branch",
        ):
            assert effective_kind(proposal(title)) == "change", title

    def test_an_unrecognised_title_stays_a_change(self) -> None:
        """The default is unchanged for anything this does not recognise."""
        assert effective_kind(proposal("Milestone 3")) == "change"


class TestNounsAreNotVerbs:
    """Stem matching claimed nouns as mutating verbs; whole words do not.

    Each of these begins with an investigative verb and mentions a noun that
    shares a stem with a change verb. Reading the noun as a verb rebuilds the
    unsatisfiable gate on exactly the read-only steps a planner proposes most.
    """

    def test_noun_forms_do_not_force_a_change_step(self) -> None:
        for title in (
            "Read the configuration",
            "Review the changes",
            "Examine the patch format",
            "Understand the build pipeline",
            "Review the updates in the changelog",
            "Inspect the migration history",
            "Analyse the test fixtures",
            "Identify a fix for the parser",
        ):
            assert effective_kind(proposal(title)) == "investigate", title

    def test_the_same_word_as_a_verb_still_forces_a_change_step(self) -> None:
        """The determiner is what disambiguates, so its absence must still bite."""
        for title in (
            "Review the code and patch the parser",
            "Understand the flow, then build the endpoint",
            "Examine the module and fix the import",
        ):
            assert effective_kind(proposal(title)) == "change", title

    def test_a_described_behaviour_is_not_an_intent(self) -> None:
        """Non-initial `-s` forms describe existing code; they do not command."""
        for title in (
            "Analyse how the planner builds a frame",
            "Understand what the compiler generates",
            "Review where the gateway adds evidence",
        ):
            assert effective_kind(proposal(title)) == "investigate", title

    def test_an_explicit_investigate_is_honoured(self) -> None:
        step = proposal("Add a feature", kind="investigate")
        assert effective_kind(step) == "investigate"


class TestMaterialisation:
    def build(self, step: PlanStepProposal) -> object:
        plan = ImplementationPlan(summary="s", steps=(step,))
        return materialise(TaskId("t"), plan)

    def test_a_reclassified_step_loses_the_change_floor(self) -> None:
        built = self.build(proposal("Understand the task"))
        record = built.steps[0]  # type: ignore[attr-defined]
        assert not CHANGE_FLOOR & set(record.required_evidence)

    def test_a_reclassified_step_loses_its_mutating_tools(self) -> None:
        built = self.build(proposal("Understand the task"))
        record = built.steps[0]  # type: ignore[attr-defined]
        assert record.allowed_tools == READ_ONLY_TOOLS
        assert "file.patch" not in record.allowed_tools

    def test_the_reclassification_is_reported_not_silent(self) -> None:
        built = self.build(proposal("Understand the task"))
        assert built.reclassified == ("Understand the task",)  # type: ignore[attr-defined]

    def test_a_real_change_step_keeps_the_floor(self) -> None:
        built = self.build(proposal("Fix the parser", files=("parser.py",)))
        record = built.steps[0]  # type: ignore[attr-defined]
        assert set(record.required_evidence) >= CHANGE_FLOOR
        assert record.allowed_tools == CHANGE_TOOLS

    def test_nothing_is_reclassified_when_nothing_needed_it(self) -> None:
        built = self.build(proposal("Fix the parser", files=("parser.py",)))
        assert built.reclassified == ()  # type: ignore[attr-defined]

    def test_an_investigate_step_cannot_require_proof_of_writing(self) -> None:
        """A self-contradictory proposal must not become an unsatisfiable gate.

        The model declared the step read-only and *also* asked for evidence
        that a file changed. Dropping the requirement is the only coherent
        reading: upgrading the step instead would let prose earn write tools.
        """
        step = proposal(
            "Review the tests",
            kind="investigate",
            required_evidence=("the file is changed", "the diff is reviewed"),
        )
        built = self.build(step)
        record = built.steps[0]  # type: ignore[attr-defined]
        assert EvidenceKind.FILE_CHANGED not in record.required_evidence
        assert EvidenceKind.GIT_DIFF_REVIEWED not in record.required_evidence

    def test_a_raised_bar_still_survives_reclassification(self) -> None:
        """Only the change floor is dropped; other requirements are kept."""
        step = proposal("Review the suite", required_evidence=("the tests pass",))
        built = self.build(step)
        record = built.steps[0]  # type: ignore[attr-defined]
        assert EvidenceKind.TESTS_PASSED in record.required_evidence


class TestValidationExplainsIt:
    def test_a_reclassified_step_is_noted(self) -> None:
        plan = ImplementationPlan(summary="s", steps=(proposal("Understand the task"),))
        validation = validate_plan(plan)
        assert validation.ok
        assert any("read-only" in note for note in validation.notes)
