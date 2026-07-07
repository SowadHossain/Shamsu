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
    return _read_json(runs_dir(workspace) / run_id / "manifest.json")


def load_summary(workspace: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(runs_dir(workspace) / run_id / "summary.json")


def load_events(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(runs_dir(workspace) / run_id / "events.jsonl")


def load_decisions(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(runs_dir(workspace) / run_id / "decisions.jsonl")


def load_tool_calls(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(runs_dir(workspace) / run_id / "tool-calls.jsonl")


def load_model_calls(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(runs_dir(workspace) / run_id / "model-calls.jsonl")


def load_mutations(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    return _read_jsonl(runs_dir(workspace) / run_id / "mutations" / "mutations.jsonl")


def load_context_preview(workspace: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(runs_dir(workspace) / run_id / "context-preview.json")


def load_final_output(workspace: Path, run_id: str) -> str:
    path = runs_dir(workspace) / run_id / "final-output.md"
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
    report_path = export_dir / "report.md"
    report_path.write_text("\n".join(report_lines), encoding="utf-8")

    zip_path = export_dir / f"{run_id}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in run_dir.rglob("*"):
            if path.is_dir() or path.is_relative_to(export_dir):
                continue
            archive.write(path, arcname=str(path.relative_to(run_dir).as_posix()))
        archive.write(report_path, arcname="report.md")
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
