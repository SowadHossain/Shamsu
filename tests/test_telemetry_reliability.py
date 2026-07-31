from __future__ import annotations

from pathlib import Path

from shamsu.action_ledger.ledger import start_run
from shamsu.telemetry.reliability import analyze_workspaces, render_markdown


def _workspace(root: Path, name: str) -> Path:
    path = root / name
    path.mkdir()
    return path


def test_aggregate_reliability_report_counts_core_metrics(tmp_path: Path):
    good_workspace = _workspace(tmp_path, "good")
    good = start_run(good_workspace, "build the app")
    call_id = good.log_tool_call("read_file", {"filepath": "big.txt"})
    good.log_tool_result(
        call_id,
        "read_file",
        True,
        "Read file.",
        {"filepath": "big.txt"},
        original_tokens=1400,
        returned_tokens=500,
        max_tokens=500,
        truncated=True,
        full_result_text="x " * 4000,
    )
    good.log_mutation_finished("txn-good", "applied", ["app.py"])
    good.log_verification_result(
        True,
        "Compilation passed.",
        command="python -m py_compile app.py",
        source="unit",
        required=True,
        files=["app.py"],
        exit_code=0,
    )
    good.log_repair_attempt(
        attempt_index=1,
        outcome="SOLVED",
        kept=True,
        files_changed=["app.py"],
        command="python -m py_compile app.py",
    )
    good.finish("done", status="success")

    unverified_workspace = _workspace(tmp_path, "unverified")
    unverified = start_run(unverified_workspace, "edit without verifier")
    unverified.log_mutation_finished("txn-unverified", "applied", ["app.py"])
    unverified.finish("done", status="success")

    false_workspace = _workspace(tmp_path, "false")
    false = start_run(false_workspace, "claim success despite failure")
    false.log_verification_result(
        False,
        "Tests failed.",
        command="pytest",
        source="unit",
        required=True,
        exit_code=1,
    )
    false.finish("done", status="success")

    report = analyze_workspaces([tmp_path], recursive=True)

    assert report.totals["runs"] == 3
    assert report.totals["apply_attempts"] == 2
    assert report.totals["clean_applies"] == 2
    assert report.totals["first_pass_verified"] == 1
    assert report.totals["first_pass_failed_or_missing"] == 1
    assert report.totals["repair_successes"] == 1
    assert report.totals["tool_results_over_threshold"] == 1
    assert report.totals["tool_results_truncated"] == 1
    assert report.totals["false_success_candidates"] == 1
    assert report.totals["success_without_verification"] == 1
    assert report.category_counts["verification"] == 2
    assert report.category_counts["none"] == 1
    assert report.rates["apply_success_rate"] == 1.0
    assert report.rates["false_success_rate"] == 0.3333

    markdown = render_markdown(report)
    assert "SHAMSU Reliability Report" in markdown
    assert "False-success candidates" in markdown
    assert "Failure Categories" in markdown
    assert "success_without_verification" in markdown


def test_reliability_report_classifies_patch_and_requirement_failures(tmp_path: Path):
    patch_workspace = _workspace(tmp_path, "patch")
    patch = start_run(patch_workspace, "apply a patch")
    patch.log_event("patch_apply_failed", error="context mismatch")
    patch.finish("failed", status="failed")

    contract_workspace = _workspace(tmp_path, "contract")
    contract = start_run(contract_workspace, "build from PRD")
    contract.log_event("contract_failed", detail="missing acceptance")
    contract.finish("failed", status="failed")

    report = analyze_workspaces([tmp_path], recursive=True)
    by_run = {run.run_id: run for run in report.runs}

    assert by_run[patch.run_id].failure_category == "patch_application"
    assert by_run[contract.run_id].failure_category == "requirement_coverage"
    assert report.category_counts["patch_application"] == 1
    assert report.category_counts["requirement_coverage"] == 1
