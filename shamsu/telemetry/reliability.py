"""Aggregate reliability telemetry for ActionLedger runs.

This module is intentionally read-only. It measures the current executor from
durable `.shamsu/runs` artifacts so later reliability gates can be audited
against stable metric definitions.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.action_ledger import store
from shamsu.context.budget import count_tokens

TOOL_PRESSURE_TOKEN_THRESHOLD = 1000
_SKIP_DISCOVERY_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}
_MUTATION_TOOL_NAMES = {
    "write_file",
    "edit_file",
    "move_file",
    "delete_file",
    "create_directory",
}
_CLEAN_APPLY_STATUSES = {"applied", "verification_failed", "rolled_back"}
_VERIFICATION_RESULTS = {
    "verification_passed",
    "verification_failed",
    "verification_unavailable",
}
_FAILURE_EVENTS = {
    "patch_apply_failed",
    "mutation_required_but_missing",
    "contract_failed",
    "agent_stopped",
    "composite_failed",
}
_FAILURE_CATEGORIES = {
    "none",
    "routing",
    "planning",
    "context",
    "tool_call",
    "patch_application",
    "verification",
    "repair",
    "requirement_coverage",
    "environment",
}


@dataclass(frozen=True)
class RunLocator:
    workspace: str
    run_id: str


@dataclass
class RunReliability:
    workspace: str
    run_id: str
    status: str
    prompt_preview: str = ""
    route: str = ""
    apply_attempts: int = 0
    clean_applies: int = 0
    apply_failures: int = 0
    verification_attempts: int = 0
    verification_passes: int = 0
    verification_failures: int = 0
    verification_unavailable: int = 0
    verifier_identity_missing: int = 0
    first_pass_verified: bool | None = None
    repair_attempts: int = 0
    repair_success_attempt: int | None = None
    tool_results: int = 0
    tool_results_over_threshold: int = 0
    tool_results_truncated: int = 0
    tool_results_missing_token_telemetry: int = 0
    max_tool_result_tokens: int = 0
    false_success_candidate: bool = False
    success_without_verification: bool = False
    failure_category: str = "none"
    failure_category_reason: str = ""
    failure_evidence: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)


@dataclass
class AggregateReliability:
    generated_at: str
    token_threshold: int
    runs: list[RunReliability]
    status_counts: dict[str, int]
    category_counts: dict[str, int]
    totals: dict[str, int]
    rates: dict[str, float | None]
    worst_failure_classes: list[dict[str, float | int | str]]
    data_gaps: list[str]


def discover_runs(inputs: list[Path], *, recursive: bool = False, limit: int | None = None) -> list[RunLocator]:
    """Resolve input paths into run locators.

    Accepted inputs:
    - a workspace containing `.shamsu/runs`
    - a `.shamsu/runs` directory
    - a single run directory containing `manifest.json`
    - with `recursive=True`, any tree containing nested `.shamsu/runs`
    """
    locators: list[RunLocator] = []
    seen: set[tuple[str, str]] = set()
    paths = inputs or [Path.cwd()]
    for raw in paths:
        path = raw.resolve()
        for workspace, run_ids in _locators_from_path(path, recursive=recursive):
            for run_id in run_ids:
                key = (str(workspace), run_id)
                if key in seen:
                    continue
                seen.add(key)
                locators.append(RunLocator(workspace=str(workspace), run_id=run_id))
                if limit is not None and len(locators) >= limit:
                    return locators
    return sorted(locators, key=lambda item: item.run_id, reverse=True)


def analyze_workspaces(
    inputs: list[Path],
    *,
    recursive: bool = False,
    limit: int | None = None,
    token_threshold: int = TOOL_PRESSURE_TOKEN_THRESHOLD,
) -> AggregateReliability:
    locators = discover_runs(inputs, recursive=recursive, limit=limit)
    runs = [
        analyze_run(Path(locator.workspace), locator.run_id, token_threshold=token_threshold)
        for locator in locators
    ]
    return aggregate(runs, token_threshold=token_threshold)


def analyze_run(
    workspace: Path,
    run_id: str,
    *,
    token_threshold: int = TOOL_PRESSURE_TOKEN_THRESHOLD,
) -> RunReliability:
    workspace = Path(workspace).resolve()
    manifest = store.load_manifest(workspace, run_id) or {}
    summary = store.load_summary(workspace, run_id) or {}
    events = store.load_events(workspace, run_id)
    tools = store.load_tool_calls(workspace, run_id)
    mutations = store.load_mutations(workspace, run_id)

    result = RunReliability(
        workspace=str(workspace),
        run_id=run_id,
        status=str(manifest.get("status", "unknown")),
        prompt_preview=str(manifest.get("prompt_preview", "")),
        route=str(summary.get("route", "")),
    )
    _measure_apply(result, events, tools, mutations)
    _measure_verification(result, events)
    _measure_repair(result, events)
    _measure_tool_pressure(result, tools, token_threshold)
    _measure_false_success(result, events, mutations)
    _classify_failure_category(result, events, tools, mutations)
    _record_data_gaps(result, events, tools, mutations)
    return result


def aggregate(
    runs: list[RunReliability],
    *,
    token_threshold: int = TOOL_PRESSURE_TOKEN_THRESHOLD,
) -> AggregateReliability:
    status_counts = Counter(run.status for run in runs)
    category_counts = Counter(run.failure_category for run in runs)
    totals = {
        "runs": len(runs),
        "apply_attempts": sum(run.apply_attempts for run in runs),
        "clean_applies": sum(run.clean_applies for run in runs),
        "apply_failures": sum(run.apply_failures for run in runs),
        "verification_attempts": sum(run.verification_attempts for run in runs),
        "verification_passes": sum(run.verification_passes for run in runs),
        "verification_failures": sum(run.verification_failures for run in runs),
        "verification_unavailable": sum(run.verification_unavailable for run in runs),
        "verifier_identity_missing": sum(run.verifier_identity_missing for run in runs),
        "first_pass_verified": sum(run.first_pass_verified is True for run in runs),
        "first_pass_failed_or_missing": sum(run.first_pass_verified is False for run in runs),
        "repair_attempts": sum(run.repair_attempts for run in runs),
        "repair_successes": sum(run.repair_success_attempt is not None for run in runs),
        "tool_results": sum(run.tool_results for run in runs),
        "tool_results_over_threshold": sum(run.tool_results_over_threshold for run in runs),
        "tool_results_truncated": sum(run.tool_results_truncated for run in runs),
        "tool_results_missing_token_telemetry": sum(
            run.tool_results_missing_token_telemetry for run in runs
        ),
        "false_success_candidates": sum(run.false_success_candidate for run in runs),
        "success_without_verification": sum(run.success_without_verification for run in runs),
    }
    rates = {
        "apply_success_rate": _rate(totals["clean_applies"], totals["apply_attempts"]),
        "verification_pass_rate": _rate(totals["verification_passes"], totals["verification_attempts"]),
        "first_pass_verified_rate": _rate(
            totals["first_pass_verified"],
            totals["first_pass_verified"] + totals["first_pass_failed_or_missing"],
        ),
        "repair_success_rate": _rate(totals["repair_successes"], totals["repair_attempts"]),
        "false_success_rate": _rate(totals["false_success_candidates"], totals["runs"]),
        "success_without_verification_rate": _rate(
            totals["success_without_verification"],
            totals["runs"],
        ),
        "tool_pressure_rate": _rate(totals["tool_results_over_threshold"], totals["tool_results"]),
        "tool_truncation_rate": _rate(totals["tool_results_truncated"], totals["tool_results"]),
        "tool_token_telemetry_missing_rate": _rate(
            totals["tool_results_missing_token_telemetry"],
            totals["tool_results"],
        ),
    }
    gaps = sorted({gap for run in runs for gap in run.data_gaps})
    return AggregateReliability(
        generated_at=datetime.now(timezone.utc).isoformat(),
        token_threshold=token_threshold,
        runs=runs,
        status_counts=dict(status_counts),
        category_counts=dict(category_counts),
        totals=totals,
        rates=rates,
        worst_failure_classes=_rank_failure_classes(totals, rates),
        data_gaps=gaps,
    )


def render_markdown(report: AggregateReliability) -> str:
    lines = [
        "# SHAMSU Reliability Report",
        "",
        f"- Generated: {report.generated_at}",
        f"- Runs included: {report.totals['runs']}",
        f"- Tool-pressure threshold: {report.token_threshold} tokens",
        "",
        "## Metric Definitions",
        "",
        "- Apply success: mutation transactions that reached `applied`, `verification_failed`, or `rolled_back` divided by observed apply attempts.",
        "- Verification pass: `verification_passed` events divided by all verification result events.",
        "- First-pass verified: a run with mutations whose first verification result passed before repair.",
        "- Repair success: first `repair_attempt_finished` event with `outcome=SOLVED` and `kept=true`.",
        "- False-success candidate: run status `success` with unrecovered failure evidence in the ledger.",
        "- Success without verification: run status `success` with mutations and no `verification_passed` evidence.",
        "- Tool pressure: finished tool results whose original token count exceeded the configured threshold.",
        "- Failure category: deterministic label from recorded ledger evidence; `none` means no failure evidence was observed.",
        "",
        "## Aggregate",
        "",
    ]
    lines.extend(_table(["Metric", "Value"], _aggregate_rows(report)))
    lines.extend(["", "## Failure Class Ranking", ""])
    lines.extend(
        _table(
            ["Class", "Rate", "Numerator", "Denominator"],
            [
                [
                    str(item["class"]),
                    _format_rate(float(item["rate"])),
                    str(item["numerator"]),
                    str(item["denominator"]),
                ]
                for item in report.worst_failure_classes
            ],
        )
    )
    lines.extend(["", "## Failure Categories", ""])
    lines.extend(
        _table(
            ["Category", "Runs"],
            [[category, str(count)] for category, count in sorted(report.category_counts.items())],
        )
    )
    lines.extend(["", "## Runs", ""])
    lines.extend(
        _table(
            [
                "Run",
                "Status",
                "Apply",
                "Verify",
                "Repair",
                "Tool",
                "Category",
                "Flags",
            ],
            [_run_row(run, report.token_threshold) for run in report.runs],
        )
    )
    lines.extend(["", "## Data Quality", ""])
    if report.data_gaps:
        lines.extend(f"- {gap}" for gap in report.data_gaps)
    else:
        lines.append("- No aggregate data gaps detected.")
    return "\n".join(lines).rstrip() + "\n"


def to_dict(report: AggregateReliability) -> dict[str, Any]:
    return asdict(report)


def _measure_apply(
    result: RunReliability,
    events: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    mutations: list[dict[str, Any]],
) -> None:
    patch_failures = sum(1 for event in events if event.get("type") == "patch_apply_failed")
    if mutations:
        result.apply_attempts = len(mutations) + patch_failures
        result.clean_applies = sum(
            str(item.get("status", "")) in _CLEAN_APPLY_STATUSES for item in mutations
        )
        result.apply_failures = sum(
            str(item.get("status", "")) not in _CLEAN_APPLY_STATUSES for item in mutations
        ) + patch_failures
        return

    finished_tools = [record for record in tools if record.get("phase") == "finished"]
    mutating_tools = [
        record
        for record in finished_tools
        if _is_mutation_tool(record)
    ]
    result.apply_attempts = len(mutating_tools) + patch_failures
    result.clean_applies = sum(bool(record.get("ok")) for record in mutating_tools)
    result.apply_failures = sum(not bool(record.get("ok")) for record in mutating_tools) + patch_failures


def _measure_verification(result: RunReliability, events: list[dict[str, Any]]) -> None:
    verification_events = [
        event for event in events if str(event.get("type", "")) in _VERIFICATION_RESULTS
    ]
    result.verification_attempts = len(verification_events)
    result.verification_passes = sum(event.get("type") == "verification_passed" for event in verification_events)
    result.verification_failures = sum(event.get("type") == "verification_failed" for event in verification_events)
    result.verification_unavailable = sum(
        event.get("type") == "verification_unavailable" for event in verification_events
    )
    result.verifier_identity_missing = sum(
        1 for event in verification_events if not str(event.get("verifier_id", "")).strip()
    )
    if result.apply_attempts:
        first = verification_events[0] if verification_events else None
        result.first_pass_verified = bool(first and first.get("type") == "verification_passed")


def _measure_repair(result: RunReliability, events: list[dict[str, Any]]) -> None:
    repairs = [event for event in events if event.get("type") == "repair_attempt_finished"]
    result.repair_attempts = len(repairs)
    for event in repairs:
        if str(event.get("outcome", "")).upper() == "SOLVED" and bool(event.get("kept")):
            try:
                result.repair_success_attempt = int(event.get("attempt_index", 0) or 0)
            except (TypeError, ValueError):
                result.repair_success_attempt = 0
            return


def _measure_tool_pressure(
    result: RunReliability,
    tools: list[dict[str, Any]],
    token_threshold: int,
) -> None:
    for record in tools:
        if record.get("phase") != "finished":
            continue
        result.tool_results += 1
        original, returned, has_telemetry = _tool_token_counts(record)
        if not has_telemetry:
            result.tool_results_missing_token_telemetry += 1
        result.max_tool_result_tokens = max(result.max_tool_result_tokens, original)
        if original > token_threshold:
            result.tool_results_over_threshold += 1
        if bool(record.get("truncated")) or (has_telemetry and returned < original):
            result.tool_results_truncated += 1


def _measure_false_success(
    result: RunReliability,
    events: list[dict[str, Any]],
    mutations: list[dict[str, Any]],
) -> None:
    evidence: list[str] = []
    event_types = [str(event.get("type", "")) for event in events]
    for event_type in sorted(_FAILURE_EVENTS & set(event_types)):
        evidence.append(f"event:{event_type}")
    if _has_unrecovered_verification_failure(events):
        evidence.append("verification:unrecovered_failure")
    if _latest_command_failed(events):
        evidence.append("command:latest_failed")
    if any(str(item.get("status", "")) == "failed" for item in mutations):
        evidence.append("mutation:failed")

    result.failure_evidence = evidence
    result.false_success_candidate = result.status == "success" and bool(evidence)
    result.success_without_verification = (
        result.status == "success"
        and result.apply_attempts > 0
        and result.verification_passes == 0
    )


def _classify_failure_category(
    result: RunReliability,
    events: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    mutations: list[dict[str, Any]],
) -> None:
    """Assign one stable failure taxonomy label from observed ledger evidence."""
    event_types = {str(event.get("type", "")) for event in events}
    if not _has_failure_signal(result):
        result.failure_category = "none"
        result.failure_category_reason = ""
        return

    if result.status in {"timed_out", "cancelled", "denied"}:
        result.failure_category = "environment"
        result.failure_category_reason = f"terminal status: {result.status}"
    elif "contract_failed" in event_types:
        result.failure_category = "requirement_coverage"
        result.failure_category_reason = "contract_failed event"
    elif result.repair_attempts and result.repair_success_attempt is None:
        result.failure_category = "repair"
        result.failure_category_reason = "repair attempts did not solve the verifier"
    elif result.verification_failures or result.verification_unavailable or result.success_without_verification:
        result.failure_category = "verification"
        result.failure_category_reason = "verification failed, unavailable, or absent after mutation"
    elif result.apply_failures or "patch_apply_failed" in event_types or _has_failed_mutation(mutations):
        result.failure_category = "patch_application"
        result.failure_category_reason = "patch/mutation apply evidence failed"
    elif _has_unrecovered_tool_result_failure(tools) or "agent_stopped" in event_types:
        result.failure_category = "tool_call"
        result.failure_category_reason = "tool failure or stopped tool loop"
    elif "mutation_required_but_missing" in event_types:
        result.failure_category = "planning"
        result.failure_category_reason = "mutation was required but no mutation landed"
    elif result.status in {"needs_input", "partial"}:
        result.failure_category = "planning"
        result.failure_category_reason = f"terminal status: {result.status}"
    else:
        result.failure_category = "routing"
        result.failure_category_reason = "failure evidence did not match a narrower class"

    if result.failure_category not in _FAILURE_CATEGORIES:
        result.failure_category = "routing"


def _record_data_gaps(
    result: RunReliability,
    events: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    mutations: list[dict[str, Any]],
) -> None:
    if result.tool_results and result.tool_results_missing_token_telemetry:
        result.data_gaps.append("some tool results lack token telemetry")
    if result.apply_attempts and not mutations:
        result.data_gaps.append("apply metrics inferred from tool results because mutation records are absent")
    if result.verification_attempts and result.verifier_identity_missing:
        result.data_gaps.append("some verification events lack verifier_id")
    if result.apply_attempts and result.verification_attempts == 0:
        result.data_gaps.append("mutations have no verification result event")
    if any(event.get("type") == "repair_attempt_finished" for event in events) and not result.repair_attempts:
        result.data_gaps.append("repair events were present but could not be counted")


def _locators_from_path(path: Path, *, recursive: bool) -> list[tuple[Path, list[str]]]:
    found: list[tuple[Path, list[str]]] = []
    if path.is_dir() and (path / "manifest.json").is_file() and path.parent.name == "runs":
        return [(path.parent.parent.parent, [path.name])]
    run_root = _runs_dir_from_path(path)
    if run_root is not None:
        workspace = run_root.parent.parent
        found.append((workspace, _run_ids_from_runs_dir(run_root)))
    if recursive and path.is_dir():
        for shamsu_dir in _iter_shamsu_dirs(path):
            nested_runs = shamsu_dir / "runs"
            if nested_runs.is_dir():
                found.append((shamsu_dir.parent, _run_ids_from_runs_dir(nested_runs)))
    return found


def _runs_dir_from_path(path: Path) -> Path | None:
    if path.name == "runs" and path.parent.name == ".shamsu" and path.is_dir():
        return path
    candidate = path / ".shamsu" / "runs"
    if candidate.is_dir():
        return candidate
    return None


def _run_ids_from_runs_dir(run_root: Path) -> list[str]:
    return sorted((child.name for child in run_root.iterdir() if child.is_dir()), reverse=True)


def _iter_shamsu_dirs(root: Path):
    for current, dirnames, _filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in _SKIP_DISCOVERY_DIRS]
        if ".shamsu" in dirnames:
            yield Path(current) / ".shamsu"


def _is_mutation_tool(record: dict[str, Any]) -> bool:
    tool = str(record.get("tool", ""))
    if tool in _MUTATION_TOOL_NAMES:
        return True
    data = record.get("data") if isinstance(record.get("data"), dict) else {}
    return tool.startswith("mcp__") and bool(data.get("touched_files")) and not bool(data.get("read_only"))


def _tool_token_counts(record: dict[str, Any]) -> tuple[int, int, bool]:
    original = record.get("original_tokens")
    returned = record.get("returned_tokens")
    has_telemetry = original is not None and returned is not None
    if has_telemetry:
        return _as_int(original), _as_int(returned), True
    payload = {
        "ok": record.get("ok"),
        "message": record.get("message", ""),
        "data": record.get("data", {}),
    }
    tokens = count_tokens(json.dumps(payload, ensure_ascii=True, default=str))
    return tokens, tokens, False


def _has_unrecovered_verification_failure(events: list[dict[str, Any]]) -> bool:
    latest: dict[str, bool] = {}
    for event in events:
        event_type = str(event.get("type", ""))
        if event_type not in {"verification_failed", "verification_passed"}:
            continue
        key = str(event.get("verifier_id") or event.get("command") or "__general__")
        latest[key] = event_type == "verification_passed"
    return any(passed is False for passed in latest.values())


def _latest_command_failed(events: list[dict[str, Any]]) -> bool:
    latest: int | None = None
    for event in events:
        if event.get("type") != "command_finished":
            continue
        latest = _as_int(event.get("exit_code", 1), default=1)
    return latest is not None and latest != 0


def _rate(numerator: int, denominator: int) -> float | None:
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _rank_failure_classes(
    totals: dict[str, int],
    rates: dict[str, float | None],
) -> list[dict[str, float | int | str]]:
    candidates = [
        (
            "apply_failure",
            1 - rates["apply_success_rate"] if rates["apply_success_rate"] is not None else None,
            totals["apply_failures"],
            totals["apply_attempts"],
        ),
        (
            "verification_failure_or_unavailable",
            _rate(totals["verification_failures"] + totals["verification_unavailable"], totals["verification_attempts"]),
            totals["verification_failures"] + totals["verification_unavailable"],
            totals["verification_attempts"],
        ),
        (
            "first_pass_not_verified",
            1 - rates["first_pass_verified_rate"] if rates["first_pass_verified_rate"] is not None else None,
            totals["first_pass_failed_or_missing"],
            totals["first_pass_verified"] + totals["first_pass_failed_or_missing"],
        ),
        (
            "repair_not_solved",
            1 - rates["repair_success_rate"] if rates["repair_success_rate"] is not None else None,
            totals["repair_attempts"] - totals["repair_successes"],
            totals["repair_attempts"],
        ),
        (
            "false_success_candidate",
            rates["false_success_rate"],
            totals["false_success_candidates"],
            totals["runs"],
        ),
        (
            "success_without_verification",
            rates["success_without_verification_rate"],
            totals["success_without_verification"],
            totals["runs"],
        ),
        (
            "tool_pressure_over_threshold",
            rates["tool_pressure_rate"],
            totals["tool_results_over_threshold"],
            totals["tool_results"],
        ),
        (
            "tool_token_telemetry_missing",
            rates["tool_token_telemetry_missing_rate"],
            totals["tool_results_missing_token_telemetry"],
            totals["tool_results"],
        ),
    ]
    rows = [
        {
            "class": name,
            "rate": round(float(rate), 4),
            "numerator": int(numerator),
            "denominator": int(denominator),
        }
        for name, rate, numerator, denominator in candidates
        if rate is not None and denominator > 0
    ]
    return sorted(rows, key=lambda item: (float(item["rate"]), int(item["numerator"])), reverse=True)


def _aggregate_rows(report: AggregateReliability) -> list[list[str]]:
    totals = report.totals
    rates = report.rates
    return [
        ["Statuses", json.dumps(report.status_counts, sort_keys=True)],
        ["Failure categories", json.dumps(report.category_counts, sort_keys=True)],
        ["Apply success", f"{totals['clean_applies']}/{totals['apply_attempts']} ({_format_optional_rate(rates['apply_success_rate'])})"],
        ["Verification pass", f"{totals['verification_passes']}/{totals['verification_attempts']} ({_format_optional_rate(rates['verification_pass_rate'])})"],
        ["First-pass verified", f"{totals['first_pass_verified']}/{totals['first_pass_verified'] + totals['first_pass_failed_or_missing']} ({_format_optional_rate(rates['first_pass_verified_rate'])})"],
        ["Repair success", f"{totals['repair_successes']}/{totals['repair_attempts']} ({_format_optional_rate(rates['repair_success_rate'])})"],
        ["False-success candidates", f"{totals['false_success_candidates']}/{totals['runs']} ({_format_optional_rate(rates['false_success_rate'])})"],
        ["Success without verification", f"{totals['success_without_verification']}/{totals['runs']} ({_format_optional_rate(rates['success_without_verification_rate'])})"],
        ["Tool pressure", f"{totals['tool_results_over_threshold']}/{totals['tool_results']} ({_format_optional_rate(rates['tool_pressure_rate'])})"],
        ["Tool truncation", f"{totals['tool_results_truncated']}/{totals['tool_results']} ({_format_optional_rate(rates['tool_truncation_rate'])})"],
        ["Missing tool-token telemetry", f"{totals['tool_results_missing_token_telemetry']}/{totals['tool_results']} ({_format_optional_rate(rates['tool_token_telemetry_missing_rate'])})"],
    ]


def _run_row(run: RunReliability, token_threshold: int) -> list[str]:
    flags: list[str] = []
    if run.false_success_candidate:
        flags.append("false-success?")
    if run.success_without_verification:
        flags.append("unverified-success")
    flags.extend(run.failure_evidence[:2])
    if len(run.failure_evidence) > 2:
        flags.append(f"+{len(run.failure_evidence) - 2} evidence")
    return [
        run.run_id,
        run.status,
        f"{run.clean_applies}/{run.apply_attempts}",
        f"{run.verification_passes}/{run.verification_attempts}",
        "-" if run.repair_success_attempt is None else f"solved@{run.repair_success_attempt}",
        f"{run.tool_results_over_threshold}/{run.tool_results} >{token_threshold}",
        run.failure_category,
        ", ".join(flags) if flags else "-",
    ]


def _has_failure_signal(result: RunReliability) -> bool:
    return bool(
        result.status not in {"success", "success_unverified"}
        or result.failure_evidence
        or result.false_success_candidate
        or result.success_without_verification
        or result.apply_failures
        or result.verification_failures
        or result.verification_unavailable
        or (result.repair_attempts and result.repair_success_attempt is None)
    )


def _has_failed_mutation(mutations: list[dict[str, Any]]) -> bool:
    return any(str(item.get("status", "")) == "failed" for item in mutations)


def _has_unrecovered_tool_result_failure(tools: list[dict[str, Any]]) -> bool:
    latest: dict[str, bool] = {}
    for record in tools:
        if record.get("phase") != "finished":
            continue
        latest[str(record.get("tool", "unknown"))] = bool(record.get("ok"))
    return any(ok is False for ok in latest.values())


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    output = ["| " + " | ".join(headers) + " |"]
    output.append("| " + " | ".join("---" for _ in headers) + " |")
    output.extend("| " + " | ".join(_escape_cell(cell) for cell in row) + " |" for row in rows)
    return output


def _escape_cell(value: str) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _format_optional_rate(value: float | None) -> str:
    return "n/a" if value is None else _format_rate(value)


def _format_rate(value: float) -> str:
    return f"{value * 100:.1f}%"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m shamsu.telemetry.reliability",
        description="Generate a Phase 0 aggregate reliability report from .shamsu/runs.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Workspace, .shamsu/runs directory, or run directory. Defaults to cwd.",
    )
    parser.add_argument("--recursive", action="store_true", help="Discover nested .shamsu/runs directories.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of runs to include.")
    parser.add_argument(
        "--tool-threshold",
        type=int,
        default=TOOL_PRESSURE_TOKEN_THRESHOLD,
        help="Token threshold for tool-pressure counts.",
    )
    parser.add_argument("--out", type=Path, default=None, help="Write Markdown report to this path.")
    parser.add_argument("--json-out", type=Path, default=None, help="Write machine-readable JSON to this path.")
    args = parser.parse_args(argv)

    report = analyze_workspaces(
        args.paths,
        recursive=args.recursive,
        limit=args.limit,
        token_threshold=args.tool_threshold,
    )
    markdown = render_markdown(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(to_dict(report), indent=2), encoding="utf-8")
    if not args.out:
        print(markdown)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
