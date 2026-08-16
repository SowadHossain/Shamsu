"""A step that names only the specification must still find its real target.

The first prompt of a fresh OpenBazaar build blocked without running a single
tool:

    Create manage.py at the workspace root: the standard Django command-line
    utility that sets DJANGO_SETTINGS_MODULE to openbazaar.settings ...

    ■ failed  plan rejected repeatedly: these steps would change something but
              name no file

The workspace held exactly one file — `OpenBazaar_Marketplace_PRD.docx` — so
that is the only name the model had seen, and it is what the step listed in
`files`. Two correct transforms then composed into a wrong one:

* `strip_documents` removes the specification, because `file.patch` cannot edit
  a zip archive and `coalesce_by_file` must not merge every step that cites it.
* `recover_named_files` fills an **empty** `files` from the step's own title.

Composed as ``strip_documents(recover_named_files(plan))`` the recovery ran
first, saw a non-empty `files`, and declined to help; stripping then emptied
the field, and validation refused a step that had named `manage.py` in its own
title all along. Each function was right and the order was wrong — so the order
is the thing under test here, not either function alone.
"""

from __future__ import annotations

from shamsu.agent.planning import (
    drop_unexecutable_steps,
    recover_named_files,
    strip_documents,
)
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal

#: Verbatim from the run, truncated the way the planner truncates it.
TITLE = (
    "This workspace will hold OpenBazaar, a Django marketplace described in "
    "OpenBazaar_Marketplace_PRD.docx. Begin the scaffold. Create manage.py at "
    "the workspace root: the standard Django command-line uti"
)


def _plan(*, files: tuple[str, ...], title: str = TITLE) -> ImplementationPlan:
    return ImplementationPlan(
        summary="Scaffold the project.",
        steps=(PlanStepProposal(title=title, kind="change", files=files),),
    )


def _pipeline(plan: ImplementationPlan) -> ImplementationPlan:
    """The composition `Planner._persist` applies, kept in one place."""
    return recover_named_files(strip_documents(plan))


class TestTheOrderOfTheTwoTransforms:
    def test_a_step_naming_only_the_spec_recovers_its_real_target(self) -> None:
        """The headline: this exact shape used to block the run."""
        plan = _pipeline(_plan(files=("OpenBazaar_Marketplace_PRD.docx",)))
        assert plan.steps[0].files == ("manage.py",)

    def test_the_specification_is_still_removed(self) -> None:
        plan = _pipeline(_plan(files=("OpenBazaar_Marketplace_PRD.docx", "manage.py")))
        assert "OpenBazaar_Marketplace_PRD.docx" not in plan.steps[0].files
        assert "manage.py" in plan.steps[0].files

    def test_recovery_cannot_reintroduce_the_document(self) -> None:
        """Recovery reads the title, and the title names the `.docx` too.

        Filtering what it recovers is what makes the composition safe in either
        order, rather than merely safe in the order written today.
        """
        recovered = recover_named_files(_plan(files=()))
        assert recovered.steps[0].files == ("manage.py",)

    def test_a_step_that_named_a_real_file_is_untouched(self) -> None:
        plan = _pipeline(_plan(files=("config/settings.py",), title="Add the settings module"))
        assert plan.steps[0].files == ("config/settings.py",)

    def test_a_step_with_no_filename_anywhere_still_names_nothing(self) -> None:
        """Recovery must not invent a target; the drop pass then removes it."""
        plan = _pipeline(_plan(files=("OpenBazaar_Marketplace_PRD.docx",), title="Develop Backend"))
        assert plan.steps[0].files == ()


class TestFillerStepsDoNotSinkTheWholePlan:
    """The second prompt of the same build, blocked by one procedural non-step.

        ■ failed  plan rejected repeatedly: these steps would change something
                  but name no file: Navigate to the project directory

    A perfectly executable "create openbazaar/settings.py" was thrown away
    alongside it, because rejection is all-or-nothing.
    """

    def _mixed(self) -> ImplementationPlan:
        return ImplementationPlan(
            summary="Set up the settings module.",
            steps=(
                PlanStepProposal(title="Navigate to the project directory", kind="change"),
                PlanStepProposal(
                    title="Create the settings module",
                    kind="change",
                    files=("openbazaar/settings.py",),
                ),
            ),
        )

    def test_the_executable_step_survives(self) -> None:
        plan, _dropped = drop_unexecutable_steps(self._mixed())
        assert [step.title for step in plan.steps] == ["Create the settings module"]

    def test_what_was_dropped_is_reported(self) -> None:
        """Silently discarding a proposed step would be its own kind of lie."""
        _plan_out, dropped = drop_unexecutable_steps(self._mixed())
        assert dropped == ("Navigate to the project directory",)

    def test_an_investigate_step_without_files_is_kept(self) -> None:
        """Only *change* steps owe a filename; reading needs no target."""
        plan = ImplementationPlan(
            summary="Look first.",
            steps=(
                PlanStepProposal(title="Read the specification", kind="investigate"),
                PlanStepProposal(title="Write it", kind="change", files=("app.py",)),
            ),
        )
        kept, dropped = drop_unexecutable_steps(plan)
        assert dropped == ()
        assert len(kept.steps) == 2

    def test_a_plan_of_nothing_but_filler_is_left_for_validation_to_refuse(self) -> None:
        """Dropping everything would run zero steps and call it a plan.

        `validate_plan` must still see the empty-handed plan and reject it, so
        the model is told to name files rather than handed a silent no-op.
        """
        plan = ImplementationPlan(
            summary="Vibes.",
            steps=(
                PlanStepProposal(title="Navigate to the project directory", kind="change"),
                PlanStepProposal(title="Open a terminal", kind="change"),
            ),
        )
        kept, dropped = drop_unexecutable_steps(plan)
        assert dropped == ()
        assert len(kept.steps) == 2
