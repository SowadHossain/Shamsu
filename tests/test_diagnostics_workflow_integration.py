from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from shamsu.agents.error_feedback_loop import ErrorFeedbackLoop
from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.diagnostics.types import ErrorPacket
from shamsu.tools.executor import CommandRunner
from shamsu.types import TestRunResult as ShamsuTestRunResult


class RecordingDigest:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def run(self, command, cwd, exit_code, stdout, stderr, raw_log_path=""):
        self.calls.append((command, cwd, exit_code, stdout, stderr, raw_log_path))
        return ErrorPacket(command=command, cwd=str(cwd), exit_code=exit_code, summary="fake summary")


class SequenceTestRunner:
    def __init__(self, results: list[ShamsuTestRunResult]) -> None:
        self.results = results
        self.calls = 0

    def run(self, project_cwd: Path | str = ".") -> ShamsuTestRunResult:
        self.calls += 1
        return self.results[min(self.calls - 1, len(self.results) - 1)]


class FakeBugFixWorkflow:
    def __init__(self, applied: bool = True) -> None:
        self.applied = applied
        self.reports: list[str] = []

    async def run(self, report: str):
        self.reports.append(report)
        return _FixResult(self.applied)


class _FixResult:
    def __init__(self, applied: bool) -> None:
        self.applied = applied
        self.changed_files = ["app/views.py"] if applied else []
        self.error = "" if applied else "Patch denied"


class EmptySearch:
    def search(self, query: str, top_k: int = 5, boost_paths=None):
        return []

    def symbol_lookup(self, name: str):
        return []

    def fts_search(self, query: str, top_k: int = 5):
        return []


# -- 27. CommandRunner calls DiagnosticDigest after a command -----------------

def test_command_runner_calls_diagnostic_digest_after_successful_command(tmp_path: Path):
    digest = RecordingDigest()
    runner = CommandRunner(tmp_path, approval_func=lambda _r: True, diagnostic_digest=digest)

    runner.run("python -c \"print('hi')\"", tmp_path)

    assert len(digest.calls) == 1
    command, cwd, exit_code, _stdout, _stderr, _raw_log_path = digest.calls[0]
    assert command == "python -c \"print('hi')\""
    assert exit_code == 0
    assert runner.last_error_packet is None
    assert runner.last_diagnostic_packet is not None
    assert runner.last_diagnostic_packet.classification == "success"


def test_successful_command_uses_a_neutral_diagnostic_artifact_name(tmp_path: Path):
    from shamsu.action_ledger.ledger import start_run

    ledger = start_run(tmp_path, "run a successful command")
    runner = CommandRunner(tmp_path, approval_func=lambda _r: True, action_ledger=ledger)

    runner.run("python -c \"print('ok')\"", tmp_path)

    assert "diagnostic_packet_" in runner.last_diagnostics_path
    assert "error_packet_" not in runner.last_diagnostics_path


def test_successful_command_does_not_replace_last_error_packet(tmp_path: Path):
    digest = RecordingDigest()
    runner = CommandRunner(tmp_path, approval_func=lambda _r: True, diagnostic_digest=digest)

    runner.run("python -c \"print('hi')\"", tmp_path)

    packet_path = tmp_path / ".shamsu" / "diagnostics" / "last-error-packet.json"
    assert packet_path.exists() is False


def test_command_runner_does_not_run_diagnostics_for_blocked_commands(tmp_path: Path):
    digest = RecordingDigest()
    runner = CommandRunner(tmp_path, diagnostic_digest=digest)

    runner.run("rm -rf /", tmp_path)

    assert digest.calls == []


def test_command_runner_digest_failure_never_breaks_command_execution(tmp_path: Path):
    class BrokenDigest:
        def run(self, *args, **kwargs):
            raise RuntimeError("boom")

    runner = CommandRunner(tmp_path, approval_func=lambda _r: True, diagnostic_digest=BrokenDigest())

    code, stdout, _stderr = runner.run("python -c \"print('hi')\"", tmp_path)

    assert code == 0
    assert "hi" in stdout


