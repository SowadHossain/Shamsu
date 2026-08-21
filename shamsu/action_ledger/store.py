"""Read-side helpers for ActionLedger runs - used by the /runs and /run CLI
commands. Pure filesystem reads; never mutates run data except clean_runs()
(retention) and export_run() (writes a separate export artifact).
"""
from __future__ import annotations

import json
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shamsu.action_ledger.config import load_config
from shamsu.action_ledger.redaction import redact_text, redact_value


def runs_dir(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".shamsu" / "runs"


def artifact_path(workspace: Path, run_id: str, relative: str | Path) -> Path:
    """Return a new-layout evidence path, falling back to a legacy run path."""
    run_root = runs_dir(workspace) / run_id
    relative = Path(relative)
    modern = run_root / ".evidence" / relative
    legacy = run_root / relative
    return modern if modern.exists() or not legacy.exists() else legacy


def report_path(workspace: Path, run_id: str) -> Path:
    """Where this run's readable story is.

    The story moved out of the run folder and into the session's
    `log-summary.md`, because a run is a turn and a turn only makes sense in the
    conversation it belongs to. Runs recorded before that still have their own
    `report.md` (or, older still, `narrative.md`), and those are the only copy
    of what happened - so the run-local files are returned when the session log
    is absent rather than reported as missing."""
    run_root = runs_dir(workspace) / run_id
    session_id = str((load_manifest(workspace, run_id) or {}).get("session_id") or "")
    if session_id:
        session_log = (
            Path(workspace) / ".shamsu" / "sessions" / session_id / "log-summary.md"
        )
        if session_log.exists():
            return session_log
    modern = run_root / "report.md"
    legacy = run_root / "narrative.md"
    return modern if modern.exists() or not legacy.exists() else legacy


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def list_run_ids(workspace: Path) -> list[str]:
    root = runs_dir(workspace)
    if not root.is_dir():
        return []
    return sorted((child.name for child in root.iterdir() if child.is_dir()), reverse=True)


@dataclass(frozen=True)
class RunListItem:
    run_id: str
    started_at: str
    status: str
    prompt_preview: str


def list_runs(workspace: Path, limit: int = 20) -> list[RunListItem]:
    items = []
    for run_id in list_run_ids(workspace)[:limit]:
        manifest = load_manifest(workspace, run_id) or {}
        items.append(
            RunListItem(
                run_id=run_id,
                started_at=str(manifest.get("started_at", "")),
                status=str(manifest.get("status", "unknown")),
                prompt_preview=str(manifest.get("prompt_preview", "")),
            )
        )
    return items


def resolve_run_id(workspace: Path, query: str) -> str | None:
    query = (query or "").strip()
    run_ids = list_run_ids(workspace)
    if not run_ids:
        return None
    if not query or query == "last":
        return run_ids[0]
    if query in run_ids:
        return query
    matches = [run_id for run_id in run_ids if run_id.startswith(query)]
    if len(matches) == 1:
        return matches[0]
    return None


def load_manifest(workspace: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(artifact_path(workspace, run_id, "manifest.json"))


def load_summary(workspace: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(artifact_path(workspace, run_id, "summary.json"))


def load_events(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(artifact_path(workspace, run_id, "events.jsonl"))


def load_decisions(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(artifact_path(workspace, run_id, "decisions.jsonl"))


def load_tool_calls(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(artifact_path(workspace, run_id, "tool-calls.jsonl"))


def load_model_calls(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(artifact_path(workspace, run_id, "model-calls.jsonl"))


def load_mutations(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(mutations_path(workspace, run_id))


def mutations_path(workspace: Path, run_id: str) -> Path:
    """The mutation journal, wherever this run put it.

    Spilled payloads moved out of eight typed subfolders into one flat
    `attachments/`. Runs recorded before that still have `mutations/`, and a run
    already on disk is the only copy of what happened - so the old location is
    read when the new one is absent rather than reported as an empty journal."""
    modern = artifact_path(workspace, run_id, Path("attachments") / "mutations.jsonl")
    legacy = artifact_path(workspace, run_id, Path("mutations") / "mutations.jsonl")
    return modern if modern.exists() or not legacy.exists() else legacy


def load_context_preview(workspace: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(artifact_path(workspace, run_id, "context-preview.json"))


def load_context_records(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    """Load every v2 context artifact, falling back to the legacy latest preview."""
    context_dir = artifact_path(workspace, run_id, "attachments")
    legacy_dir = artifact_path(workspace, run_id, "contexts")
    if not any(context_dir.glob("context_*.json")) and legacy_dir.is_dir():
        context_dir = legacy_dir
    records = [
        record
        for path in sorted(context_dir.glob("context_*.json"))
        if (record := _read_json(path)) is not None
    ]
    if records:
        return records
    legacy = load_context_preview(workspace, run_id)
    if legacy is not None and legacy.get("contexts") == []:
        return []
    return [legacy] if legacy is not None else []


def validate_run(workspace: Path, run_id: str) -> dict[str, Any]:
    """Validate structural integrity without changing or replaying a run."""
    run_dir = runs_dir(workspace) / run_id
    errors: list[str] = []
    warnings: list[str] = []
    if not run_dir.is_dir():
        return {"ok": False, "run_id": run_id, "errors": ["run directory is missing"], "warnings": []}

    manifest = load_manifest(workspace, run_id)
    if manifest is None:
        errors.append("manifest.json is missing or invalid")
        manifest = {}
    terminal = str(manifest.get("status", "")) != "running"
    if terminal and load_summary(workspace, run_id) is None:
        errors.append("terminal run is missing summary.json")
    if terminal and not artifact_path(workspace, run_id, "final-output.md").exists():
        errors.append("terminal run is missing final-output.md")

    groups = {
        "events": load_events(workspace, run_id),
        "decisions": load_decisions(workspace, run_id),
        "tools": load_tool_calls(workspace, run_id),
        "models": load_model_calls(workspace, run_id),
        "mutations": load_mutations(workspace, run_id),
        "contexts": load_context_records(workspace, run_id),
    }
    jsonl_paths = {
        "events": artifact_path(workspace, run_id, "events.jsonl"),
        "decisions": artifact_path(workspace, run_id, "decisions.jsonl"),
        "tools": artifact_path(workspace, run_id, "tool-calls.jsonl"),
        "models": artifact_path(workspace, run_id, "model-calls.jsonl"),
        "mutations": mutations_path(workspace, run_id),
    }
    for name, path in jsonl_paths.items():
        if not path.exists():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                errors.append(f"{name} line {line_number} is invalid JSON")
                continue
            if not isinstance(parsed, dict):
                errors.append(f"{name} line {line_number} is not a JSON object")
    required = {
        "schema_version",
        "session_id",
        "turn_id",
        "run_id",
        "operation_id",
        "parent_operation_id",
        "timestamp",
        "sequence",
    }
    for group_name, records in groups.items():
        for index, record in enumerate(records):
            if int(record.get("schema_version", 1) or 1) < 2:
                continue
            missing = sorted(required - record.keys())
            if missing:
                errors.append(f"{group_name}[{index}] missing: {', '.join(missing)}")
            if record.get("run_id") != run_id:
                errors.append(f"{group_name}[{index}] has the wrong run_id")

    v2_sequences = [
        int(record.get("sequence", 0) or 0)
        for records in groups.values()
        for record in records
        if int(record.get("schema_version", 1) or 1) >= 2
    ]
    if any(sequence <= 0 for sequence in v2_sequences):
        errors.append("v2 records contain a non-positive sequence")
    if len(v2_sequences) != len(set(v2_sequences)):
        errors.append("v2 record sequences are not unique")

    tool_records = groups["tools"]
    called_tools = {
        str(record.get("tool_call_id", ""))
        for record in tool_records
        if record.get("phase") == "called"
    }
    finished_tools = {
        str(record.get("tool_call_id", ""))
        for record in tool_records
        if record.get("phase") == "finished"
    }
    for call_id in sorted(called_tools - finished_tools):
        errors.append(f"tool call {call_id} has no finished record")
    for call_id in sorted(finished_tools - called_tools):
        errors.append(f"tool result {call_id} has no called record")

    model_records = groups["models"]
    started_models = {
        str(record.get("model_call_id", ""))
        for record in model_records
        if record.get("phase") == "started"
    }
    finished_models = {
        str(record.get("model_call_id", ""))
        for record in model_records
        if record.get("phase") in {"finished", "failed"}
    }
    for call_id in sorted(started_models - finished_models):
        errors.append(f"model call {call_id} has no terminal record")
    for context in groups["contexts"]:
        model_call_id = str(context.get("model_call_id", ""))
        if model_call_id and model_call_id not in started_models:
            errors.append(f"context {context.get('context_id', '')} references unknown model call {model_call_id}")

    diagnostic_events = [event for event in groups["events"] if event.get("type") == "diagnostics_parsed"]
    command_ids = {
        str(event.get("cmd_id", ""))
        for event in groups["events"]
        if event.get("type") == "command_started"
    }
    for event in diagnostic_events:
        relative = str(event.get("diagnostics_path", ""))
        path = run_dir / relative
        if not relative or not path.is_file():
            errors.append(f"diagnostic artifact is missing: {relative or '(empty path)'}")
            continue
        try:
            packet = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            errors.append(f"diagnostic artifact is invalid JSON: {relative}")
            continue
        operation_id = str(packet.get("operation_id", ""))
        if operation_id and operation_id not in command_ids:
            errors.append(f"diagnostic {relative} references unknown command {operation_id}")
        raw_log_path = str(packet.get("raw_log_path", ""))
        if raw_log_path and not Path(raw_log_path).is_absolute() and not (run_dir / raw_log_path).is_file():
            errors.append(f"diagnostic {relative} references missing raw log {raw_log_path}")
        traceback_path = str(packet.get("traceback_path", ""))
        if traceback_path and not Path(traceback_path).is_absolute() and not (run_dir / traceback_path).is_file():
            errors.append(f"diagnostic {relative} references missing traceback {traceback_path}")
    for record in groups["tools"]:
        diagnostics_path = str(record.get("diagnostics_path", ""))
        if diagnostics_path and not (run_dir / diagnostics_path).is_file():
            errors.append(
                f"tool call {record.get('tool_call_id', '')} references missing diagnostics {diagnostics_path}"
            )
        traceback_path = str(record.get("traceback_path", ""))
        if traceback_path and not (run_dir / traceback_path).is_file():
            errors.append(
                f"tool call {record.get('tool_call_id', '')} references missing traceback {traceback_path}"
            )
        result_artifact_path = str(record.get("artifact_path", ""))
        if result_artifact_path and not (run_dir / result_artifact_path).is_file():
            errors.append(
                f"tool call {record.get('tool_call_id', '')} references missing artifact {result_artifact_path}"
            )
    for record in groups["models"]:
        traceback_path = str(record.get("traceback_path", ""))
        if traceback_path and not (run_dir / traceback_path).is_file():
            errors.append(
                f"model call {record.get('model_call_id', '')} references missing traceback {traceback_path}"
            )

    if terminal and not groups["decisions"]:
        warnings.append("run has no structured decision records")
    return {
        "ok": not errors,
        "run_id": run_id,
        "errors": errors,
        "warnings": warnings,
        "counts": {name: len(records) for name, records in groups.items()},
    }


def load_final_output(workspace: Path, run_id: str) -> str:
    path = artifact_path(workspace, run_id, "final-output.md")
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def command_events(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return [event for event in load_events(workspace, run_id) if event.get("type") in {"command_started", "command_finished"}]


def export_run(workspace: Path, run_id: str) -> Path:
    """Zip the run folder plus a markdown report, redacting defensively
    (data on disk is already redacted at write time; this re-redacts so an
    export is safe even if redact_secrets was off for this run)."""
    run_dir = runs_dir(workspace) / run_id
    if not run_dir.is_dir():
        raise ValueError(f"No such run: {run_id}")
    export_dir = run_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_manifest(workspace, run_id) or {}
    summary = load_summary(workspace, run_id) or {}
    decisions = load_decisions(workspace, run_id)
    report_lines = [
        f"# SHAMSU Run {run_id}",
        "",
        f"- Status: {manifest.get('status', 'unknown')}",
        f"- Started: {manifest.get('started_at', '-')}",
        f"- Finished: {manifest.get('finished_at', '-')}",
        f"- Prompt: {redact_text(str(manifest.get('prompt_preview', '')))}",
        "",
        "## Decisions",
        "",
    ]
    for decision in decisions:
        report_lines.append(f"- **{decision.get('decision')}**: {redact_text(str(decision.get('reason_summary', '')))}")
    report_lines.extend(["", "## Summary", "", f"```json\n{json.dumps(redact_value(summary), indent=2)}\n```", ""])
    generated_report_path = export_dir / "report.md"
    generated_report_path.write_text("\n".join(report_lines), encoding="utf-8")

    zip_path = export_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in run_dir.rglob("*"):
            if path.is_dir() or path.is_relative_to(export_dir):
                continue
            archive.write(path, arcname=str(path.relative_to(run_dir).as_posix()))
        # New runs already contain the human report at this archive path. The
        # generated fallback keeps legacy runs exportable without duplicating
        # the same member name in modern archives.
        if not (run_dir / "report.md").is_file():
            archive.write(generated_report_path, arcname="report.md")
    return zip_path


def runs_older_than(workspace: Path, retention_days: int) -> list[str]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    stale: list[str] = []
    for run_id in list_run_ids(workspace):
        manifest = load_manifest(workspace, run_id) or {}
        started_at = manifest.get("started_at")
        try:
            started = datetime.fromisoformat(str(started_at))
        except (TypeError, ValueError):
            continue
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if started < cutoff:
            stale.append(run_id)
    return stale


def clean_runs(workspace: Path, retention_days: int | None = None) -> list[str]:
    """Delete runs older than retention_days (config default if not given).
    Caller (CLI) is responsible for asking the user to confirm first."""
    days = retention_days if retention_days is not None else load_config(workspace).get("retention_days", 30)
    stale = runs_older_than(workspace, days)
    for run_id in stale:
        shutil.rmtree(runs_dir(workspace) / run_id, ignore_errors=True)
    return stale
