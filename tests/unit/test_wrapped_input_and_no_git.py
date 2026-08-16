"""Two failures from one live session, neither of which the model caused.

**The prompt duplicated itself down the screen.** `_input` returned one string
however long the typed request was. The terminal wrapped it, every row below
shifted by the overflow, and the old rows were never cleared — typing past the
first line redrew the prompt once per keystroke, eight copies deep.

**A plain folder could never finish a change.** `git.inspect` and
`git.checkpoint` fail with *"not a git repository"* outside one, so
`GIT_DIFF_REVIEWED` is unproducible and the change floor is a gate with no key.
The run spent both repair attempts on `git.checkpoint`, failed identically each
time, and blocked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agent.planning import (
    CHANGE_FLOOR,
    coalesce_by_file,
    evidence_floor,
    materialise,
)
from shamsu.interfaces.enums import EvidenceKind, Risk
from shamsu.interfaces.ids import TaskId
from shamsu.models.contracts import ImplementationPlan, PlanStepProposal
from shamsu.runtime.session import is_git_repository
from shamsu.ui.session_frame import SessionState, input_rows, input_width, render_session

LONG = (
    "can you make me a plan to build me this system that is stated in the prd "
    "@OpenBazaar_Marketplace_PRD.docx , it has all the details you need to"
)


def _state(text: str = "") -> SessionState:
    state = SessionState(workspace="/tmp/x", model="m")
    state.text = text
    state.cursor = len(text)
    return state


class TestTheFrameKeepsItsPromise:
    """`render_session` owes exactly `height` rows. A wrapped input broke that."""

    @pytest.mark.parametrize("height", [10, 16, 24, 40])
    def test_a_long_request_does_not_change_the_row_count(self, height: int) -> None:
        assert len(render_session(_state(LONG), 100, height, colour=False)) == height

    @pytest.mark.parametrize("width", [40, 60, 80, 100, 120])
    def test_no_row_exceeds_the_width(self, width: int) -> None:
        for line in render_session(_state(LONG), width, 20, colour=False):
            assert len(line) <= width, line

    def test_a_long_request_occupies_several_input_rows(self) -> None:
        assert len(input_rows(_state(LONG), 100)) > 1

    def test_a_short_request_still_takes_one(self) -> None:
        assert len(input_rows(_state("hi"), 100)) == 1

    def test_an_empty_prompt_takes_one(self) -> None:
        assert input_rows(_state(), 100) == [""]

    def test_wrapping_loses_no_characters(self) -> None:
        assert "".join(input_rows(_state(LONG), 100)) == LONG

    def test_the_text_is_visible_rather_than_truncated(self) -> None:
        """The point of wrapping: the user can read all of what they typed.

        Character wrap, not word wrap — so a long name straddles the boundary.
        That is deliberate: the cursor is placed with `divmod` over the buffer
        offset, and word wrapping would make that arithmetic a lie.
        """
        rendered = "\n".join(render_session(_state(LONG), 100, 20, colour=False))
        assert "the details you need to" in rendered, "the tail was lost"

        typed = "".join(
            line.lstrip(" ›") for line in rendered.splitlines() if line.startswith((" ›", "   "))
        )
        assert "OpenBazaar_Marketplace_PRD.docx" in typed.replace(" ", "")

    def test_a_narrow_window_still_leaves_room_to_type(self) -> None:
        assert input_width(40) >= 8


#: What a plain folder can prove: everything the tools declare, minus the
#: two kinds that need a repository.
_NO_GIT = CHANGE_FLOOR - {EvidenceKind.GIT_DIFF_REVIEWED}


class TestAPlainFolderCanStillFinish:
    def test_the_floor_drops_the_diff_when_git_is_absent(self) -> None:
        floor = evidence_floor("change", producible=_NO_GIT)
        assert EvidenceKind.FILE_CHANGED in floor
        assert EvidenceKind.GIT_DIFF_REVIEWED not in floor

    def test_a_repository_keeps_the_full_floor(self) -> None:
        assert evidence_floor("change", producible=CHANGE_FLOOR) == CHANGE_FLOOR
        assert evidence_floor("change") == CHANGE_FLOOR

    def test_an_investigate_step_is_unaffected(self) -> None:
        assert evidence_floor("investigate", producible=_NO_GIT) == frozenset()

    def test_a_plan_materialised_without_git_is_satisfiable(self) -> None:
        """The whole point: something must be able to open this gate."""
        plan = ImplementationPlan(
            summary="s",
            steps=(PlanStepProposal(title="Fix the parser", files=("parser.py",)),),
        )
        built = materialise(TaskId("t"), plan, producible=_NO_GIT)
        assert set(built.steps[0].required_evidence) == {EvidenceKind.FILE_CHANGED}

    def test_detection_is_by_the_git_directory(self, tmp_path: Path) -> None:
        assert is_git_repository(tmp_path) is False
        (tmp_path / ".git").mkdir()
        assert is_git_repository(tmp_path) is True


class TestOneFileIsOneStep:
    """A live build finished the file and still reported NOT COMPLETE.

    The 7B was asked for a `TaskList` class and planned four steps — define the
    class, implement `add`, implement `all`, implement `complete` — every one
    of them targeting `tasks.py`. It then wrote all three methods correctly in
    the first step, because a whole file is what it produces per turn. Steps 3,
    4 and 5 had nothing left to do and each still owed its own `FILE_CHANGED`,
    so the task ended unfinished with finished code on disk.
    """

    @staticmethod
    def _step(title: str, files: tuple[str, ...], **extra: object) -> PlanStepProposal:
        return PlanStepProposal.model_validate({"title": title, "files": files, **extra})

    def test_adjacent_steps_on_one_file_become_one(self) -> None:
        merged = coalesce_by_file(
            (
                self._step("Define the TaskList class", ("tasks.py",)),
                self._step("Implement the add method", ("tasks.py",)),
                self._step("Implement the all method", ("tasks.py",)),
            )
        )
        assert len(merged) == 1
        assert merged[0].files == ("tasks.py",)

    def test_the_merged_title_still_names_every_part(self) -> None:
        """Nothing silently disappears — the run log has to stay legible."""
        merged = coalesce_by_file(
            (
                self._step("Define the class", ("t.py",)),
                self._step("Implement add", ("t.py",)),
            )
        )
        assert merged[0].title == "Define the class; implement add"

    def test_different_files_are_left_alone(self) -> None:
        merged = coalesce_by_file(
            (
                self._step("Write the model", ("model.py",)),
                self._step("Write the view", ("view.py",)),
            )
        )
        assert len(merged) == 2

    def test_a_step_in_between_breaks_the_run(self) -> None:
        """Merging across an unrelated step would reorder the work."""
        merged = coalesce_by_file(
            (
                self._step("Edit the model", ("model.py",)),
                self._step("Edit the view", ("view.py",)),
                self._step("Edit the model again", ("model.py",)),
            )
        )
        assert len(merged) == 3

    def test_an_investigation_is_never_absorbed(self) -> None:
        """It holds read-only tools; merging it into a write would grant more."""
        merged = coalesce_by_file(
            (
                self._step("Review tasks.py", ("tasks.py",)),
                self._step("Fix the bug in tasks.py", ("tasks.py",)),
            )
        )
        assert len(merged) == 2

    def test_steps_with_no_declared_file_are_never_merged(self) -> None:
        """Empty files tuples are not evidence of a shared target."""
        merged = coalesce_by_file((self._step("Do a thing", ()), self._step("Do another", ())))
        assert len(merged) == 2

    def test_the_union_of_required_evidence_survives(self) -> None:
        """Merging must not lower the bar — the same proof is owed, once."""
        merged = coalesce_by_file(
            (
                self._step("Write it", ("t.py",), required_evidence=("tests pass",)),
                self._step("Lint it", ("t.py",), required_evidence=("lint passes",)),
            )
        )
        assert set(merged[0].required_evidence) == {"tests pass", "lint passes"}

    def test_the_merged_step_carries_the_higher_risk(self) -> None:
        """`Risk` is a StrEnum, so a naive max() would rank "high" below "low".

        A proposal may only claim low/medium/high — `critical` is the runtime's
        to assign — so "high" is the top a merge can reach from here.
        """
        merged = coalesce_by_file(
            (
                self._step("Write it", ("t.py",), risk="low"),
                self._step("Migrate it", ("t.py",), risk="high"),
            )
        )
        assert merged[0].risk == "high"


class TestRiskCannotExceedWhatAStepCanDo:
    """A headless build died authorising a read.

    The model labelled *"Check for existing storage.py file"* high risk. High
    risk demands approval, a headless run has no approver, and the task stopped
    — to authorise a step holding nothing but read-only tools.
    """

    @staticmethod
    def _plan(title: str, risk: str) -> ImplementationPlan:
        return ImplementationPlan(
            summary="s",
            steps=(
                PlanStepProposal.model_validate(
                    {"title": title, "files": ("storage.py",), "risk": risk}
                ),
            ),
        )

    def test_an_investigation_is_capped_and_needs_no_approval(self) -> None:
        step = materialise(TaskId("t"), self._plan("Check for existing storage.py", "high")).steps[
            0
        ]
        assert step.risk is Risk.LOW
        assert step.approval_required is False

    def test_a_real_change_keeps_the_risk_it_declared(self) -> None:
        """The cap only ever lowers, and only where the runtime can prove it."""
        step = materialise(TaskId("t"), self._plan("Rewrite storage.py", "high")).steps[0]
        assert step.risk is Risk.HIGH
        assert step.approval_required is True
