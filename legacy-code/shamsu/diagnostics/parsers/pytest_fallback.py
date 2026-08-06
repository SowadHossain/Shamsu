"""Small fallback parser for plain-text pytest failure output.

Prefer pytest's own structured output (`--junitxml`, `pytest-json-report`)
via `adapters/native_json.py` when the command already requested it. This
parser only handles the default plain-text summary pytest prints when no
structured output was requested.
"""
from __future__ import annotations

import re

from shamsu.diagnostics.types import DiagnosticRecord

# FAILED tests/test_foo.py::test_bar - AssertionError: expected 1, got 2
SUMMARY_FAILURE_RE = re.compile(
    r"^(?:FAILED|ERROR)\s+(?P<nodeid>\S+)(?:\s+-\s+(?P<message>.+))?$"
)
FRAME_RE = re.compile(r'^(?P<file>[\w./\\-]+\.py):(?P<line>\d+):\s*(?P<func>\S*)')


def parse_pytest_failures(text: str) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    lines = text.splitlines()
    seen_nodeids: set[str] = set()

    for index, line in enumerate(lines):
        match = SUMMARY_FAILURE_RE.match(line.strip())
        if not match:
            continue
        nodeid = match.group("nodeid")
        if nodeid in seen_nodeids:
            continue
        seen_nodeids.add(nodeid)
        file_path, test_name = _split_nodeid(nodeid)
        message = (match.group("message") or "").strip()
        frame = _find_user_frame(lines, index, file_path)
        records.append(
            DiagnosticRecord(
                tool="pytest",
                language="python",
                severity="error",
                category="test_failure",
                message=message or "Test failed",
                file=frame[0] if frame else file_path,
                line=frame[1] if frame else None,
                symbol=test_name,
                raw_excerpt=line.strip(),
                parser_name="pytest_fallback",
                confidence=0.8,
            )
        )
    return records


def _split_nodeid(nodeid: str) -> tuple[str, str]:
    if "::" in nodeid:
        file_path, _, test_name = nodeid.partition("::")
        return file_path, test_name
    return nodeid, ""


def _find_user_frame(lines: list[str], summary_index: int, test_file: str) -> tuple[str, int] | None:
    """Pytest prints the short summary section last; the traceback for a
    given test appears earlier in the output, so scan backward from the
    summary line for the deepest frame inside the failing test file."""
    for line in reversed(lines[:summary_index]):
        match = FRAME_RE.match(line.strip())
        if match and test_file and test_file in match.group("file"):
            return match.group("file"), int(match.group("line"))
    return None
