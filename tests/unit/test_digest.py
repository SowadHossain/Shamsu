"""Test-output digesting and error signatures.

The signature is what lets a repair loop tell "same failure again" from
"different failure, still trying". It has to be stable across runs of the same
broken code and different for genuinely different failures — those two
properties are the entire basis of bounded repair.
"""

from __future__ import annotations

from shamsu.verification import (
    RepairTracker,
    digest_test_output,
    error_signature,
)

FAILING = """\
=================================== FAILURES ===================================
_________________________________ test_add _____________________________________
tests/test_calc.py:5: in test_add
    assert add(2, 3) == 5
E   assert -1 == 5
E    +  where -1 = add(2, 3)
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_add - assert -1 == 5
========================= 1 failed, 3 passed in 0.12s ==========================
"""

PASSING = "========================= 4 passed in 0.09s =========================\n"


class TestDigesting:
    def test_a_passing_run_is_recognised(self) -> None:
        digest = digest_test_output(PASSING, "", exit_code=0)
        assert digest.passed is True
        assert "4 passed" in digest.summary
        assert digest.signature == ""

    def test_exit_code_decides_not_the_text(self) -> None:
        """A runner that crashes before printing a summary still failed.

        Inferring success from the absence of the word 'failed' is how a
        broken run gets reported green.
        """
        digest = digest_test_output("Traceback...\n", "", exit_code=2)
        assert digest.passed is False

    def test_a_success_message_cannot_fake_a_pass(self) -> None:
        digest = digest_test_output("everything passed, honest!", "", exit_code=1)
        assert digest.passed is False

    def test_failed_tests_are_extracted(self) -> None:
        digest = digest_test_output(FAILING, "", exit_code=1)
        assert digest.failed_tests == ("tests/test_calc.py::test_add",)

    def test_assertions_are_extracted(self) -> None:
        digest = digest_test_output(FAILING, "", exit_code=1)
        assert any("assert -1 == 5" in line for line in digest.assertions)

    def test_the_render_is_compact_and_actionable(self) -> None:
        """A frame has ~700 tokens for observations."""
        rendered = digest_test_output(FAILING, "", exit_code=1).render()
        assert "test_add" in rendered
        assert "assert -1 == 5" in rendered
        assert len(rendered) < len(FAILING)

    def test_huge_output_keeps_both_ends(self) -> None:
        """The summary is at the end; the cause is nearer the start."""
        noise = "\n".join(f"line {i}" for i in range(20_000))
        digest = digest_test_output(f"START-MARKER\n{noise}\nEND-MARKER", "", exit_code=1)
        assert digest.truncated_from > 0


class TestErrorSignatures:
    def test_the_same_failure_signs_the_same(self) -> None:
        first = digest_test_output(FAILING, "", exit_code=1)
        second = digest_test_output(FAILING, "", exit_code=1)
        assert first.signature == second.signature

    def test_a_different_failure_signs_differently(self) -> None:
        other = FAILING.replace("assert -1 == 5", "assert 7 == 5").replace(
            "test_add", "test_multiply"
        )
        assert (
            digest_test_output(FAILING, "", exit_code=1).signature
            != digest_test_output(other, "", exit_code=1).signature
        )

    def test_temp_paths_do_not_change_the_signature(self) -> None:
        """Otherwise every attempt looks like new information."""
        a = FAILING + "\n/tmp/pytest-of-root/pytest-1/test_add0/x.py\n"
        b = FAILING + "\n/tmp/pytest-of-root/pytest-99/test_add0/x.py\n"
        assert error_signature(a) == error_signature(b)

    def test_durations_do_not_change_the_signature(self) -> None:
        a = FAILING
        b = FAILING.replace("in 0.12s", "in 9.87s")
        assert error_signature(a) == error_signature(b)

    def test_shifted_line_numbers_do_not_change_the_signature(self) -> None:
        """Line numbers move with every edit above the failure."""
        a = "E   assert x\ntests/t.py:5: in test_x"
        b = "E   assert x\ntests/t.py:97: in test_x"
        assert error_signature(a) == error_signature(b)

    def test_unstructured_output_still_gets_a_signature(self) -> None:
        """An empty signature would collide with every other opaque failure."""
        assert error_signature("segmentation fault") != ""
        assert error_signature("segmentation fault") != error_signature("bus error")


class TestRepairTracker:
    def test_one_attempt_is_not_stuck(self) -> None:
        tracker = RepairTracker()
        tracker.record("abc")
        assert tracker.is_stuck() is False

    def test_the_same_signature_twice_is_stuck(self) -> None:
        """Two identical failures mean the attempts are not making progress."""
        tracker = RepairTracker()
        tracker.record("abc")
        tracker.record("abc")
        assert tracker.is_stuck() is True

    def test_a_changing_signature_is_progress(self) -> None:
        tracker = RepairTracker()
        tracker.record("abc")
        tracker.record("def")
        assert tracker.is_stuck() is False
        assert tracker.attempts == 2

    def test_recovering_after_a_repeat_is_not_stuck(self) -> None:
        """Only the most recent attempts count, not the whole history."""
        tracker = RepairTracker()
        tracker.record("abc")
        tracker.record("abc")
        tracker.record("xyz")
        assert tracker.is_stuck() is False

    def test_empty_signatures_are_ignored(self) -> None:
        tracker = RepairTracker()
        tracker.record("")
        tracker.record("")
        assert tracker.attempts == 0
        assert tracker.is_stuck() is False
