"""Test-output digesting and error signatures.

A pytest failure can run to thousands of lines. A frame has ~700 tokens for
observations. Something has to decide what survives, and "the last N lines"
is the wrong answer — the summary is at the end, but the *cause* is in the
middle.

The error signature is the other half. Two repair attempts that produce the
same signature are not making progress, and that is what stops a repair loop
from grinding through its whole budget on the same failure. The signature must
therefore be stable across runs: it hashes the failure's *identity* (test name,
exception type, assertion shape) and deliberately not its incidental detail
(temp paths, addresses, durations, line numbers that shift with every edit).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

#: Volatile fragments removed before signing. Each of these changes between
#: otherwise-identical runs, and leaving any one in makes every attempt look
#: like new information.
_VOLATILE = (
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"/tmp/[^\s'\"]+"), "TMP"),
    (re.compile(r"[A-Za-z]:\\\\[^\s'\"]+"), "TMP"),
    (re.compile(r"pytest-of-[^\s/'\"]+"), "PYTEST"),
    (re.compile(r"\bin \d+\.\d+s\b"), "in TIMEs"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*"), "TIMESTAMP"),
    (re.compile(r":\d+:"), ":LINE:"),
    (re.compile(r"\bline \d+\b"), "line LINE"),
)

_FAILED_LINE = re.compile(r"^(FAILED|ERROR)\s+(?P<test>\S+)", re.M)
_SUMMARY = re.compile(
    r"^=+\s*(?P<body>.*?(?:passed|failed|error|no tests ran).*?)\s*=+$", re.M | re.I
)
_ASSERTION = re.compile(r"^E\s+(?P<body>.+)$", re.M)
_TRACEBACK_FRAME = re.compile(r"^(?P<path>[^\s:]+\.py):(?P<line>\d+):", re.M)


@dataclass(frozen=True)
class TestDigest:
    """A compact, model-readable account of a test run."""

    passed: bool
    summary: str
    failed_tests: tuple[str, ...] = ()
    assertions: tuple[str, ...] = ()
    frames: tuple[str, ...] = ()
    signature: str = ""
    truncated_from: int = 0

    def render(self, max_assertions: int = 5, max_tests: int = 10) -> str:
        """The text that goes into an observation."""
        if self.passed:
            return self.summary or "All tests passed."

        parts: list[str] = [self.summary or "Tests failed."]

        if self.failed_tests:
            shown = list(self.failed_tests[:max_tests])
            extra = len(self.failed_tests) - len(shown)
            listed = "\n".join(f"  {name}" for name in shown)
            more = f"\n  … and {extra} more" if extra > 0 else ""
            parts.append(f"Failed:\n{listed}{more}")

        if self.assertions:
            shown = list(self.assertions[:max_assertions])
            listed = "\n".join(f"  {line}" for line in shown)
            parts.append(f"Assertions:\n{listed}")

        if self.frames:
            parts.append("Where:\n" + "\n".join(f"  {frame}" for frame in self.frames[:5]))

        return "\n\n".join(parts)


def error_signature(text: str, *, failed_tests: tuple[str, ...] = ()) -> str:
    """A stable identity for a failure.

    Built from the failing test names plus the normalised assertion text. Two
    runs of the same broken code produce the same signature; a genuinely
    different failure produces a different one. That distinction is the whole
    basis of "stop repairing, this is not working".
    """
    assertions = [match.group("body").strip() for match in _ASSERTION.finditer(text)]
    material = "\n".join([*sorted(failed_tests), *assertions[:5]])

    if not material.strip():
        # Nothing structured to sign — fall back to the whole normalised text
        # rather than returning an empty signature that would collide with
        # every other unstructured failure.
        material = text

    for pattern, replacement in _VOLATILE:
        material = pattern.sub(replacement, material)

    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def digest_test_output(
    stdout: str, stderr: str, *, exit_code: int, max_chars: int = 60_000
) -> TestDigest:
    """Reduce a test run to what a model can act on.

    `exit_code` decides pass/fail, not the text. A runner that crashes before
    printing a summary still failed, and inferring success from the absence of
    the word "failed" is how a broken run gets reported as green.
    """
    combined = f"{stdout}\n{stderr}".strip()
    truncated_from = 0
    if len(combined) > max_chars:
        truncated_from = len(combined)
        # Keep both ends: the summary is at the end, the cause is usually
        # nearer the start.
        head = combined[: max_chars // 2]
        tail = combined[-max_chars // 2 :]
        combined = f"{head}\n… [middle omitted] …\n{tail}"

    passed = exit_code == 0

    # The LAST summary line is the authoritative one: a run can print an
    # intermediate summary per test session, and the final tally is what counts.
    summaries = _SUMMARY.findall(combined)
    summary = summaries[-1].strip() if summaries else ""

    if not summary:
        summary = "Tests passed." if passed else f"Test command exited with code {exit_code}."

    failed_tests = tuple(
        dict.fromkeys(match.group("test") for match in _FAILED_LINE.finditer(combined))
    )
    assertions = tuple(
        dict.fromkeys(match.group("body").strip() for match in _ASSERTION.finditer(combined))
    )
    frames = tuple(
        dict.fromkeys(
            f"{match.group('path')}:{match.group('line')}"
            for match in _TRACEBACK_FRAME.finditer(combined)
        )
    )

    return TestDigest(
        passed=passed,
        summary=summary,
        failed_tests=failed_tests,
        assertions=assertions,
        frames=frames,
        signature="" if passed else error_signature(combined, failed_tests=failed_tests),
        truncated_from=truncated_from,
    )


@dataclass
class RepairTracker:
    """Detects a repair loop that is not making progress.

    Same-signature detection, kept out of the agent so it is testable on its
    own. v1 buried this logic in the loop where nobody could exercise it.
    """

    threshold: int = 2
    seen: list[str] = field(default_factory=list)

    def record(self, signature: str) -> None:
        if signature:
            self.seen.append(signature)

    def is_stuck(self) -> bool:
        """Whether the last `threshold` attempts produced the same failure."""
        if len(self.seen) < self.threshold:
            return False
        recent = self.seen[-self.threshold :]
        return len(set(recent)) == 1

    @property
    def attempts(self) -> int:
        return len(self.seen)


__all__ = [
    "RepairTracker",
    "TestDigest",
    "digest_test_output",
    "error_signature",
]
