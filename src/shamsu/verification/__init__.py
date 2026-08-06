"""Evidence collection and the verification pipeline.

The model may propose that a step is complete. This package decides whether the
required evidence actually exists.

    required_evidence ⊆ verified_evidence

`EvidenceRecorder` is where a gateway's *report* of evidence becomes a
persisted fact: only a row in `evidence`, keyed to a real `tool_events` row,
counts. There is no path through this package by which a model assertion
becomes a row.

`digest` handles the other half of verification — turning thousands of lines of
test output into something that fits an observation budget, and computing a
stable error signature so a repair loop can tell "same failure again" from
"different failure, still trying".

Milestone 5. See plan sections 25, 26, 27.
"""

from shamsu.verification.digest import (
    RepairTracker,
    TestDigest,
    digest_test_output,
    error_signature,
)
from shamsu.verification.evidence import (
    CLAIM_REQUIREMENTS,
    EvidenceRecorder,
    GateResult,
    Recorded,
    check_completion,
    requirements_for,
)

__all__ = [
    "CLAIM_REQUIREMENTS",
    "EvidenceRecorder",
    "GateResult",
    "Recorded",
    "RepairTracker",
    "TestDigest",
    "check_completion",
    "digest_test_output",
    "error_signature",
    "requirements_for",
]
