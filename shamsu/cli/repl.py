"""
Minimal REPL shell.

The selected workspace is the sandbox boundary for project reads and indexes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Callable

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from shamsu.agents.audit_workflow import AuditWorkflow
from shamsu.agents.bugfix_workflow import BugFixWorkflow
from shamsu.agents.code_edit_workflow import CodeEditWorkflow
from shamsu.agents.doc_workflow import DocumentationWorkflow
from shamsu.agents.qa_workflow import QAWorkflow
from shamsu.agents.test_generation_workflow import TestGenerationWorkflow
from shamsu.core.coordinator import Coordinator
from shamsu.indexer.walker import FileWalker
from shamsu.llm.manager import LLMManager
from shamsu.prd.input import PRDParseError, parse_prd_file
from shamsu.prd.project import build_project_spec
from shamsu.prd.state import create_generation_state, save_generation_state
from shamsu.retriever.search import SearchAgent
from shamsu.runtime.ollama import collect_status, pull_missing_models, repair_runtime, status_text
from shamsu.safety.approval import ask_approval
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.patch.engine import PatchEngine
from shamsu.session.manager import SessionLogger, SessionManager
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.tools.executor import CommandRunner
from shamsu.types import ApprovalRequest, ProjectSpec, RoutingDecision, SearchResult

if sys.platform == "win32":
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError
else:
    NoConsoleScreenBufferError = RuntimeError


class EmptySearchAgent:
    def search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return []

    def symbol_lookup(self, name: str) -> list[SearchResult]:
        return []

    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return []


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shamsu",
        description="Local-first coding agent REPL.",
    )
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace directory to treat as the sandbox boundary. Defaults to cwd.",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Resume a session by id or title prefix.",
    )
    parser.add_argument(
        "--new-session",
        nargs="?",
        const="SHAMSU Session",
        default=None,
        help="Create a new session with an optional title.",
    )
    return parser.parse_args(argv)


def resolve_workspace(workspace_arg: str | None) -> Path:
    workspace = Path(workspace_arg).expanduser() if workspace_arg else Path.cwd()
    resolved = workspace.resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise ValueError(f"Workspace does not exist or is not a directory: {resolved}")
    return resolved


def _print_help(console: Console) -> None:
    console.print(
        Panel(
            "\n".join(
                [
                    "Natural prompts:",
                    "  explain how auth works",
                    "  change the CLI banner text",
                    "  fix this traceback: <paste error>",
                    "  write tests for the parser",
                    "  audit this project for security issues",
                    "  update the README",
                    "",
                    "Commands:",
                    "  index                    Index the current workspace",
                    "  status                   Show index counts",
                    "  search <query>           Search indexed snippets",
                    "  symbols <name>           Look up indexed symbols",
                    "  parse-prd <file>         Parse a Markdown, TXT, or PDF PRD",
                    "  plan-prd <file>          Preview and approve a project plan",
                    "  generate-django <file>   Generate deterministic Django backend files",
                    "  models status            Show local Ollama/model status",
                    "  models pull              Pull missing local models",
                    "  models repair            Start Ollama and pull missing models",
                    "  sessions list            List workspace sessions",
                    "  sessions current         Show current session",
                    "  sessions show <id>       Show session metadata",
                    "  sessions resume <id>     Resume another session",
                    "  sessions rename <id> <title>",
                    "  sessions close [id]      Close a session",
                    "  sessions export <id>     Export redacted session bundle",
                    "  log tail                 Show recent session events",
                    "  edit <request>           Force code-edit workflow",
                    "  fix <bug/traceback>      Force bug-fix workflow",
                    "  test-gen <request>       Force test-generation workflow",
                    "  audit <request>          Force audit workflow",
                    "  docs <request>           Force README documentation workflow",
                    "  help                     Show commands",
                    "  exit                     Quit",
                    "",
                    "File edits are previewed and require approval before applying.",
                ]
            ),
            title="SHAMSU Commands",
        )
    )


def _index_db_path(workspace: Path) -> Path:
    return workspace / ".shamsu" / "index.db"


def _has_index(workspace: Path) -> bool:
    return _index_db_path(workspace).exists()


def _build_search_agent(workspace: Path) -> tuple[SearchAgent | EmptySearchAgent, bool]:
    if _has_index(workspace):
        return SearchAgent(_index_db_path(workspace)), True
    return EmptySearchAgent(), False


def _build_workspace_qa_workflow(workspace: Path) -> tuple[QAWorkflow, bool]:
    search, uses_real_index = _build_search_agent(workspace)
    return QAWorkflow(search=search), uses_real_index


def _handle_index(workspace: Path, console: Console) -> None:
    entries = FileWalker(workspace).index()
    console.print(f"Indexed {len(entries)} files.")
    for entry in entries[:20]:
        console.print(f"{entry.language:10} {entry.path}")
    if len(entries) > 20:
        console.print(f"... {len(entries) - 20} more")


def _handle_parse_prd(user_input: str, workspace: Path, console: Console) -> None:
    _, _, path_text = user_input.partition(" ")
    cleaned_path = path_text.strip().strip('"').strip("'")
    if not cleaned_path:
        console.print("[red]Usage: parse-prd <file>[/red]")
        return
    try:
        file_path = _resolve_workspace_file(cleaned_path, workspace)
    except SecurityError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    if not file_path.exists() or not file_path.is_file():
        console.print(f"[red]File not found: {file_path}[/red]")
        return
    try:
        parsed = parse_prd_file(file_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    console.print(f"Title: {parsed.title}")
    console.print(json.dumps(parsed.sections, indent=2))


def _handle_plan_prd(
    user_input: str,
    workspace: Path,
    console: Console,
    approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
    session_logger: SessionLogger | None = None,
) -> None:
    _, _, path_text = user_input.partition(" ")
    cleaned_path = path_text.strip().strip('"').strip("'")
    if not cleaned_path:
        console.print("[red]Usage: plan-prd <file>[/red]")
        return
    try:
        file_path = _resolve_workspace_file(cleaned_path, workspace)
    except SecurityError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    if not file_path.exists() or not file_path.is_file():
        console.print(f"[red]File not found: {file_path}[/red]")
        return
    try:
        parsed = parse_prd_file(file_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]")
        return

    _log_event(
        session_logger,
        "prd.parsed",
        {"path": str(file_path), "title": parsed.title, "sections": list(parsed.sections)},
        f"Parsed PRD {file_path.name}",
        workflow_id="plan-prd",
    )
    spec = build_project_spec(parsed)
    _log_event(
        session_logger,
        "project.planned",
        {
            "project": spec.project_name,
            "app": spec.app_name,
            "entities": [entity.name for entity in spec.entities],
            "endpoints": [endpoint.path for endpoint in spec.endpoints],
            "pages": [page.name for page in spec.pages],
            "files": [file.path for file in spec.generation_order],
        },
        f"Built project plan for {spec.project_name}",
        workflow_id="plan-prd",
    )
    _print_project_plan(spec, console)
    request = ApprovalRequest(
        action_type="file_write",
        description="Record this PRD project plan as approved for future generation.",
        risk_level="medium",
        preview=_project_plan_summary(spec),
        working_dir=str(workspace),
        reason="M3 only stores resume metadata; it does not generate project files.",
    )
    _log_event(session_logger, "approval.request", {"request": request}, request.description, "plan-prd")
    approved = approval_func(request)
    _log_event(
        session_logger,
        "approval.result",
        {"action_type": request.action_type, "approved": approved},
        f"Approval {'granted' if approved else 'denied'}: {request.description}",
        "plan-prd",
    )
    if not approved:
        console.print("[yellow]Project plan was not approved. No state was written.[/yellow]")
        return

    state = create_generation_state(spec, file_path, workspace, accepted=True)
    path = save_generation_state(state, workspace)
    console.print(f"[green]Project plan approved and saved: {path}[/green]")


def _handle_generate_django(
    user_input: str,
    workspace: Path,
    console: Console,
    approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
    session_logger: SessionLogger | None = None,
) -> None:
    _, _, path_text = user_input.partition(" ")
    cleaned_path = path_text.strip().strip('"').strip("'")
    if not cleaned_path:
        console.print("[red]Usage: generate-django <file>[/red]")
        return
    try:
        file_path = _resolve_workspace_file(cleaned_path, workspace)
    except SecurityError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    if not file_path.exists() or not file_path.is_file():
        console.print(f"[red]File not found: {file_path}[/red]")
        return
    try:
        parsed = parse_prd_file(file_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    _log_event(
        session_logger,
        "prd.parsed",
        {"path": str(file_path), "title": parsed.title, "sections": list(parsed.sections)},
        f"Parsed PRD {file_path.name}",
        workflow_id="generate-django",
    )
    spec = build_project_spec(parsed)
    _log_event(
        session_logger,
        "project.planned",
        {
            "project": spec.project_name,
            "app": spec.app_name,
            "entities": [entity.name for entity in spec.entities],
            "files": [file.path for file in spec.generation_order],
        },
        f"Built project plan for {spec.project_name}",
        workflow_id="generate-django",
    )
    _print_project_plan(spec, console)
    writer = DjangoProjectWriter(
        workspace,
        approval_func=approval_func,
        session_logger=session_logger,
    )
    try:
        state = writer.write_project(spec, file_path)
    except (PermissionError, ValueError) as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        return
    diagnostics = writer.check_project(spec)
    done = [step.file.path for step in state.generation_order if step.status.value == "done"]
    console.print(Panel("\n".join(f"- {path}" for path in done), title="Django Files Written"))
    if diagnostics:
        table = Table(title="Backend Consistency Diagnostics")
        table.add_column("File")
        table.add_column("Symbol")
        table.add_column("Message")
        for diagnostic in diagnostics:
            table.add_row(diagnostic.file_path, diagnostic.symbol, diagnostic.message)
        console.print(table)
    else:
        console.print("[green]Backend consistency check passed.[/green]")


def _print_project_plan(spec: ProjectSpec, console: Console) -> None:
    console.print(Panel(_project_plan_summary(spec), title="Project Plan"))

    entities = Table(title="Entities")
    entities.add_column("Entity")
    entities.add_column("Fields")
    entities.add_column("Relationships")
    for entity in spec.entities:
        fields = ", ".join(
            f"{field.name}:{field.django_type}" for field in entity.fields
        )
        entities.add_row(entity.name, fields, ", ".join(entity.relationships) or "-")
    console.print(entities)

    endpoints = Table(title="Endpoints")
    endpoints.add_column("Method")
    endpoints.add_column("Path")
    endpoints.add_column("Resource")
    endpoints.add_column("Auth")
    for endpoint in spec.endpoints:
        endpoints.add_row(
            endpoint.method,
            endpoint.path,
            endpoint.resource,
            "yes" if endpoint.auth_required else "no",
        )
    console.print(endpoints)

    pages = Table(title="Pages")
    pages.add_column("Name")
    pages.add_column("Type")
    pages.add_column("Resource")
    pages.add_column("Login")
    for page in spec.pages:
        pages.add_row(
            page.name,
            page.page_type,
            page.resource or "-",
            "yes" if page.requires_login else "no",
        )
    console.print(pages)

    files = Table(title="Generation Order")
    files.add_column("#")
    files.add_column("Path")
    files.add_column("Generator")
    files.add_column("Specialist")
    for index, file_spec in enumerate(spec.generation_order, start=1):
        files.add_row(
            str(index),
            file_spec.path,
            file_spec.generator,
            file_spec.specialist or "-",
        )
    console.print(files)


def _project_plan_summary(spec: ProjectSpec) -> str:
    return "\n".join(
        [
            f"Project: {spec.project_name}",
            f"App: {spec.app_name}",
            f"Theme: {spec.theme}",
            f"Entities: {len(spec.entities)}",
            f"Endpoints: {len(spec.endpoints)}",
            f"Pages: {len(spec.pages)}",
            f"Files planned: {len(spec.generation_order)}",
        ]
    )


def _resolve_workspace_file(path_text: str, workspace: Path) -> Path:
    return Sandbox(workspace).validate(path_text)


def _handle_status(workspace: Path, console: Console) -> None:
    db_path = _index_db_path(workspace)
    if not db_path.exists():
        console.print("[yellow]No index found. Run `index` first.[/yellow]")
        return

    conn = sqlite3.connect(db_path)
    try:
        files = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        symbols = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        snippets = conn.execute("SELECT COUNT(*) FROM snippets").fetchone()[0]
    finally:
        conn.close()
    console.print(f"Files: {files}")
    console.print(f"Symbols: {symbols}")
    console.print(f"Snippets: {snippets}")


def _handle_search(user_input: str, workspace: Path, console: Console) -> None:
    _, _, query = user_input.partition(" ")
    query = query.strip()
    if not query:
        console.print("[red]Usage: search <query>[/red]")
        return
    if not _has_index(workspace):
        console.print("[yellow]No index found. Run `index` first.[/yellow]")
        return
    results = SearchAgent(_index_db_path(workspace)).search(query, top_k=5)
    if not results:
        console.print("[yellow]No results.[/yellow]")
        return
    for result in results:
        console.print(
            f"{result.file_path}:{result.line_start}-{result.line_end} "
            f"score={result.score:.4f}"
        )


def _handle_symbols(user_input: str, workspace: Path, console: Console) -> None:
    _, _, name = user_input.partition(" ")
    name = name.strip()
    if not name:
        console.print("[red]Usage: symbols <name>[/red]")
        return
    if not _has_index(workspace):
        console.print("[yellow]No index found. Run `index` first.[/yellow]")
        return
    results = SearchAgent(_index_db_path(workspace)).symbol_lookup(name)
    if not results:
        console.print("[yellow]No symbols found.[/yellow]")
        return
    for result in results:
        symbol = result.symbol_name or name
        console.print(f"{symbol}: {result.file_path}:{result.line_start}-{result.line_end}")


def _handle_models(user_input: str, console: Console) -> None:
    parts = user_input.split(maxsplit=1)
    command = parts[1].strip().lower() if len(parts) > 1 else "status"
    if command == "status":
        _print_runtime_status(console)
        return
    if command == "pull":
        status = collect_status()
        if not status.ollama_found:
            console.print("[red]Ollama was not found. Run `models repair` after installing Ollama.[/red]")
            return
        if not status.server_running:
            console.print("[yellow]Ollama is not running. Run `models repair`.[/yellow]")
            return
        if not status.missing_models:
            console.print("[green]All required local models are installed.[/green]")
            return
        console.print(f"Pulling missing models: {', '.join(status.missing_models)}")
        results = pull_missing_models(Path(status.ollama_path), status.missing_models)
        for model, exit_code in results.items():
            style = "green" if exit_code == 0 else "red"
            console.print(f"[{style}]{model}: exit {exit_code}[/{style}]")
        _print_runtime_status(console)
        return
    if command == "repair":
        status = repair_runtime(pull_models=True)
        _print_runtime_status(console, status=status)
        return
    console.print("[red]Usage: models status|pull|repair[/red]")


def _print_runtime_status(console: Console, status=None) -> None:
    status = status or collect_status()
    table = Table(title="Local Runtime")
    table.add_column("Item")
    table.add_column("Value")
    table.add_row("Inference", "local-only Ollama")
    table.add_row("Endpoint", status.base_url)
    table.add_row("Ollama", status.ollama_path or "not found")
    table.add_row("Server", "running" if status.server_running else "not running")
    table.add_row("Missing models", ", ".join(status.missing_models) or "none")
    table.add_row("Status", status_text(status))
    console.print(table)


def _start_session(args: argparse.Namespace, workspace: Path, console: Console) -> SessionLogger:
    manager = SessionManager(workspace)
    if args.new_session is not None:
        logger = manager.create_session(args.new_session)
    elif args.session:
        logger = manager.resume_session(args.session)
    else:
        logger = manager.get_or_create_latest()
    console.print(f"[dim]Session: {logger.session_id} ({logger.metadata.title})[/dim]")
    return logger


def _handle_sessions(
    user_input: str,
    manager: SessionManager,
    current: SessionLogger,
    console: Console,
) -> SessionLogger:
    parts = user_input.split(maxsplit=3)
    command = parts[1].lower() if len(parts) > 1 else "list"
    try:
        if command == "list":
            table = Table(title="Sessions")
            table.add_column("ID")
            table.add_column("Title")
            table.add_column("Status")
            table.add_column("Updated")
            table.add_column("Events")
            for item in manager.list_sessions():
                table.add_row(item.session_id, item.title, item.status, item.updated_at, str(item.event_count))
            console.print(table)
            return current
        if command == "current":
            _print_session(current.metadata, console)
            return current
        if command == "show" and len(parts) >= 3:
            _print_session(manager.resolve(parts[2]), console)
            return current
        if command == "resume" and len(parts) >= 3:
            resumed = manager.resume_session(parts[2])
            console.print(f"[green]Resumed session {resumed.session_id}[/green]")
            return resumed
        if command == "rename" and len(parts) >= 4:
            renamed = manager.rename_session(parts[2], parts[3])
            console.print(f"[green]Renamed session {renamed.session_id}[/green]")
            if renamed.session_id == current.session_id:
                return SessionLogger(manager, renamed)
            return current
        if command == "close":
            target = parts[2] if len(parts) >= 3 else current.session_id
            closed = manager.close_session(target)
            console.print(f"[yellow]Closed session {closed.session_id}[/yellow]")
            if closed.session_id == current.session_id:
                return manager.create_session("SHAMSU Session")
            return current
        if command == "export" and len(parts) >= 3:
            path = manager.export_session(parts[2])
            console.print(f"[green]Exported session bundle: {path}[/green]")
            return current
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return current
    console.print("[red]Usage: sessions list|current|show|resume|rename|close|export[/red]")
    return current


def _print_session(metadata, console: Console) -> None:
    table = Table(title=f"Session {metadata.session_id}")
    table.add_column("Field")
    table.add_column("Value")
    for key, value in metadata.__dict__.items():
        table.add_row(key, str(value))
    console.print(table)


def _handle_log(user_input: str, logger: SessionLogger, console: Console) -> None:
    parts = user_input.split()
    count = 20
    if len(parts) >= 3 and parts[1].lower() == "tail":
        try:
            count = int(parts[2])
        except ValueError:
            count = 20
    events = logger.tail(count=count)
    if not events:
        console.print("[yellow]No session events yet.[/yellow]")
        return
    table = Table(title=f"Last {len(events)} Events")
    table.add_column("Time")
    table.add_column("Type")
    table.add_column("Summary")
    for event in events:
        table.add_row(event["timestamp"], event["event_type"], event.get("summary", ""))
    console.print(table)


def _log_event(
    session_logger: SessionLogger | None,
    event_type: str,
    payload: dict,
    summary: str,
    workflow_id: str | None = None,
) -> None:
    if session_logger:
        session_logger.log(event_type, payload, summary, workflow_id=workflow_id)


async def _handle_request(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    if _looks_like_django_generation_request(user_input):
        generate_command = f"generate-django {_extract_prd_path_from_prompt(user_input)}"
        _handle_generate_django(generate_command, workspace, console, session_logger=session_logger)
        return
    if _looks_like_prd_plan_request(user_input):
        plan_command = f"plan-prd {_extract_prd_path_from_prompt(user_input)}"
        _handle_plan_prd(plan_command, workspace, console, session_logger=session_logger)
        return
    search, uses_real_index = _build_search_agent(workspace)
    if not uses_real_index:
        console.print(
            "[yellow]No index found. Run `index` first for project-specific QA.[/yellow]"
        )
    llm = LLMManager(session_logger=session_logger)
    decision = await _route_prompt(user_input, llm)
    _print_decision(decision, console)

    try:
        _log_event(
            session_logger,
            "workflow.started",
            {"intent": decision.intent, "prompt": user_input},
            f"Workflow started: {decision.intent}",
            workflow_id=decision.intent,
        )
        if decision.intent in {"qa", "explain"}:
            await _run_qa(user_input, workspace, console, llm)
        elif decision.intent == "code_edit":
            await _run_code_edit(user_input, workspace, search, console, llm, session_logger)
        elif decision.intent == "bug_fix":
            await _run_bug_fix(user_input, workspace, search, console, llm, session_logger)
        elif decision.intent == "audit":
            await _run_audit(user_input, search, console, llm)
        elif decision.intent == "test_gen":
            await _run_test_generation(user_input, workspace, search, console, llm, session_logger)
        elif decision.intent == "doc_gen":
            await _run_docs(user_input, workspace, search, console, llm, session_logger)
        else:
            console.print("[yellow]Project generation is not wired into this CLI yet.[/yellow]")
        _log_event(
            session_logger,
            "workflow.finished",
            {"intent": decision.intent},
            f"Workflow finished: {decision.intent}",
            workflow_id=decision.intent,
        )
    except Exception as exc:
        message = str(exc)
        if _looks_like_runtime_error(message):
            message = f"{message}\n\nRun `models status` or `models repair`."
        _log_event(
            session_logger,
            "workflow.failed",
            {"intent": decision.intent, "error": message},
            f"Workflow failed: {decision.intent}",
            workflow_id=decision.intent,
        )
        console.print(Panel(message, title="Workflow Unavailable", border_style="red"))


async def _route_prompt(user_input: str, llm: LLMManager) -> RoutingDecision:
    forced = _forced_decision(user_input)
    if forced is not None:
        return forced
    try:
        return await llm.route(user_input, "Indexed workspace selected in SHAMSU CLI.")
    except Exception:
        return _keyword_decision(user_input)


def _forced_decision(user_input: str) -> RoutingDecision | None:
    lowered = user_input.lower()
    command_to_intent = {
        "edit ": "code_edit",
        "fix ": "bug_fix",
        "test-gen ": "test_gen",
        "audit ": "audit",
        "docs ": "doc_gen",
    }
    for prefix, intent in command_to_intent.items():
        if lowered.startswith(prefix):
            return RoutingDecision(
                intent=intent,
                complexity="single",
                steps=[{"id": 1, "specialist": intent, "task": user_input[len(prefix):]}],
                needs_tools=["search"],
                confidence=1.0,
            )
    return None


def _keyword_decision(user_input: str) -> RoutingDecision:
    text = user_input.lower()
    intent = "qa"
    if _looks_like_django_generation_request(user_input):
        intent = "generate"
    elif _looks_like_prd_plan_request(user_input):
        intent = "generate"
    elif any(word in text for word in ("traceback", "exception", "error:", "failing", "fix ")):
        intent = "bug_fix"
    elif any(word in text for word in ("write tests", "generate tests", "test for", "pytest")):
        intent = "test_gen"
    elif any(word in text for word in ("audit", "review", "security issue")):
        intent = "audit"
    elif any(word in text for word in ("readme", "documentation", "docs")):
        intent = "doc_gen"
    elif any(word in text for word in ("change", "edit", "add ", "remove ", "update")):
        intent = "code_edit"
    return RoutingDecision(
        intent=intent,
        complexity="single",
        steps=[{"id": 1, "specialist": intent, "task": user_input}],
        needs_tools=["search"],
        confidence=0.35,
    )


def _looks_like_prd_plan_request(user_input: str) -> bool:
    text = user_input.lower()
    return (
        any(phrase in text for phrase in ("plan project", "project plan", "plan-prd"))
        and bool(_extract_prd_path_from_prompt(user_input))
    )


def _looks_like_django_generation_request(user_input: str) -> bool:
    text = user_input.lower()
    return (
        any(phrase in text for phrase in ("generate django", "generate project", "build django"))
        and bool(_extract_prd_path_from_prompt(user_input))
    )


def _extract_prd_path_from_prompt(user_input: str) -> str:
    quoted = re.search(r"['\"]([^'\"]+\.(?:md|markdown|txt|pdf))['\"]", user_input, re.I)
    if quoted:
        return quoted.group(1)
    match = re.search(r"([^\s]+?\.(?:md|markdown|txt|pdf))", user_input, re.I)
    return match.group(1) if match else ""


def _print_decision(decision: RoutingDecision, console: Console) -> None:
    console.print(
        f"[dim]intent={decision.intent} confidence={decision.confidence:.2f}[/dim]"
    )


async def _run_qa(
    user_input: str,
    workspace: Path,
    console: Console,
    llm: LLMManager,
) -> None:
    qa_workflow, _uses_real_index = _build_workspace_qa_workflow(workspace)
    result = await Coordinator(llm=llm, qa_workflow=qa_workflow).handle(user_input)
    if result.answer:
        title = f"Answer ({result.model_used})" if result.model_used else "Answer"
        console.print(Panel(result.answer, title=title))
    elif result.fallback_reason:
        console.print(f"[yellow]{result.fallback_reason}[/yellow]")
    if result.preview:
        console.print(Panel(result.preview, title="Context Preview"))


async def _run_code_edit(
    user_input: str,
    workspace: Path,
    search: SearchAgent | EmptySearchAgent,
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
) -> None:
    kwargs = {}
    if session_logger:
        kwargs["patch_engine"] = PatchEngine(workspace, session_logger=session_logger)
    result = await CodeEditWorkflow(workspace, search=search, llm=llm, **kwargs).run(
        _strip_forced_prefix(user_input, "edit")
    )
    _print_patch_result("Code Edit", result.applied, result.changed_files, result.error, console)


async def _run_bug_fix(
    user_input: str,
    workspace: Path,
    search: SearchAgent | EmptySearchAgent,
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
) -> None:
    kwargs = {}
    if session_logger:
        kwargs["patch_engine"] = PatchEngine(workspace, session_logger=session_logger)
    result = await BugFixWorkflow(workspace, search=search, llm=llm, **kwargs).run(
        _strip_forced_prefix(user_input, "fix")
    )
    _print_patch_result("Bug Fix", result.applied, result.changed_files, result.error, console)


async def _run_audit(
    user_input: str,
    search: SearchAgent | EmptySearchAgent,
    console: Console,
    llm: LLMManager,
) -> None:
    report = await AuditWorkflow(search=search, llm=llm).run(
        _strip_forced_prefix(user_input, "audit")
    )
    table = Table(title="Audit Findings")
    table.add_column("Severity")
    table.add_column("File")
    table.add_column("Line")
    table.add_column("Reason")
    if not report.findings:
        console.print("[green]No structured findings returned.[/green]")
        return
    for finding in report.findings:
        table.add_row(
            finding.severity,
            finding.file_path,
            str(finding.line_start or ""),
            finding.reason,
        )
    console.print(table)


async def _run_test_generation(
    user_input: str,
    workspace: Path,
    search: SearchAgent | EmptySearchAgent,
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
) -> None:
    kwargs = {}
    if session_logger:
        kwargs["patch_engine"] = PatchEngine(workspace, session_logger=session_logger)
        kwargs["command_runner"] = CommandRunner(workspace, session_logger=session_logger)
    result = await TestGenerationWorkflow(workspace, search=search, llm=llm, **kwargs).run(
        _strip_forced_prefix(user_input, "test-gen")
    )
    _print_patch_result("Test Generation", result.applied, result.changed_files, result.error, console)


async def _run_docs(
    user_input: str,
    workspace: Path,
    search: SearchAgent | EmptySearchAgent,
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
) -> None:
    result = await DocumentationWorkflow(
        search=search,
        llm=llm,
        workspace_root=workspace,
        **(
            {"patch_engine": PatchEngine(workspace, session_logger=session_logger)}
            if session_logger
            else {}
        ),
    ).apply_readme_update(request=_strip_forced_prefix(user_input, "docs"))
    _print_patch_result("Documentation", result.applied, result.changed_files, result.error, console)


def _print_patch_result(
    title: str,
    applied: bool,
    changed_files: list[str],
    error: str,
    console: Console,
) -> None:
    if applied:
        files = "\n".join(f"- {path}" for path in changed_files) or "No files reported."
        console.print(Panel(files, title=f"{title} Applied", border_style="green"))
        return
    console.print(Panel(error or "No changes applied.", title=f"{title} Not Applied", border_style="yellow"))


def _looks_like_runtime_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in ("connect", "connection", "localhost:11434", "ollama", "model")
    )


def _strip_forced_prefix(user_input: str, command: str) -> str:
    prefix = f"{command} "
    if user_input.lower().startswith(prefix):
        return user_input[len(prefix):].strip()
    return user_input


def _make_prompt_session(workspace: Path) -> PromptSession | None:
    style = Style.from_dict(
        {
            "prompt": "ansigreen bold",
            "workspace": "ansiblue",
        }
    )
    try:
        return PromptSession(
            history=InMemoryHistory(),
            style=style,
            bottom_toolbar=f"Workspace: {workspace} | help | index | exit",
        )
    except NoConsoleScreenBufferError:
        return None


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    console = Console()
    console.print(Panel("SHAMSU v0.3.0\nLocal AI coding agent", title="SHAMSU"))

    try:
        workspace = resolve_workspace(args.workspace)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    console.print(f"[dim]Workspace: {workspace}[/dim]")
    console.print(f"[dim]{status_text(collect_status())}[/dim]")
    session_manager = SessionManager(workspace)
    try:
        session_logger = _start_session(args, workspace, console)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    console.print("[dim]Type a prompt, or `help` for commands.[/dim]\n")
    session = _make_prompt_session(workspace)

    while True:
        try:
            if session is None:
                user_input = input("shamsu> ").strip()
            else:
                user_input = session.prompt([("class:prompt", "shamsu> ")]).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            sys.exit(0)

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Goodbye.")
            break
        session_logger.log(
            "user.prompt",
            {"prompt": user_input},
            "User submitted prompt",
            workflow_id="repl",
        )
        if user_input.lower() == "help":
            _print_help(console)
            continue
        if user_input.lower() == "index":
            _handle_index(workspace, console)
            continue
        if user_input.lower() == "status":
            _handle_status(workspace, console)
            continue
        if user_input.lower().startswith("search "):
            _handle_search(user_input, workspace, console)
            continue
        if user_input.lower().startswith("symbols "):
            _handle_symbols(user_input, workspace, console)
            continue
        if user_input.lower().startswith("parse-prd "):
            _handle_parse_prd(user_input, workspace, console)
            continue
        if user_input.lower().startswith("plan-prd "):
            _handle_plan_prd(user_input, workspace, console, session_logger=session_logger)
            continue
        if user_input.lower().startswith("generate-django "):
            _handle_generate_django(user_input, workspace, console, session_logger=session_logger)
            continue
        if user_input.lower().startswith("models"):
            _handle_models(user_input, console)
            continue
        if user_input.lower().startswith("sessions"):
            session_logger = _handle_sessions(user_input, session_manager, session_logger, console)
            continue
        if user_input.lower() == "log" or user_input.lower().startswith("log "):
            _handle_log(user_input, session_logger, console)
            continue

        asyncio.run(_handle_request(user_input, workspace, console, session_logger))


if __name__ == "__main__":
    main()