def test_command_runner_replaces_undecodable_output_bytes(tmp_path: Path):
    runner = CommandRunner(tmp_path, approval_func=lambda _r: True)
    command = (
        f"\"{sys.executable}\" -c "
        "\"import sys; "
        "sys.stdout.buffer.write(bytes([0x81])); "
        "sys.stderr.buffer.write(bytes([0x82]))\""
    )

    code, stdout, stderr = runner.run(command, tmp_path)

    assert code == 0
    assert "\ufffd" in stdout
    assert "\ufffd" in stderr


# -- 28. bugfix workflow uses ErrorPacket before the LLM call -----------------

@pytest.mark.asyncio
async def test_error_feedback_loop_builds_bug_report_from_error_packet_before_bugfix_call(tmp_path: Path):
    failing = ShamsuTestRunResult(
        passed=0,
        failed=1,
        raw_output=(
            "Traceback (most recent call last):\n"
            '  File "app/views.py", line 12, in task_list\n'
            "ValueError: bad input\n"
        ),
    )
    passing = ShamsuTestRunResult(passed=1, failed=0, raw_output="OK")
    bugfix = FakeBugFixWorkflow(applied=True)

    result = await ErrorFeedbackLoop(
        tmp_path,
        search=EmptySearch(),
        test_runner=SequenceTestRunner([failing, passing]),
        bugfix_workflow=bugfix,
    ).run(tmp_path)

    assert result.success is True
    assert bugfix.reports
    report = bugfix.reports[0]
    assert "ErrorPacket for" in report
    assert "ValueError" in report
    assert "Django test feedback iteration 1 failed" in report
    assert result.iterations[0].diagnostics_summary


def test_bug_report_prefers_compact_log_over_full_raw_output_when_diagnostics_found():
    from shamsu.agents.error_feedback_loop import _bug_report_from_tests

    raw_output = (
        "npm notice noise\n"
        "Traceback (most recent call last):\n"
        '  File "app/views.py", line 12, in task_list\n'
        "ValueError: bad input\n"
    )
    result = ShamsuTestRunResult(passed=0, failed=1, raw_output=raw_output)
    digest = DiagnosticDigest(Path("."))
    packet = digest.run("python manage.py test --verbosity=2", ".", 1, "", raw_output)

    report = _bug_report_from_tests(result, 1, packet)

    assert "npm notice noise" not in report
    assert "ValueError" in report


def test_bug_report_falls_back_to_raw_output_when_no_packet_supplied():
    from shamsu.agents.error_feedback_loop import _bug_report_from_tests

    result = ShamsuTestRunResult(passed=0, failed=1, raw_output="FAILED (failures=1)")

    report = _bug_report_from_tests(result, 1, None)

    assert "FAILED (failures=1)" in report
    assert "ErrorPacket for" not in report


# -- 31. repeated same ErrorPacket triggers a stall guard ---------------------

def test_error_packet_signature_repeats_for_identical_test_output(tmp_path: Path):
    digest = DiagnosticDigest(tmp_path)
    raw_output = (
        "Traceback (most recent call last):\n"
        '  File "app/views.py", line 12, in task_list\n'
        "ValueError: bad input\n"
    )

    first = digest.run("python manage.py test --verbosity=2", tmp_path, 1, "", raw_output)
    second = digest.run("python manage.py test --verbosity=2", tmp_path, 1, "", raw_output)

    assert first.signature() == second.signature()


# -- 37. LLM is never called before/instead of deterministic parsing ---------

def test_diagnostics_package_never_imports_the_llm_layer():
    diagnostics_dir = Path(__file__).resolve().parent.parent / "shamsu" / "diagnostics"
    for path in diagnostics_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            elif isinstance(node, ast.Import):
                module = ",".join(alias.name for alias in node.names)
            if module and "shamsu.llm" in module:
                pytest.fail(f"{path} imports the LLM layer: {module}")
