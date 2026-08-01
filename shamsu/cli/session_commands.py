"""Human-facing ActionLedger run inspection commands."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from shamsu.action_ledger import store
from shamsu.action_ledger.config import (
    DEFAULT_LOG_LEVEL,
    LOG_LEVEL_ENV_VAR,
    config_path,
    load_config,
    save_config,
)
from shamsu.safety.approval import ask_approval
from shamsu.types import ApprovalRequest


def handle_runs(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=1)
    limit = int(parts[1].strip()) if len(parts) > 1 and parts[1].strip().isdigit() else 20
    runs = store.list_runs(workspace, limit=limit)
    if not runs:
        console.print("[dim]No runs recorded yet.[/dim]")
        return
    table = Table(title="ActionLedger Runs")
    for label in ("Run ID", "Started", "Status", "Prompt"):
        table.add_column(label)
    for item in runs:
        table.add_row(item.run_id, item.started_at, item.status, item.prompt_preview[:80])
    console.print(table)


def _resolve(workspace: Path, query: str, console: Console) -> str | None:
    run_id = store.resolve_run_id(workspace, query)
    if not run_id:
        console.print(f"[red]No run found for: {query or 'last'}[/red]")
    return run_id


def _summary(workspace: Path, run_id: str, console: Console) -> None:
    manifest = store.load_manifest(workspace, run_id) or {}
    summary = store.load_summary(workspace, run_id) or {}
    decisions = store.load_decisions(workspace, run_id)
    tool_results = [
        item for item in store.load_tool_calls(workspace, run_id) if item.get("phase") == "finished"
    ]
    mutations = store.load_mutations(workspace, run_id)
    events = store.load_events(workspace, run_id)
    changed_files = list(
        dict.fromkeys(
            str(path)
            for mutation in mutations
            for path in mutation.get("touched_files", [])
            if path
        )
    )
    verification = [
        item
        for item in events
        if item.get("type") in {"verification_passed", "verification_failed"}
    ]
    decision_text = "; ".join(
        str(item.get("chosen_action") or item.get("decision") or "unknown")
        for item in decisions
    ) or "none"
    tool_text = "; ".join(
        f"{item.get('tool', 'unknown')}={item.get('status') or ('success' if item.get('ok') else 'failed')}"
        for item in tool_results
    ) or "none"
    verification_text = "; ".join(
        f"{str(item.get('type', '')).removeprefix('verification_')}: {item.get('summary', '')}".rstrip()
        for item in verification
    ) or "not run"
    final_output = store.load_final_output(workspace, run_id).strip()
    if len(final_output) > 500:
        final_output = f"{final_output[:500]}..."
    lines = [
        f"Run: {run_id}",
        f"Status: {manifest.get('status', 'unknown')}",
        f"Started: {manifest.get('started_at', '-')}",
        f"Finished: {manifest.get('finished_at', '-')}",
        f"Prompt: {manifest.get('prompt_preview', '-')}",
        f"Events: {summary.get('event_count', '-')}",
        f"Decisions: {summary.get('decision_count', '-')}",
        f"Tool calls: {summary.get('tool_call_count', '-')}",
        f"Commands: {summary.get('command_count', '-')}",
        f"Decision summary: {decision_text}",
        f"Tool outcomes: {tool_text}",
        f"Changed files: {', '.join(changed_files) if changed_files else 'none'}",
        f"Verification: {verification_text}",
        f"Output: {final_output or '(empty)'}",
    ]
    console.print(Panel("\n".join(lines), title=f"Run {run_id}"))


def _narrative(workspace: Path, run_id: str, console: Console) -> None:
    """Print the readable report for a run."""
    path = store.report_path(workspace, run_id)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        console.print(
            f"[yellow]No report recorded for {run_id}.[/yellow]\n"
            "[dim]Runs from before the human report predate this; new runs write it "
            "automatically.[/dim]"
        )
        return
    # Text(), not rich's Markdown renderer: Markdown emits box-drawing glyphs a
    # legacy Windows console cannot encode, and log text containing "[REDACTED]"
    # would otherwise be parsed as console markup.
    console.print(Text(text))
    console.print(f"[dim]{path}[/dim]")


def _model_artifacts(workspace: Path, run_id: str, kind: str, console: Console) -> None:
    """Print a run's captured prompts or chain-of-thought - tier 1 of the log,
    in model-call order."""
    evidence = store.runs_dir(workspace) / run_id / ".evidence"
    directory = evidence / ("prompts" if kind == "prompt" else "reasoning")
    if not directory.is_dir():
        directory = store.runs_dir(workspace) / run_id / ("prompts" if kind == "prompt" else "cot")
    paths = sorted(directory.glob("*.txt")) if directory.is_dir() else []
    if not paths:
        label = "prompt" if kind == "prompt" else "chain-of-thought"
        console.print(
            f"[yellow]No {label} artifacts for {run_id}.[/yellow]\n"
            "[dim]Full model prompt and reasoning artifacts are only written under "
            f"{LOG_LEVEL_ENV_VAR}=verbose; reasoning also depends on model support.[/dim]"
        )
        return
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            console.print(f"[red]Could not read {path.name}: {exc}[/red]")
            continue
        console.print(Panel(Text(text), title=f"{run_id} / {directory.name}/{path.name}"))
    console.print(f"[dim]{directory}[/dim]")


def handle_logs(user_input: str, workspace: Path, console: Console) -> None:
    """Show or configure the project's human-readable run reports."""
    parts = user_input.split(maxsplit=1)
    subcommand = parts[1].strip().lower() if len(parts) > 1 else ""
    root = Path(workspace).resolve() / ".shamsu"
    runs_root = store.runs_dir(workspace)
    run_example = runs_root / "<run-id>"
    session_example = root / "sessions" / "<session-id>"
    level = load_config(workspace).get("log_level", DEFAULT_LOG_LEVEL)

    if subcommand.startswith("mode"):
        _, _, requested = subcommand.partition(" ")
        requested = requested.strip().lower()
        aliases = {"compact": "essential", "full": "verbose"}
        requested = aliases.get(requested, requested)
        if requested not in {"essential", "verbose"}:
            console.print("[red]Usage: /logs mode essential|verbose[/red]")
            return
        config = load_config(workspace)
        config["log_level"] = requested
        save_config(workspace, config)
        console.print(
            f"[green]Log mode set to {requested}.[/green] "
            "[dim]It applies to the next run.[/dim]"
        )
        return

    lines = [
        f"Logs for this project live in [bold]{root}[/bold]",
        "",
        "[bold]Human-readable report[/bold]",
        f"  this request             {run_example / 'report.md'}",
        f"  whole conversation       {session_example / 'report.md'}",
        "",
        "[bold]Internal evidence[/bold]",
        f"  {run_example / '.evidence'}",
        "",
        f"[bold]Detail level[/bold]  {level}"
        + (
            "  (detailed readable report + full redacted evidence)"
            if level == "verbose"
            else "  (concise readable report + compact evidence)"
        ),
        "  change with            /logs mode essential|verbose",
        f"  process override       {LOG_LEVEL_ENV_VAR}=essential|verbose",
        "",
        "[bold]Read them here[/bold]",
        "  /run report            the report for the last run",
        "  /run prompt            verbose-mode model prompt evidence",
        "  /run cot               verbose-mode reasoning evidence",
        "  /runs                  list recent runs",
        "  /logs open             show every path",
    ]
    console.print(Panel("\n".join(lines), title="SHAMSU logs"))

    if subcommand == "open":
        for label, path in (
            ("runs", runs_root),
            ("sessions", root / "sessions"),
            ("audit", root / "audit"),
            ("ledger config", config_path(workspace)),
            ("layout notes", root / "README.md"),
        ):
            console.print(f"  {label:<14} {path}")
        return

    recent = store.list_runs(workspace, limit=5)
    if not recent:
        console.print("[dim]No runs recorded yet - the next request creates one.[/dim]")
        return
    table = Table(title="Recent runs")
    for label in ("Run ID", "Status", "Prompt"):
        table.add_column(label)
    for item in recent:
        table.add_row(item.run_id, item.status, item.prompt_preview[:60])
    console.print(table)


