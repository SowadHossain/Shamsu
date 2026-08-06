"""Compare the error state before and after a patch.

The verifier's exit code and the diagnostic error set together decide whether
a patch helped, hurt, or moved sideways. This is what lets the repair loop
keep good patches and roll back bad ones instead of trusting the model's
self-assessment.
"""
from __future__ import annotations

from collections import Counter
from enum import Enum

from shamsu.repair.kinds import RepairError


class RepairOutcome(str, Enum):
    SOLVED = "SOLVED"        # verifier passes, no errors remain
    IMPROVED = "IMPROVED"    # strictly fewer errors than before
    ADVANCED = "ADVANCED"    # same test advanced to a later failing line
    UNCHANGED = "UNCHANGED"  # identical error set
    WORSE = "WORSE"          # more errors than before
    DIFFERENT = "DIFFERENT"  # same count, different errors (lateral move)


def _signatures(errors: list[RepairError]) -> Counter[str]:
    return Counter(e.signature() for e in errors)


class ErrorComparator:
    def compare(
        self,
        before: list[RepairError],
        after: list[RepairError],
        after_exit_code: int,
    ) -> RepairOutcome:
        after_sigs = _signatures(after)
        # SOLVED requires ground truth: the verifier exited clean AND no
        # structured errors remain. Neither alone is sufficient.
        if after_exit_code == 0 and not after:
            return RepairOutcome.SOLVED

        before_sigs = _signatures(before)
        before_count = sum(before_sigs.values())
        after_count = sum(after_sigs.values())

        if after_count > before_count:
            return RepairOutcome.WORSE
        if after_count < before_count:
            return RepairOutcome.IMPROVED
        if after_sigs == before_sigs:
            return RepairOutcome.UNCHANGED
        if _same_test_advanced(before, after):
            return RepairOutcome.ADVANCED
        return RepairOutcome.DIFFERENT

    @staticmethod
    def is_progress(outcome: RepairOutcome) -> bool:
        """Outcomes worth keeping the patch for and continuing."""
        return outcome in (
            RepairOutcome.SOLVED,
            RepairOutcome.IMPROVED,
            RepairOutcome.ADVANCED,
        )


def _same_test_advanced(before: list[RepairError], after: list[RepairError]) -> bool:
    """A later failure in the same test is sequential progress, not a lateral move."""
    if len(before) != 1 or len(after) != 1:
        return False
    old, new = before[0], after[0]
    old_file = old.file.replace("\\", "/").lower()
    new_file = new.file.replace("\\", "/").lower()
    test_file = "/test" in old_file or ".test." in old_file or ".spec." in old_file
    same_test_symbol = bool(old.symbol and old.symbol == new.symbol)
    return bool(
        test_file
        and same_test_symbol
        and old_file == new_file
        and old.line is not None
        and new.line is not None
        and new.line > old.line
    )
