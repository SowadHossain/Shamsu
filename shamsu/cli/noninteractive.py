"""Headless entry point for one complete SHAMSU request lifecycle."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import time
from contextlib import ExitStack
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

from rich.console import Console

from shamsu.action_ledger import store as ledger_store
from shamsu.indexer.policy import DEFAULT_EXCLUDED_FILES, walk_workspace_files
from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.runtime.models import initialize_model_tier
from shamsu.memory.queue import flush_memory_queues
from shamsu.safety.approval_context import approval_override
from shamsu.safety.dry_run import DryRunRecorder, dry_run as dry_run_context
from shamsu.verify import contract as run_contract
from shamsu.session.manager import SessionManager
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import WebTool
from shamsu.types import ApprovalRequest


ApprovalMode = Literal["allow", "deny"]


@dataclass(frozen=True)
class HeadlessRunResult:
    schema_version: int
    run_id: str
    session_id: str
    turn_id: str
    status: str
    route: str
    final_response: str
    error: str
    duration_s: float
    timeout_phase: str
    dry_run: bool = False
    approvals: list[dict[str, Any]] = field(default_factory=list)
    planned_actions: list[dict[str, Any]] = field(default_factory=list)
    # What a dry run WOULD have written. Empty on a real run.
    planned_mutations: list[dict[str, Any]] = field(default_factory=list)
    operations: list[str] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    changed_files: list[dict[str, str]] = field(default_factory=list)
    transaction_ids: list[str] = field(default_factory=list)
    verification: list[dict[str, Any]] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    artifact_integrity: dict[str, bool] = field(default_factory=dict)
    # Structural: are the artifacts well-formed? Says nothing about the task.
    run_validation: dict[str, Any] = field(default_factory=dict)
    # Semantic: did the run keep the promises in the prompt? This is the check
    # that fails a run which built the wrong thing with perfect bookkeeping.
    contract: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Read-only inspection commands that headless mode can execute directly.
# Slash commands used to be handed straight to the model as if they were
# English: `/run show <id>` made the agent try to RUN the file it saw in the
# prompt instead of showing the run. These handlers are standalone,
# workspace-scoped, and mutate nothing, so they are safe to dispatch here.
# Anything else slash-prefixed is refused honestly rather than guessed at -
# the full command chain lives in the interactive loop.
_HEADLESS_COMMAND_HANDLERS: dict[str, str] = {
    "runs": "_handle_runs",
    "run": "_handle_run",
    "doctor": "_handle_doctor",
    "tasks": "_handle_tasks",
    "permissions": "_handle_permissions",
}


def _dispatch_slash_command(
    normalized: str, workspace: Path, console: Console
) -> tuple[bool, str]:
    """Run a read-only inspection command. Returns (handled, message)."""
    from shamsu.cli import repl

    name = normalized.split(maxsplit=1)[0].lower()
    handler_name = _HEADLESS_COMMAND_HANDLERS.get(name)
    if handler_name is None:
        return False, (
            f"/{name} is not available in headless mode. Supported here: "
            + ", ".join(f"/{item}" for item in sorted(_HEADLESS_COMMAND_HANDLERS))
            + ". Use the interactive REPL for the rest."
        )
    handler = getattr(repl, handler_name)
    if handler_name == "_handle_doctor":
        handler(workspace, console)
    else:
        handler(normalized, workspace, console)
    return True, ""


class _ApprovalScript:
    def __init__(self, policy: ApprovalMode | Literal["dry-run"] | Sequence[bool]) -> None:
        self._mode = policy if isinstance(policy, str) else "deny"
        self._remaining = list(policy) if not isinstance(policy, str) else []
        self.records: list[dict[str, Any]] = []

    def __call__(self, request: ApprovalRequest) -> bool:
        if self._remaining:
            approved = bool(self._remaining.pop(0))
            source = "script"
        elif self._mode == "dry-run":
            # File mutations never reach an approval gate under dry run - the
            # tool layer intercepts them first and records a planned action.
            # What DOES arrive here is everything with a real side effect
            # (commands, web fetches), and a dry run must not perform those.
            # Approving reads keeps the agent able to inspect the workspace so
            # its plan is grounded in what is actually there.
            approved = request.action_type in {"file_read", "search"}
            source = "policy:dry-run"
        else:
            approved = self._mode == "allow"
            source = f"policy:{self._mode}"
        self.records.append(
            {
                "request": asdict(request),
                "action_type": request.action_type,
                "description": request.description,
                "approved": approved,
                "decision_scope": "once" if approved else "none",
                "source": source,
            }
        )
        return approved


async def run_prompt(
    workspace: Path,
    prompt: str,
    *,
    approval: ApprovalMode | Sequence[bool] = "deny",
    timeout_s: float = 300.0,
    session: str | None = None,
    dry_run: bool = False,
) -> HeadlessRunResult:
    """Run one prompt through the same dispatcher used by the interactive REPL.

    Human-facing Rich output is captured. The returned object is assembled from
    persisted ActionLedger and session evidence, not from model self-report.
    """
    # Imported lazily so repl.py can expose this runner as a CLI subcommand
    # without introducing an import cycle.
    from shamsu.cli.command_router import CommandRouter
    from shamsu.cli.repl import (
        SYSTEM_COMMANDS,
        _finish_current_run,
        _handle_request,
        _make_approval_manager,
    )

    command_router = CommandRouter(SYSTEM_COMMANDS)

    root = Path(workspace).resolve()
    if not root.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {root}")
    clean_prompt = prompt.lstrip("\ufeff").strip()
    if not clean_prompt:
        raise ValueError("Prompt must not be empty")

    from shamsu.runtime.state_upgrade import upgrade_workspace_state

    upgrade_workspace_state(root)
    initialize_model_tier(root)
    before = _workspace_snapshot(root)
    started = time.perf_counter()
    console_buffer = io.StringIO()
    console = Console(
        file=console_buffer,
        force_terminal=False,
        color_system=None,
        width=120,
    )
    manager = SessionManager(root)
    logger = manager.resume_session(session) if session else manager.create_session("Headless Run")
    previous_user_prompt = logger.metadata.last_user_prompt
    user_event = logger.log(
        "user.prompt",
        {"prompt": clean_prompt},
        "User submitted prompt",
        workflow_id="headless",
    )
    manager.maybe_auto_title(logger, clean_prompt)
    approvals = _ApprovalScript("dry-run" if dry_run else approval)
    ledger = start_run(root, clean_prompt, session_logger=logger)
    set_current_run(ledger)
    error = ""
    timeout_phase = ""
    browser_tool: BrowserTool | None = None
    recorder: DryRunRecorder | None = None

    try:
        with ExitStack() as stack:
            stack.enter_context(approval_override(approvals))
            if dry_run:
                recorder = stack.enter_context(dry_run_context())
            web_tool = WebTool(
                workspace=root,
                session_logger=logger,
                approval_manager=_make_approval_manager(root, logger, console),
                action_ledger=ledger,
            )
            browser_tool = BrowserTool(
                root,
                session_logger=logger,
                approval_manager=_make_approval_manager(root, logger, console),
                action_ledger=ledger,
            )
            try:
                if clean_prompt.startswith("/"):
                    # A slash command is a COMMAND, not a prompt. Resolve it
                    # before the model ever sees it - otherwise `/run show <id>`
                    # reads as English and the agent goes off and runs a script.
                    route = command_router.route(clean_prompt)
                    if not route.valid:
                        message = route.error
                        if route.suggestions:
                            message += " Did you mean: " + ", ".join(route.suggestions)
                        console.print(message)
                        ledger.finish(message, status="failed")
                    else:
                        handled, refusal = _dispatch_slash_command(
                            route.normalized, root, console
                        )
                        if not handled:
                            console.print(refusal)
                        ledger.finish(
                            console_buffer.getvalue().strip() or route.normalized,
                            status="success" if handled else "failed",
                        )
                else:
                    await asyncio.wait_for(
                        _handle_request(
                            clean_prompt,
                            root,
                            console,
                            web_tool,
                            browser_tool,
                            previous_user_prompt=previous_user_prompt,
                            session_logger=logger,
                        ),
                        timeout=max(float(timeout_s), 0.01),
                    )
            except TimeoutError:
                timeout_phase = "request"
                error = f"Request timed out after {timeout_s:g}s"
                ledger.finish(error, status="timed_out")
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                failed_tools = [
                    record
                    for record in ledger_store.load_tool_calls(root, ledger.run_id)
                    if record.get("phase") == "finished" and not record.get("ok", False)
                ]
                if failed_tools:
                    latest = failed_tools[-1]
                    error += (
                        f"\nLast tool failure: {latest.get('tool', 'unknown')}: "
                        f"{latest.get('message', '')}"
                    )
                ledger.fail(error)
    finally:
        if browser_tool is not None:
            browser_tool.close()
        changed_now = _changed_files(before, _workspace_snapshot(root))
        contract_now = run_contract.check(
            run_contract.derive(clean_prompt, dry_run=dry_run),
            changed_files=changed_now,
            planned_mutations=recorder.as_dicts() if recorder is not None else [],
        )
        manifest_now = ledger_store.load_manifest(root, ledger.run_id) or {}
        if manifest_now.get("status") == "running":
            if not contract_now.ok:
                ledger.log_event("contract_failed", violations=contract_now.violations)
            if dry_run:
                ledger.finish(
                    recorder.summary() if recorder is not None else "Dry run complete.",
                    status="dry_run",
                )
        _finish_current_run(root, ledger)
        clear_current_run()
        flush_memory_queues()

    duration_s = time.perf_counter() - started
    return _build_result(
        root,
        ledger.run_id,
        logger.session_id,
        str(user_event.get("event_id", "")),
        logger.get_last_route(),
        approvals.records,
        before,
        duration_s,
        timeout_phase,
        error,
        dry_run,
        recorder,
        clean_prompt,
    )


def run_cli(args: Any) -> int:
    try:
        result = asyncio.run(
            run_prompt(
                Path(args.workspace) if args.workspace else Path.cwd(),
                args.prompt,
                approval=args.approval,
                timeout_s=args.timeout,
                session=args.session,
                dry_run=bool(getattr(args, "dry_run", False)),
            )
        )
    except Exception as exc:
        result = HeadlessRunResult(
            schema_version=1,
            run_id="",
            session_id="",
            turn_id="",
            status="failed",
            route="",
            final_response="",
            error=f"{type(exc).__name__}: {exc}",
            duration_s=0.0,
            timeout_phase="setup",
        )
    contract_ok = bool(result.contract.get("ok", True))
    if args.output == "json":
        print(json.dumps(result.to_dict(), indent=2, ensure_ascii=True))
    else:
        print(result.final_response or result.error)
        for violation in result.contract.get("violations", []):
            print(f"contract violation: {violation}")
    status_ok = result.status in {"success", "success_unverified", "dry_run"}
    # A run can be structurally perfect and still have done the wrong job -
    # that is precisely the failure mode the 2026-07-20 dogfood exposed, where
    # every artifact validated `ok` on a run that built the wrong product. A
    # broken promise is a failed run, so it has to move the exit code.
    return 0 if (status_ok and contract_ok) else 1


def _build_result(
    workspace: Path,
    run_id: str,
    session_id: str,
    turn_id: str,
    route_state: dict[str, Any],
    approvals: list[dict[str, Any]],
    before: dict[str, str],
    duration_s: float,
    timeout_phase: str,
    error: str,
    dry_run: bool,
    recorder: DryRunRecorder | None = None,
    prompt: str = "",
) -> HeadlessRunResult:
    manifest = ledger_store.load_manifest(workspace, run_id) or {}
    events = ledger_store.load_events(workspace, run_id)
    mutations = ledger_store.load_mutations(workspace, run_id)
    run_dir = ledger_store.runs_dir(workspace) / run_id
    artifact_paths = {
        "run_dir": run_dir,
        "manifest": run_dir / "manifest.json",
        "events": run_dir / "events.jsonl",
        "decisions": run_dir / "decisions.jsonl",
        "tool_calls": run_dir / "tool-calls.jsonl",
        "model_calls": run_dir / "model-calls.jsonl",
        "mutations": run_dir / "mutations" / "mutations.jsonl",
        "context_preview": run_dir / "context-preview.json",
        "contexts": run_dir / "contexts",
        "final_output": run_dir / "final-output.md",
        "summary": run_dir / "summary.json",
    }
    operations = [
        str(event.get("task_type", ""))
        for event in events
        if event.get("type") == "task_classified" and event.get("task_type")
    ]
    verification = [
        event
        for event in events
        if event.get("type")
        in {"verification_started", "verification_passed", "verification_failed"}
    ]
    transaction_ids = list(
        dict.fromkeys(
            str(item.get("transaction_id"))
            for item in mutations
            if item.get("transaction_id")
        )
    )
    manifest_status = str(manifest.get("status", "failed"))
    result_status = (
        "dry_run"
        if dry_run and manifest_status in {"success", "success_unverified", "denied"}
        else manifest_status
    )
    final_response = ledger_store.load_final_output(workspace, run_id)
    planned_mutations = recorder.as_dicts() if recorder is not None else []
    changed = _changed_files(before, _workspace_snapshot(workspace))
    # Checked against the filesystem diff, never against the model's account of
    # itself - a run that reports success while changing the wrong files is
    # exactly what this is for.
    contract_result = run_contract.check(
        run_contract.derive(prompt, dry_run=dry_run),
        changed_files=changed,
        planned_mutations=planned_mutations,
    )
    if result_status == "dry_run":
        # Report what the agent PLANNED, which is the question a dry run asks.
        # The old summary counted approval-gated actions instead - and since
        # every gate was denied, that number was really "how far did the agent
        # get before giving up", which for a create-file prompt was zero.
        final_response = recorder.summary() if recorder is not None else (
            "Dry run complete: no file changes were planned."
        )
    return HeadlessRunResult(
        schema_version=1,
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        status=result_status,
        route=str(route_state.get("route", "")),
        final_response=final_response,
        error=error,
        duration_s=round(duration_s, 6),
        timeout_phase=timeout_phase,
        dry_run=dry_run,
        approvals=approvals,
        planned_actions=[record["request"] for record in approvals],
        planned_mutations=planned_mutations,
        operations=operations,
        tool_calls=ledger_store.load_tool_calls(workspace, run_id),
        commands=ledger_store.command_events(workspace, run_id),
        changed_files=changed,
        transaction_ids=transaction_ids,
        verification=verification,
        artifacts={name: str(path) for name, path in artifact_paths.items()},
        artifact_integrity={name: path.exists() for name, path in artifact_paths.items()},
        run_validation=ledger_store.validate_run(workspace, run_id),
        contract=contract_result.to_dict(),
    )


def _workspace_snapshot(workspace: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in walk_workspace_files(workspace):
        relative = path.relative_to(workspace)
        if "__pycache__" in relative.parts:
            continue
        try:
            snapshot[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    # Index policy files are deliberately excluded from retrieval, but they are
    # still real root-level workspace files. Include them in the behavioral
    # contract so a read-only run cannot create `.cbmignore` invisibly.
    for name in DEFAULT_EXCLUDED_FILES:
        path = workspace / name
        if not path.is_file():
            continue
        try:
            snapshot[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return snapshot


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[dict[str, str]]:
    changes: list[dict[str, str]] = []
    for path in sorted(before.keys() | after.keys()):
        old = before.get(path, "")
        new = after.get(path, "")
        if old == new:
            continue
        change = "created" if not old else "deleted" if not new else "modified"
        changes.append(
            {
                "path": path,
                "change": change,
                "before_sha256": old,
                "after_sha256": new,
            }
        )
    return changes