def handle_run(
    user_input: str,
    workspace: Path,
    console: Console,
    approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
) -> None:
    _, _, rest = user_input.partition(" ")
    parts = rest.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else "last"
    argument = parts[1].strip() if len(parts) > 1 else ""
    run_id = store.resolve_run_id(workspace, "last") if subcommand == "last" else None
    if subcommand in {
        "show",
        "timeline",
        "decisions",
        "tools",
        "commands",
        "context",
        "diff",
        "export",
        "validate",
        "report",
        "narrative",
        "prompt",
        "cot",
    }:
        run_id = _resolve(workspace, argument, console)
    if subcommand == "last" and not run_id:
        console.print("[dim]No runs recorded yet.[/dim]")
        return
    if subcommand in {"last", "show"} and run_id:
        _summary(workspace, run_id, console)
        return
    if not run_id and subcommand not in {"clean"}:
        return
    if subcommand in {"report", "narrative"}:
        _narrative(workspace, run_id, console)
        return
    if subcommand in {"prompt", "cot"}:
        _model_artifacts(workspace, run_id, subcommand, console)
        return
    if subcommand == "timeline":
        events = store.load_events(workspace, run_id)
        if not events:
            console.print("[dim]No events recorded for this run.[/dim]")
            return
        table = Table(title=f"Run Timeline: {run_id}")
        for label in ("Time", "Event", "Detail"):
            table.add_column(label)
        for event in events:
            detail = {
                k: v
                for k, v in event.items()
                if k not in {"event_id", "run_id", "type", "timestamp"}
            }
            table.add_row(
                event.get("timestamp", ""),
                event.get("type", ""),
                json.dumps(detail, default=str)[:100],
            )
        console.print(table)
        return
    if subcommand == "decisions":
        records = store.load_decisions(workspace, run_id)
        columns = ("Decision", "Reason", "Chosen Action", "Outcome")
        rows = [
            (
                item.get("decision", ""),
                item.get("reason_summary", ""),
                item.get("chosen_action", ""),
                str(item.get("outcome", "")),
            )
            for item in records
        ]
        _print_records(
            console,
            f"Run Decisions: {run_id}",
            columns,
            rows,
            "No decisions recorded for this run.",
        )
        return
    if subcommand == "tools":
        records = store.load_tool_calls(workspace, run_id)
        rows = [
            (
                item.get("tool", ""),
                item.get("phase", ""),
                "ok" if item.get("ok") else ("failed" if item.get("phase") == "finished" else ""),
            )
            for item in records
        ]
        _print_records(
            console,
            f"Run Tool Calls: {run_id}",
            ("Tool", "Phase", "Outcome"),
            rows,
            "No tool calls recorded for this run.",
        )
        return
    if subcommand == "commands":
        records = [
            item
            for item in store.command_events(workspace, run_id)
            if item.get("type") == "command_finished"
        ]
        rows = [
            (
                str(item.get("command", ""))[:60],
                str(item.get("exit_code", "")),
                item.get("stdout_path", ""),
                item.get("stderr_path", ""),
            )
            for item in records
        ]
        _print_records(
            console,
            f"Run Commands: {run_id}",
            ("Command", "Exit", "Stdout", "Stderr"),
            rows,
            "No commands recorded for this run.",
        )
        return
    if subcommand == "context":
        records = store.load_context_records(workspace, run_id)
        console.print(
            Panel(json.dumps(records, indent=2, default=str), title=f"Contexts: {run_id}")
        ) if records else console.print("[dim]No context preview recorded for this run.[/dim]")
        return
    if subcommand == "diff":
        records = store.load_mutations(workspace, run_id)
        rows = [
            (
                item.get("transaction_id", ""),
                item.get("status", ""),
                str(len(item.get("touched_files", []))),
                str(item.get("rollback_available", False)),
            )
            for item in records
        ]
        _print_records(
            console,
            f"Run Mutations: {run_id}",
            ("Transaction", "Status", "Files", "Rollback"),
            rows,
            "No patches/mutations recorded for this run.",
        )
        return
    if subcommand == "export":
        console.print(f"[green]Exported run to {store.export_run(workspace, run_id)}[/green]")
        return
    if subcommand == "validate":
        result = store.validate_run(workspace, run_id)
        lines = [f"Integrity: {'valid' if result['ok'] else 'invalid'}"]
        lines.extend(f"Error: {item}" for item in result["errors"])
        lines.extend(f"Warning: {item}" for item in result["warnings"])
        console.print(
            Panel(
                "\n".join(lines),
                title=f"Run Validation: {run_id}",
                border_style="green" if result["ok"] else "red",
            )
        )
        return
    if subcommand == "clean":
        retention_days = int(load_config(workspace).get("retention_days", 30))
        stale = store.runs_older_than(workspace, retention_days)
        if not stale:
            console.print(f"[dim]No runs older than {retention_days} day(s).[/dim]")
            return
        request = ApprovalRequest(
            action_type="file_delete",
            description=f"Delete {len(stale)} ActionLedger run(s) older than {retention_days} day(s).",
            risk_level="medium",
            preview="\n".join(stale[:20]),
            working_dir=str(workspace),
            reason="Run folders under .shamsu/runs/ older than the retention window are being cleaned up.",
            target_paths=[f".shamsu/runs/{item}" for item in stale],
        )
        if not approval_func(request):
            console.print("[yellow]Clean cancelled; no runs were deleted.[/yellow]")
            return
        console.print(
            f"[green]Removed {len(store.clean_runs(workspace, retention_days))} run(s) older than {retention_days} day(s).[/green]"
        )
        return
    console.print(
        "[red]Usage: /run last|show|timeline|decisions|tools|commands|context|diff|validate|export|clean [run-id][/red]"
    )


def _print_records(
    console: Console,
    title: str,
    columns: tuple[str, ...],
    rows: list[tuple],
    empty_message: str,
) -> None:
    if not rows:
        console.print(f"[dim]{empty_message}[/dim]")
        return
    table = Table(title=title)
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*row)
    console.print(table)
