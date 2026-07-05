"""
Minimal REPL shell.

The selected workspace is the sandbox boundary for project reads and indexes.
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import difflib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table
from rich.text import Text

from shamsu.agents.audit_workflow import AuditWorkflow
from shamsu.agents.bugfix_workflow import BugFixWorkflow
from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.agents.code_edit_workflow import CodeEditWorkflow
from shamsu.agents.doc_workflow import DocumentationWorkflow
from shamsu.agents.error_feedback_loop import ErrorFeedbackLoop
from shamsu.agents.full_pipeline import FullDjangoPipeline, FullPipelineResult
from shamsu.agents.orchestrator import AgentOrchestrator
from shamsu.cli.command_router import CommandRouter
from shamsu.agents.qa_workflow import NO_LIVE_TOOLS_NOTICE, QAWorkflow
from shamsu.agents.test_generation_workflow import TestGenerationWorkflow
from shamsu.core.coordinator import Coordinator
from shamsu.indexer.walker import FileWalker, ensure_index
from shamsu.llm.manager import LLMManager, ModelPullProgress
from shamsu.prd.input import PRDParseError, is_prd_filename, parse_prd_file
from shamsu.prd.project import build_project_spec
from shamsu.prd.state import create_generation_state, save_generation_state
from shamsu.registry.schema import Category
from shamsu.retriever.search import SearchAgent
from shamsu.tasks.state import (
    MilestoneTask,
    advance_phase,
    create_task,
    list_task_ids,
    load_task,
    mark_step_done,
    mark_step_failed,
    mark_step_running,
    save_task,
)
from shamsu.runtime.doctor import find_ancestor_workspace, format_report, run_doctor
from shamsu.runtime.models import model_for_role
from shamsu.runtime.ollama import (
    collect_status,
    pull_model_streaming,
    repair_runtime,
    shutdown_if_last_session,
    start_ollama,
    status_text,
    wait_until_running,
)
from shamsu.runtime.session_registry import claim_ollama_ownership, register_session
from shamsu.safety.approval import ask_approval, ask_approval_menu
from shamsu.safety.autonomy import is_long_running_enabled, set_long_running_enabled
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.permission_store import PermissionMemory
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.patch.engine import PatchEngine
from shamsu.session.manager import SessionLogger, SessionManager
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import WebFetchResult, WebSearchResult, WebTool
from shamsu.tools.django import DjangoSetupResult, DjangoSetupRunner, DjangoTestRunner
from shamsu.tools.executor import CommandRunner
from shamsu.tools.git import GitTool
from shamsu.tools.workspace import MentionResolver, WorkspaceTool
from shamsu.types import (
    ApprovalRequest,
    ContextPack,
    ProjectSpec,
    RoutingDecision,
    SearchResult,
    TaskStep,
    TaskStepStatus,
)

if sys.platform == "win32":
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError
else:
    NoConsoleScreenBufferError = RuntimeError

DEFAULT_ASK_APPROVAL = ask_approval


class EmptySearchAgent:
    def search(self, query: str, top_k: int = 5, boost_paths: list[str] | None = None) -> list[SearchResult]:
        return []

    def symbol_lookup(self, name: str) -> list[SearchResult]:
        return []

    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]:
        return []


SYSTEM_COMMANDS = (
    "/help",
    "/index",
    "/status",
    "/doctor",
    "/search ",
    "/symbols ",
    "/parse-prd ",
    "/plan-prd ",
    "/generate-django ",
    "/generate-prd ",
    "/models status",
    "/models pull",
    "/models repair",
    "/web search ",
    "/web open ",
    "/web summarize ",
    "/browse open ",
    "/browse read",
    "/browse click ",
    "/browse type ",
    "/browse screenshot",
    "/django setup ",
    "/django test ",
    "/django fix-tests ",
    "/sessions list",
    "/sessions current",
    "/sessions show ",
    "/sessions resume ",
    "/sessions rename ",
    "/sessions close",
    "/sessions export ",
    "/permissions list",
    "/permissions clear",
    "/tasks list",
    "/tasks show ",
    "/autonomy status",
    "/autonomy on",
    "/autonomy off",
    "/log",
    "/log tail",
    "/edit ",
    "/fix ",
    "/test-gen ",
    "/audit ",
    "/docs ",
    "/exit",
)


class SlashCommandCompleter(Completer):
    def __init__(self, workspace: Path | None = None) -> None:
        self.workspace = workspace

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        token = text.rsplit(maxsplit=1)[-1] if text.strip() else text
        if token.startswith("@") and self.workspace is not None:
            prefix = token[1:]
            for suggestion in WorkspaceTool(self.workspace).mention_suggestions(prefix):
                yield Completion(suggestion, start_position=-len(token))
            return
        if not text.startswith("/"):
            return
        lowered = text.lower()
        for command in SYSTEM_COMMANDS:
            if command.startswith(lowered):
                yield Completion(command, start_position=-len(text))


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
                    "  build the product from this PRD",
                    "  change the CLI banner text",
                    "  fix this traceback: <paste error>",
                    "  write tests for the parser",
                    "  audit this project for security issues",
                    "  update the README",
                    "",
                    "Commands:",
                    "  /index                    Index the current workspace",
                    "  /status                   Show index counts",
                    "  /doctor                   Diagnose install/workspace health (read-only)",
                    "  /search <query>           Search indexed snippets",
                    "  /symbols <name>           Look up indexed symbols",
                    "  /parse-prd <file>         Parse a Markdown, TXT, or PDF PRD",
                    "  /plan-prd <file>          Preview and approve a project plan",
                    "  /generate-django <file>   Generate deterministic Django backend files",
                    "  /generate-prd <file> --output <dir>",
                    "  /django setup [dir]       Install generated deps and run migrations",
                    "  /django test [dir]        Run generated Django tests",
                    "  /django fix-tests [dir]   Run tests and apply bug-fix loop",
                    "  /models status            Show local Ollama/model status",
                    "  /models pull              Pull missing local models",
                    "  /models repair            Start Ollama and pull missing models",
                    "  /web search <query>       Search the web with approval",
                    "  /web open <url>           Fetch and summarize a web page",
                    "  /web summarize <url>      Alias for /web open",
                    "  /browse open <url>        Open a page in the local browser",
                    "  /browse read              Read the current browser page",
                    "  /browse click <selector>  Click the current page",
                    "  /browse type <selector> <text>",
                    "  /browse screenshot        Save a browser screenshot",
                    "  /sessions list            List workspace sessions",
                    "  /sessions current         Show current session",
                    "  /sessions show <id>       Show session metadata",
                    "  /sessions resume <id>     Resume another session",
                    "  /sessions rename <id> <title>",
                    "  /sessions close [id]      Close a session",
                    "  /sessions export <id>     Export redacted session bundle",
                    "  /permissions list         Show remembered 'always allow' decisions",
                    "  /permissions clear        Forget all remembered approval decisions",
                    "  /tasks list               List tracked multi-step tasks",
                    "  /tasks show <id>          Show a task's steps, phase, and blockers",
                    "  /autonomy status          Show whether long-running mode is on",
                    "  /autonomy on|off          Toggle long-running autonomous mode for this workspace",
                    "  /log tail                 Show recent session events",
                    "  /edit <request>           Force code-edit workflow",
                    "  /fix <bug/traceback>      Force bug-fix workflow",
                    "  /test-gen <request>       Force test-generation workflow",
                    "  /audit <request>          Force audit workflow",
                    "  /docs <request>           Force README documentation workflow",
                    "  /help                     Show commands",
                    "  /exit                     Quit",
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


# Re-exported so existing call sites/tests can keep using repl._ensure_index;
# the shared implementation lives in indexer.walker so AgentOrchestrator can
# use the exact same best-effort auto-indexing without importing this module.
_ensure_index = ensure_index


def _build_search_agent(
    workspace: Path,
    session_logger: SessionLogger | None = None,
) -> tuple[SearchAgent | EmptySearchAgent, bool]:
    _ensure_index(workspace, session_logger)
    if _has_index(workspace):
        return SearchAgent(_index_db_path(workspace)), True
    return EmptySearchAgent(), False


def _build_workspace_qa_workflow(
    workspace: Path,
    session_logger: SessionLogger | None = None,
) -> tuple[QAWorkflow, bool]:
    search, uses_real_index = _build_search_agent(workspace, session_logger)
    return QAWorkflow(search=search), uses_real_index


def _handle_index(workspace: Path, console: Console, session_logger: SessionLogger | None = None) -> None:
    entries = FileWalker(workspace, session_logger=session_logger).index()
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
    approved = _make_approval_manager(workspace, session_logger, console, approval_func).ask(request)
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
        approval_manager=_make_approval_manager(workspace, session_logger, console, approval_func),
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


async def _handle_generate_prd(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    try:
        prd_path, output_dir = _parse_generate_prd_args(user_input)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    search, _uses_real_index = _build_search_agent(workspace, session_logger)
    result = await FullDjangoPipeline(
        workspace,
        search=search,
        session_logger=session_logger,
        approval_func=lambda request: ask_approval(request, console=console),
        long_running=is_long_running_enabled(workspace),
    ).run(prd_path, target_dir=output_dir)
    _print_full_pipeline_result(result, console)


def _parse_generate_prd_args(user_input: str) -> tuple[str, str]:
    parts = shlex.split(user_input)
    if len(parts) < 2:
        raise ValueError("Usage: generate-prd <file> --output <dir>")
    prd_path = parts[1]
    output_dir = "."
    if "--output" in parts:
        index = parts.index("--output")
        if index + 1 >= len(parts):
            raise ValueError("Usage: generate-prd <file> --output <dir>")
        output_dir = parts[index + 1]
    return prd_path, output_dir


def _print_full_pipeline_result(result: FullPipelineResult, console: Console) -> None:
    table = Table(title="Full Project Pipeline")
    table.add_column("Step")
    table.add_column("Status")
    table.add_row("Project", result.project.project_name if result.project else "not built")
    if result.project:
        table.add_row("Category", result.project.category or result.project.archetype.value)
    table.add_row("Target", str(result.target_dir))
    table.add_row("Files", str(len(result.written_files or [])))
    table.add_row("Diagnostics", str(len(result.diagnostics or [])))
    if result.preview_url:
        table.add_row("Preview", result.preview_url)
    if result.setup_result:
        table.add_row("Setup", "ok" if result.setup_result.ok else "failed")
    if result.test_result:
        table.add_row("Tests", f"{result.test_result.passed} passed, {result.test_result.failed} failed")
    if result.dod_result:
        dod_status = "ok" if result.dod_result.ok else (
            "failed: " + ", ".join(item.item_id for item in result.dod_result.required_failures)
        )
        table.add_row("Definition of Done", dod_status)
    table.add_row("Result", "success" if result.success else "failed")
    console.print(table)
    if result.dod_result:
        dod_table = Table(title="Definition of Done")
        dod_table.add_column("Item")
        dod_table.add_column("Severity")
        dod_table.add_column("Status")
        dod_table.add_column("Detail")
        for item in result.dod_result.results:
            dod_table.add_row(
                item.item_id,
                item.severity,
                "pass" if item.passed else "fail",
                item.detail,
            )
        console.print(dod_table)
    if result.error:
        console.print(Panel(result.error, title="Pipeline Error", border_style="red"))


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


def _handle_status(workspace: Path, console: Console, session_logger: SessionLogger | None = None) -> None:
    _ensure_index(workspace, session_logger)
    db_path = _index_db_path(workspace)

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


def _handle_search(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    _, _, query = user_input.partition(" ")
    query = query.strip()
    if not query:
        console.print("[red]Usage: search <query>[/red]")
        return
    _ensure_index(workspace, session_logger)
    results = SearchAgent(_index_db_path(workspace)).search(query, top_k=5)
    if not results:
        console.print("[yellow]No results.[/yellow]")
        return
    for result in results:
        console.print(
            f"{result.file_path}:{result.line_start}-{result.line_end} "
            f"score={result.score:.4f}"
        )


def _handle_doctor(workspace: Path, console: Console) -> None:
    report = run_doctor(workspace=workspace)
    console.print(format_report(report))


def _handle_permissions(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=1)
    command = parts[1].strip().lower() if len(parts) > 1 else "list"
    memory = _get_permission_memory(workspace)
    if command == "clear":
        memory.forget_all()
        console.print("[green]Forgot all remembered approval decisions.[/green]")
        return
    if command == "list":
        remembered = memory.list_remembered()
        if not remembered:
            console.print("[dim]No remembered approval decisions for this workspace.[/dim]")
            return
        table = Table(title="Remembered Approvals")
        table.add_column("Action Type")
        table.add_column("Scope")
        for action_type, scope in sorted(remembered.items()):
            table.add_row(action_type, scope)
        console.print(table)
        return
    console.print("[red]Usage: permissions list|clear[/red]")


def _print_task(task: MilestoneTask, console: Console) -> None:
    console.print(f"Task: {task.task_id}")
    console.print(f"Request: {task.user_request}")
    console.print(f"Phase: {task.phase}")
    if task.next_action:
        console.print(f"[yellow]Next action: {task.next_action}[/yellow]")
    table = Table(title="Steps")
    table.add_column("ID")
    table.add_column("Phase")
    table.add_column("Description")
    table.add_column("Status")
    table.add_column("Error")
    for step in task.steps:
        table.add_row(str(step.id), step.phase, step.description, step.status.value, step.error or "")
    console.print(table)
    if task.files_created:
        console.print(f"Files created: {', '.join(task.files_created)}")
    if task.files_edited:
        console.print(f"Files edited: {', '.join(task.files_edited)}")


def _handle_autonomy(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=1)
    command = parts[1].strip().lower() if len(parts) > 1 else "status"
    if command == "on":
        set_long_running_enabled(workspace, True)
        console.print(
            "[green]Long-running mode enabled for this workspace.[/green] "
            "Agent chat, the Django fix-tests loop, and full PRD generation will use "
            "higher round/iteration ceilings with a repetition guard and stall detection "
            "instead of the default low caps."
        )
        return
    if command == "off":
        set_long_running_enabled(workspace, False)
        console.print("[yellow]Long-running mode disabled. Back to the default low caps.[/yellow]")
        return
    if command == "status":
        enabled = is_long_running_enabled(workspace)
        state = "[green]on[/green]" if enabled else "[dim]off (default)[/dim]"
        console.print(f"Long-running mode: {state}")
        return
    console.print("[red]Usage: autonomy status|on|off[/red]")


def _handle_tasks(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=2)
    command = parts[1].strip().lower() if len(parts) > 1 else "list"
    if command == "list":
        task_ids = list_task_ids(workspace)
        if not task_ids:
            console.print("[dim]No tracked tasks for this workspace.[/dim]")
            return
        table = Table(title="Tasks")
        table.add_column("ID")
        table.add_column("Phase")
        table.add_column("Pending")
        table.add_column("Blocked")
        table.add_column("Next Action")
        for task_id in task_ids:
            try:
                task = load_task(workspace, task_id)
            except (OSError, ValueError, KeyError) as exc:
                table.add_row(task_id, "-", "-", "-", f"Could not load: {exc}")
                continue
            pending = sum(1 for step in task.steps if step.status == TaskStepStatus.PENDING)
            table.add_row(
                task.task_id, task.phase, str(pending), str(len(task.blocked_steps)),
                task.next_action or "-",
            )
        console.print(table)
        return
    if command == "show":
        if len(parts) < 3:
            console.print("[red]Usage: tasks show <id>[/red]")
            return
        try:
            task = load_task(workspace, parts[2].strip())
        except (OSError, ValueError) as exc:
            console.print(f"[red]Could not load task {parts[2].strip()}: {exc}[/red]")
            return
        _print_task(task, console)
        return
    console.print("[red]Usage: tasks list|show <id>[/red]")


def _handle_symbols(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    _, _, name = user_input.partition(" ")
    name = name.strip()
    if not name:
        console.print("[red]Usage: symbols <name>[/red]")
        return
    _ensure_index(workspace, session_logger)
    results = SearchAgent(_index_db_path(workspace)).symbol_lookup(name)
    if not results:
        console.print("[yellow]No symbols found.[/yellow]")
        return
    for result in results:
        symbol = result.symbol_name or name
        console.print(f"{symbol}: {result.file_path}:{result.line_start}-{result.line_end}")


def _handle_models(
    user_input: str,
    console: Console,
) -> None:
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
            console.print("[yellow]Ollama is not running. Starting local Ollama...[/yellow]")
            serve_pid = start_ollama(Path(status.ollama_path))
            if serve_pid:
                claim_ollama_ownership(serve_pid)
            wait_until_running()
            status = collect_status(Path(status.ollama_path))
        if not status.missing_models:
            console.print("[green]All required local models are installed.[/green]")
            return
        # Typing `/models pull` is itself the consent - download directly rather
        # than gating on a second y/n prompt (which can auto-cancel on some
        # Windows terminals where built-in input() sees a non-interactive stdin).
        console.print(
            "[cyan]Downloading missing local model(s):[/cyan] " + ", ".join(status.missing_models)
        )
        results = _pull_models_with_progress(Path(status.ollama_path), status.missing_models, console)
        _print_model_pull_results(results, console)
        _print_runtime_status(console)
        return
    if command == "repair":
        status = repair_runtime(pull_models=False)
        if status.ollama_found and not status.server_running:
            console.print("[yellow]Starting local Ollama...[/yellow]")
            serve_pid = start_ollama(Path(status.ollama_path))
            if serve_pid:
                claim_ollama_ownership(serve_pid)
            wait_until_running()
            status = collect_status(Path(status.ollama_path))
        if status.ollama_found and status.server_running and status.missing_models:
            # Explicit `/models repair` is consent - download directly.
            console.print(
                "[cyan]Downloading missing local model(s):[/cyan] " + ", ".join(status.missing_models)
            )
            results = _pull_models_with_progress(Path(status.ollama_path), status.missing_models, console)
            _print_model_pull_results(results, console)
            status = collect_status(Path(status.ollama_path))
        _print_runtime_status(console, status=status)
        return
    console.print("[red]Usage: models status|pull|repair[/red]")


def _handle_web(
    user_input: str,
    console: Console,
    web_tool: WebTool,
    llm: LLMManager,
) -> None:
    parts = user_input.split(maxsplit=2)
    command = parts[1].strip().lower() if len(parts) > 1 else ""
    argument = parts[2].strip() if len(parts) > 2 else ""
    if command == "search":
        if not argument:
            console.print("[red]Usage: web search <query>[/red]")
            return
        result = web_tool.search(argument, reason="User explicitly requested a web search.")
        asyncio.run(_print_web_answer(argument, result, [], console, llm))
        return
    if command in {"open", "summarize"}:
        if not argument:
            console.print("[red]Usage: web open <url>[/red]")
            return
        fetch = web_tool.fetch(argument, reason="User explicitly requested a web page fetch.")
        _print_web_fetch(fetch, console)
        return
    console.print("[red]Usage: web search <query>|open <url>|summarize <url>[/red]")


def _handle_browse(
    user_input: str,
    console: Console,
    browser_tool: BrowserTool,
) -> None:
    parts = user_input.split(maxsplit=3)
    command = parts[1].strip().lower() if len(parts) > 1 else ""
    if command == "open":
        if len(parts) < 3:
            console.print("[red]Usage: browse open <url>[/red]")
            return
        _print_browser_result(
            browser_tool.open(parts[2].strip(), reason="User explicitly requested browser access."),
            console,
        )
        return
    if command == "read":
        _print_browser_result(browser_tool.read(), console)
        return
    if command == "click":
        if len(parts) < 3:
            console.print("[red]Usage: browse click <selector>[/red]")
            return
        _print_browser_result(browser_tool.click(parts[2].strip()), console)
        return
    if command == "type":
        if len(parts) < 4:
            console.print("[red]Usage: browse type <selector> <text>[/red]")
            return
        _print_browser_result(browser_tool.type_text(parts[2].strip(), parts[3]), console)
        return
    if command == "screenshot":
        _print_browser_result(browser_tool.screenshot(), console)
        return
    console.print("[red]Usage: browse open|read|click|type|screenshot[/red]")


def _build_pull_progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    )


class _LazyModelPullProgress:
    """Drives a Rich progress bar for models pulled on-demand mid-workflow.

    Unlike `_pull_models_with_progress` (a known batch pulled up front by
    `/models pull`), models here become needed one at a time from inside
    async workflows, so the bar is started lazily and torn down once no
    pull is in flight.
    """

    def __init__(self, console: Console) -> None:
        self._console = console
        self._progress: Progress | None = None
        self._task_ids: dict[str, int] = {}

    def _ensure_progress(self) -> Progress:
        if self._progress is None:
            self._progress = _build_pull_progress(self._console)
            self._progress.start()
        return self._progress

    def on_start(self, model_name: str) -> None:
        progress = self._ensure_progress()
        self._task_ids[model_name] = progress.add_task(
            f"Pulling {model_name} (first use of this model)", total=None
        )

    def on_chunk(self, model_name: str, _chunk: str) -> None:
        task_id = self._task_ids.get(model_name)
        if task_id is not None and self._progress is not None:
            self._progress.advance(task_id)

    def on_finish(self, model_name: str, success: bool) -> None:
        task_id = self._task_ids.pop(model_name, None)
        if task_id is not None and self._progress is not None:
            self._progress.update(
                task_id,
                description=f"{'Installed' if success else 'Failed'} {model_name}",
            )
        if self._progress is not None and not self._task_ids:
            self._progress.stop()
            self._progress = None

    def as_model_pull_progress(self) -> ModelPullProgress:
        return ModelPullProgress(on_start=self.on_start, on_chunk=self.on_chunk, on_finish=self.on_finish)


def _make_llm_manager(session_logger: SessionLogger | None, console: Console) -> LLMManager:
    lazy_progress = _LazyModelPullProgress(console)
    return LLMManager(
        session_logger=session_logger,
        model_pull_progress=lazy_progress.as_model_pull_progress(),
    )


# One PermissionMemory per workspace per process, so "always allow" choices
# made in one workflow (e.g. /edit) are honored by later ones (e.g. /fix)
# within the same REPL session, not just within a single handler call.
_PERMISSION_MEMORY_CACHE: dict[Path, PermissionMemory] = {}


def _get_permission_memory(workspace: Path) -> PermissionMemory:
    resolved = workspace.resolve()
    memory = _PERMISSION_MEMORY_CACHE.get(resolved)
    if memory is None:
        memory = PermissionMemory(resolved)
        _PERMISSION_MEMORY_CACHE[resolved] = memory
    return memory


def _make_approval_manager(
    workspace: Path,
    session_logger: SessionLogger | None,
    console: Console,
    approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
) -> ApprovalManager:
    # Only use the interactive single-menu (yes / yes+remember / no) when this
    # is the real interactive prompt. Callers (mainly tests) that inject their
    # own approval_func get memory-gated auto-approval but keep their own
    # approval behavior, so a substituted approval_func isn't silently
    # overridden here.
    menu_prompt = (
        (lambda request, offer: ask_approval_menu(request, offer_remember=offer, console=console))
        if approval_func is DEFAULT_ASK_APPROVAL
        else None
    )
    return ApprovalManager(
        approval_func=approval_func,
        session_logger=session_logger,
        memory=_get_permission_memory(workspace),
        menu_prompt=menu_prompt,
    )


def _pull_models_with_progress(
    ollama_path: Path,
    models: list[str],
    console: Console,
) -> dict[str, int]:
    results: dict[str, int] = {}
    with _build_pull_progress(console) as progress:
        for model in models:
            task_id = progress.add_task(f"Pulling {model} (resumes if interrupted)", total=None)
            exit_code = pull_model_streaming(
                ollama_path,
                model,
                progress_callback=lambda _chunk: progress.advance(task_id),
            )
            results[model] = exit_code
            progress.update(task_id, description=f"{'Installed' if exit_code == 0 else 'Failed'} {model}")
    return results


def _print_model_pull_results(results: dict[str, int], console: Console) -> None:
    for model, exit_code in results.items():
        if exit_code == 0:
            console.print(f"[green]{model}: installed[/green]")
        else:
            console.print(f"[red]{model}: failed with exit {exit_code}. Re-run `models pull` to resume.[/red]")


def _handle_django(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    parts = user_input.split(maxsplit=2)
    command = parts[1].strip().lower() if len(parts) > 1 else ""
    project_dir = parts[2].strip() if len(parts) > 2 else "."
    if command == "setup":
        result = DjangoSetupRunner(workspace, session_logger=session_logger).run(project_dir)
        _print_django_setup_result(result, console)
        return
    if command == "test":
        result = DjangoTestRunner(workspace, session_logger=session_logger).run(project_dir)
        _print_django_test_result(result, console)
        return
    console.print("[red]Usage: django setup|test|fix-tests [project-dir][/red]")


def _print_django_setup_result(result: DjangoSetupResult, console: Console) -> None:
    table = Table(title="Django Setup")
    table.add_column("Step")
    table.add_column("Command")
    table.add_column("Exit")
    for command in result.commands:
        style = "green" if command.ok else "red"
        table.add_row(command.step, command.command, f"[{style}]{command.exit_code}[/{style}]")
    if not result.commands and result.failures:
        failure = result.failures[0]
        table.add_row(failure.step, failure.command or "validate project", "[red]1[/red]")
    console.print(table)
    if result.ok:
        console.print("[green]Django dependencies installed and migrations completed.[/green]")
        return
    console.print(Panel(result.bugfix_context, title="Setup Failure", border_style="red"))


def _print_django_test_result(result, console: Console) -> None:
    table = Table(title="Django Tests")
    table.add_column("Passed")
    table.add_column("Failed")
    style = "green" if result.failed == 0 else "red"
    table.add_row(str(result.passed), str(result.failed), style=style)
    console.print(table)
    if result.failures:
        failures = "\n".join(
            f"- {failure.test_name} {failure.file}:{failure.line or ''} {failure.error_message}"
            for failure in result.failures
        )
        console.print(Panel(failures, title="Failures", border_style="red"))


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


def _log_assistant_message(
    session_logger: SessionLogger | None,
    message: str,
    workflow_id: str | None = None,
) -> None:
    if session_logger and message:
        session_logger.log(
            "assistant.message",
            {"message": message},
            "Assistant responded",
            workflow_id=workflow_id,
        )


def _append_agent_context(user_input: str, agent_context: str) -> str:
    if not agent_context:
        return user_input
    return f"{user_input}\n\nAdditional SHAMSU context:\n{agent_context}"


async def _handle_request(
    user_input: str,
    workspace: Path,
    console: Console,
    web_tool: WebTool,
    browser_tool: BrowserTool,
    previous_user_prompt: str = "",
    session_logger: SessionLogger | None = None,
    thinking_status: Any = None,
) -> None:
    agent_result = AgentOrchestrator(workspace, session_logger=session_logger).run(user_input)
    effective_input = agent_result.effective_input or user_input
    if session_logger is None:
        effective_input = _expand_followup_prompt(effective_input, previous_user_prompt)
    agent_context = agent_result.context
    if agent_result.handled:
        console.print(Panel(agent_result.message, title=agent_result.title or "SHAMSU"))
        _log_assistant_message(session_logger, agent_result.message, workflow_id=agent_result.action or "agent")
        return
    if _looks_like_workspace_location_prompt(effective_input):
        _print_workspace_location(workspace, console)
        return
    if _looks_like_workspace_files_prompt(effective_input):
        _print_workspace_files(workspace, console)
        return
    if _looks_like_prd_build_request(effective_input, workspace):
        await _handle_prd_build_request(effective_input, workspace, console, session_logger=session_logger)
        return
    if _looks_like_file_write_request(effective_input):
        await _run_agent_chat(
            _append_agent_context(effective_input, agent_context),
            workspace,
            console,
            session_logger=session_logger,
            auto_approve=is_long_running_enabled(workspace),
        )
        return
    if _looks_like_workspace_prd_request(effective_input):
        _handle_workspace_prd_request(workspace, console)
        return
    if _looks_like_affirmative_continue(effective_input) and _multiplayer_template_present(workspace):
        await _run_agent_chat(
            _build_continue_game_request(),
            workspace,
            console,
            session_logger=session_logger,
            force_long_running=True,
            auto_approve=True,
        )
        return
    if _looks_like_run_game_request(effective_input):
        await _handle_run_game(workspace, console, session_logger=session_logger)
        return
    if _looks_like_browser_needed_prompt(effective_input):
        await _run_browser_assist(effective_input, console, llm=_make_llm_manager(session_logger, console), browser_tool=browser_tool)
        return
    if _looks_like_web_needed_prompt(effective_input):
        await _run_web_assist(
            effective_input,
            console,
            llm=_make_llm_manager(session_logger, console),
            web_tool=web_tool,
            session_logger=session_logger,
        )
        return
    if _looks_like_react_prompt(effective_input):
        await _run_agent_chat(effective_input, workspace, console, session_logger=session_logger)
        return
    if _looks_like_django_generation_request(effective_input):
        generate_command = f"generate-django {_extract_prd_path_from_prompt(effective_input)}"
        _handle_generate_django(generate_command, workspace, console, session_logger=session_logger)
        return
    if _looks_like_prd_plan_request(effective_input):
        plan_command = f"plan-prd {_extract_prd_path_from_prompt(effective_input)}"
        _handle_plan_prd(plan_command, workspace, console, session_logger=session_logger)
        return
    search, uses_real_index = _build_search_agent(workspace, session_logger)
    llm = _make_llm_manager(session_logger, console)
    if not uses_real_index:
        decision = _keyword_decision(effective_input)
        if decision.intent in {"qa", "explain"}:
            if _is_general_chat_prompt(effective_input):
                chat_input = effective_input
                if not _is_casual_prompt(effective_input):
                    chat_input = _append_agent_context(effective_input, agent_context)
                await _run_agent_chat(
                    chat_input,
                    workspace,
                    console,
                    session_logger=session_logger,
                )
            else:
                console.print(
                    "[yellow]No index found. Run `index` first for project-specific QA.[/yellow]"
                )
                console.print(
                    "I can still do general local chat without an index, but for this workspace-specific question "
                    "I need you to run `index` first."
                )
            return
        console.print(
            "[yellow]No index found. Run `index` first for project-specific QA.[/yellow]"
        )
    decision = await _route_prompt(effective_input, llm)
    _print_decision(decision, console)

    try:
        _log_event(
            session_logger,
            "workflow.started",
            {"intent": decision.intent, "prompt": user_input, "effective_prompt": effective_input},
            f"Workflow started: {decision.intent}",
            workflow_id=decision.intent,
        )
        if decision.intent in {"qa", "explain"}:
            # An imperative ("fix the code", "do the thing", "continue", ...) is
            # an action, not a question. Route it to the agent loop, which
            # actually has file and command tools, instead of the tool-less QA
            # brain that would only describe the fix (or claim it can't access
            # files). With `/autonomy on` the edits run hands-free; otherwise
            # each write asks for approval.
            if (
                _looks_like_action_request(effective_input)
                or _looks_like_trouble_report(effective_input)
                or (_is_general_chat_prompt(effective_input) and not uses_real_index)
            ):
                await _run_agent_chat(
                    _append_agent_context(effective_input, agent_context),
                    workspace,
                    console,
                    session_logger=session_logger,
                    auto_approve=is_long_running_enabled(workspace),
                )
            else:
                await _run_qa(effective_input, workspace, console, llm, extra_context=agent_context, session_logger=session_logger, thinking_status=thinking_status)
        elif decision.intent == "code_edit":
            await _run_code_edit(_append_agent_context(effective_input, agent_context), workspace, search, console, llm, session_logger)
        elif decision.intent == "bug_fix":
            await _run_bug_fix(_append_agent_context(effective_input, agent_context), workspace, search, console, llm, session_logger)
        elif decision.intent == "audit":
            await _run_audit(_append_agent_context(effective_input, agent_context), search, console, llm)
        elif decision.intent == "test_gen":
            await _run_test_generation(_append_agent_context(effective_input, agent_context), workspace, search, console, llm, session_logger)
        elif decision.intent == "doc_gen":
            await _run_docs(_append_agent_context(effective_input, agent_context), workspace, search, console, llm, session_logger)
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
            console.print(
                Panel(
                    f"{message}\n\nSHAMSU can try to repair the local runtime now.",
                    title="Workflow Unavailable",
                    border_style="red",
                )
            )
            _handle_models("models repair", console)
            return
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
    elif _looks_like_code_edit_request(user_input):
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


def _looks_like_workspace_prd_request(user_input: str) -> bool:
    text = user_input.lower()
    return (
        "prd" in text
        and not bool(_extract_prd_path_from_prompt(user_input))
        and any(
            phrase in text
            for phrase in (
                "check that out",
                "check it out",
                "check the prd",
                "look at the prd",
                "look at that prd",
                "review the prd",
                "read the prd",
                "added a prd",
                "add a prd",
                "working folder",
                "workspace",
            )
        )
    )


def _looks_like_workspace_location_prompt(user_input: str) -> bool:
    text = user_input.lower()
    return any(
        phrase in text
        for phrase in (
            "what folder are you in",
            "what folder you're in",
            "where are you right now",
            "where are you rn",
            "what directory are you in",
            "what workspace are you in",
            "where am i",
            "current folder",
            "current directory",
            "current workspace",
        )
    )


def _looks_like_workspace_files_prompt(user_input: str) -> bool:
    text = user_input.lower()
    return any(
        phrase in text
        for phrase in (
            "what files do i have here",
            "what files are here",
            "what's in this folder",
            "whats in this folder",
            "what's in this directory",
            "whats in this directory",
            "list files",
            "show files",
            "show me the files",
            "what files do i have",
            "what files are in this workspace",
        )
    )


def _looks_like_django_generation_request(user_input: str) -> bool:
    text = user_input.lower()
    return (
        any(phrase in text for phrase in ("generate django", "generate project", "build django"))
        and bool(_extract_prd_path_from_prompt(user_input))
    )


def _looks_like_web_needed_prompt(user_input: str) -> bool:
    text = user_input.lower()
    if _looks_like_browser_needed_prompt(user_input):
        return False
    extracted_url = _extract_url_from_prompt(user_input)
    if extracted_url and not _is_local_url(extracted_url):
        return True
    if any(phrase in text for phrase in ("search the web", "look up", "find docs", "documentation for", "official docs", "latest ", "current ", "check on the web")):
        return True
    if any(word in text for word in ("weather", "forecast", "temperature", "rain today", "news today", "stock price", "exchange rate")):
        return True
    if _has_fuzzy_web_keyword(text):
        return True
    if any(word in text for word in ("package", "api docs", "release notes", "version", "breaking change")) and not _is_project_local_prompt(text):
        return True
    return False


_WEB_FUZZY_KEYWORDS = ("weather", "forecast", "temperature")


def _has_fuzzy_web_keyword(text: str) -> bool:
    """Catches common typos (e.g. "weither" for "weather") that exact
    substring matching above would otherwise silently miss, sending the
    prompt into a tool-less chat path that hallucinates a fake search."""
    words = re.findall(r"[a-z]+", text)
    return any(
        difflib.get_close_matches(word, _WEB_FUZZY_KEYWORDS, n=1, cutoff=0.8)
        for word in words
    )


def _looks_like_browser_needed_prompt(user_input: str) -> bool:
    text = user_input.lower()
    return any(
        phrase in text
        for phrase in (
            "check the app",
            "open the app",
            "open the site",
            "show me the project",
            "show the project",
            "debug this page",
            "verify the dashboard",
            "inspect the rendered ui",
            "see if login works",
            "open localhost",
            "preview the app",
        )
    ) or _is_local_url(_extract_url_from_prompt(user_input))


def _extract_prd_path_from_prompt(user_input: str) -> str:
    quoted = re.search(r"['\"]([^'\"]+\.(?:md|markdown|txt|pdf))['\"]", user_input, re.I)
    if quoted:
        return quoted.group(1)
    match = re.search(r"([^\s]+?\.(?:md|markdown|txt|pdf))", user_input, re.I)
    return match.group(1) if match else ""


def _extract_url_from_prompt(user_input: str) -> str:
    match = re.search(r"(https?://[^\s]+)", user_input, re.I)
    return match.group(1) if match else ""


def _is_local_url(url: str) -> bool:
    if not url:
        return False
    hostname = urlparse(url).hostname or ""
    return hostname in {"localhost", "127.0.0.1", "::1"}


def _expand_followup_prompt(user_input: str, previous_user_prompt: str) -> str:
    if not previous_user_prompt:
        return user_input
    text = user_input.strip().lower()
    if text in {
        "check on the web",
        "look it up",
        "search the web",
        "use the web",
        "check online",
        "search online",
    }:
        return f"{previous_user_prompt.strip()} Please check on the web for this."
    if text in {
        "open it",
        "check it in the browser",
        "open it in the browser",
        "use the browser",
    }:
        return f"{previous_user_prompt.strip()} Please inspect it in the browser."
    return user_input


def _print_decision(decision: RoutingDecision, console: Console) -> None:
    console.print(
        f"[dim]intent={decision.intent} confidence={decision.confidence:.2f}[/dim]"
    )


def _print_workspace_location(workspace: Path, console: Console) -> None:
    console.print(
        Panel(
            f"I am working in:\n{workspace}",
            title="Current Workspace",
        )
    )


def _print_workspace_files(workspace: Path, console: Console, limit: int = 20) -> None:
    entries = sorted(
        [
            path for path in workspace.iterdir()
            if path.name != ".shamsu"
        ],
        key=lambda path: (not path.is_dir(), path.name.lower()),
    )
    if not entries:
        console.print(Panel(f"{workspace}\n\nThis workspace is empty.", title="Workspace Files"))
        return
    shown = entries[:limit]
    body = "\n".join(
        f"[dir]  {item.name}" if item.is_dir() else f"[file] {item.name}"
        for item in shown
    )
    if len(entries) > limit:
        body = f"{body}\n... {len(entries) - limit} more"
    console.print(
        Panel(
            f"Workspace: {workspace}\n\n{body}",
            title="Workspace Files",
        )
    )


def _looks_like_code_edit_request(user_input: str) -> bool:
    text = user_input.lower().strip()
    if any(word in text for word in ("change", "edit", "update")):
        return True
    if text.startswith("remove "):
        return True
    if text.startswith("add "):
        code_targets = (
            "function",
            "class",
            "test",
            "endpoint",
            "route",
            "model",
            "view",
            "serializer",
            "field",
            "button",
            "banner",
            "readme",
            "docstring",
            "logging",
            "validation",
        )
        return any(target in text for target in code_targets)
    return False


def _find_workspace_prd_files(workspace: Path) -> list[Path]:
    candidates: list[Path] = []
    for path in workspace.rglob("*"):
        if not path.is_file():
            continue
        if not is_prd_filename(path.name):
            continue
        try:
            candidates.append(path.relative_to(workspace))
        except ValueError:
            continue
    return sorted(candidates)


def _handle_workspace_prd_request(workspace: Path, console: Console) -> None:
    candidates = _find_workspace_prd_files(workspace)
    if not candidates:
        console.print(
            "[yellow]I couldn't find a PRD file in this workspace yet.[/yellow] "
            "Add a `.md`, `.txt`, or `.pdf` PRD (e.g. named `*prd*` or `Product Requirements*`), "
            "then ask again or run `/parse-prd <file>`."
        )
        return
    if len(candidates) > 1:
        console.print("[yellow]I found multiple PRD files in this workspace:[/yellow]")
        for path in candidates[:10]:
            console.print(f"- {path}")
        console.print("Tell me which one to open, or run `/parse-prd <file>` or `/plan-prd <file>`.")
        return
    relative_path = candidates[0]
    absolute_path = workspace / relative_path
    try:
        parsed = parse_prd_file(absolute_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    section_names = ", ".join(parsed.sections.keys()) or "none"
    console.print(
        Panel(
            f"File: {relative_path}\n"
            f"Title: {parsed.title}\n"
            f"Sections: {section_names}\n\n"
            f"Use `/plan-prd \"{relative_path}\"` if you want me to turn it into a project plan.",
            title="PRD Found",
        )
    )


_PRD_BUILD_VERBS = ("build", "finish", "implement", "generate", "make", "create", "develop")
_PRD_BUILD_NOUNS = ("product", "app", "application", "game", "project", "website", "site", "it", "this", "prd")

# Terse imperative "just do it" style commands. These carry no task detail of
# their own, so they must be routed to something that can actually act (the
# PRD build when a PRD is present, otherwise the tool-having agent loop) - never
# the tool-less QA brain, which would hallucinate "I cannot access files".
_VAGUE_ACTION_WORDS = {"go", "start", "build", "continue", "proceed", "run", "do"}
_VAGUE_ACTION_PHRASES = (
    "do the task",
    "do this task",
    "do that task",
    "do the tasks",
    "do that",
    "do it",
    "do this",
    "do them",
    "get it done",
    "get this done",
    "keep going",
    "go ahead",
    "carry on",
    "finish it",
    "finish the task",
    "finish this",
    "complete it",
    "complete the task",
    "start building",
    "start the build",
    "make it happen",
    "just do it",
    "make it",
    "build it",
    "run it",
)


def _looks_like_vague_action_request(user_input: str) -> bool:
    text = re.sub(r"[^\w\s]", "", user_input.lower()).strip()
    words = text.split()
    if not words or len(words) > 6:
        return False
    if text in _VAGUE_ACTION_WORDS:
        return True
    return any(phrase in text for phrase in _VAGUE_ACTION_PHRASES)


# Verbs that mean "modify the project" - their presence (outside obvious
# question phrasing) marks a request as an action to perform, not a question.
_ACTION_VERBS = {
    "fix", "build", "make", "create", "add", "implement", "update", "change",
    "edit", "modify", "write", "refactor", "rewrite", "improve", "apply",
    "generate", "install", "setup", "configure", "continue", "finish",
    "complete", "proceed", "resume", "redo", "regenerate", "review", "debug",
    "resolve", "correct", "patch",
}

# Leading phrasing that marks a genuine question/explanation: keep it on QA.
_QUESTION_PREFIXES = (
    "what", "why", "how", "when", "where", "who", "which", "whose",
    "explain", "tell me", "show me", "describe", "list ", "summarize",
    "is ", "are ", "was ", "were ", "does ", "do you", "did you", "should i",
    "can you explain", "could you explain", "whats", "hows",
)


def _looks_like_action_request(user_input: str) -> bool:
    """True when the prompt asks SHAMSU to *do work* (fix/edit/build) rather
    than answer a question. Broader than the narrow PRD-build trigger: any clear
    imperative or action verb (outside obvious question phrasing) should reach
    the tool-having agent loop, never the tool-less QA specialist that can only
    describe changes. This is deliberately verb-based, not a fixed phrase list,
    so new phrasings ("do the thing", "fix the code and check the reqs") are
    caught without another edit here."""
    raw = user_input.strip().lower()
    if not raw:
        return False
    if _looks_like_vague_action_request(user_input):
        return True
    # A trailing '?' or question-style opening means it is a question, leave on QA.
    if raw.endswith("?"):
        return False
    if any(raw.startswith(prefix) for prefix in _QUESTION_PREFIXES):
        return False
    words = set(re.sub(r"[^\w\s]", "", raw).split())
    if _ACTION_VERBS & words:
        return True
    # Generic "do the thing / work / rest / task" imperatives.
    if "do" in words and words & {"thing", "things", "work", "stuff", "rest", "task", "tasks", "job"}:
        return True
    return False


# "It's broken" reports and pasted error/stack-trace logs. These are implicit
# fix requests: the user is showing a problem, not asking a question, so they
# must reach the tool-having agent loop, which can read the files and repair,
# not the tool-less QA brain that returns a troubleshooting checklist.
_TROUBLE_SIGNALS = (
    "not working", "cant see", "can't see", "cannot see", "doesnt work", "doesn't work",
    "does not work", "not showing", "nothing happens", "nothing shows", "still broken",
    "still not", "blank page", "blank screen", "white screen", "wont run", "won't run",
    "crashes", "not rendering", "isnt working", "isn't working", "no game", "page is blank",
)
_ERROR_LOG_SIGNALS = (
    "error:", "cannot find", "is not exported", "has no exported member", "failed to compile",
    "uncaught", "syntaxerror", "referenceerror", "typeerror", "module not found",
    "cannot find module", "unexpected token", "traceback (most recent", "does not exist on type",
    "ts(", "vite:", "[plugin:",
)


def _looks_like_trouble_report(user_input: str) -> bool:
    low = user_input.lower()
    return any(s in low for s in _TROUBLE_SIGNALS) or any(s in low for s in _ERROR_LOG_SIGNALS)


_FILE_WRITE_VERBS = {
    "create", "write", "save", "generate", "make", "add", "edit", "update",
    "modify", "overwrite",
}

_FILE_HINT_WORDS = {
    "file", "files", "script", "component", "module", "readme", "gitignore",
    "config", "page", "class", "test", "tests",
}

_FILELIKE_RE = re.compile(
    r"(?:^|\s|['\"`@])(?:[A-Za-z0-9_. -]+[/\\])*[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12}(?:\s|$|['\"`,.;:])"
)


def _looks_like_file_write_request(user_input: str) -> bool:
    """Route explicit file-creation/edit prompts to the ReAct tool loop.

    This avoids handing "create hello.py" to the router/specialist stack, where
    a small model may produce prose or an invalid diff instead of using the
    safe write_file tool. Questions still stay on QA.
    """
    raw = user_input.strip().lower()
    if not raw or raw.endswith("?"):
        return False
    if any(raw.startswith(prefix) for prefix in _QUESTION_PREFIXES):
        return False
    words = set(re.sub(r"[^\w\s]", " ", raw).split())
    if not (_FILE_WRITE_VERBS & words):
        return False
    if _FILELIKE_RE.search(user_input):
        return True
    return bool(words & _FILE_HINT_WORDS)


def _looks_like_run_game_request(user_input: str) -> bool:
    text = user_input.lower()
    has_run = any(word in text for word in ("run", "start", "launch", "serve", "open"))
    has_game = any(word in text for word in ("game", "app", "site", "preview", "link", "access"))
    return has_run and has_game


def _looks_like_affirmative_continue(user_input: str) -> bool:
    text = re.sub(r"[^\w\s]", " ", user_input.lower()).strip()
    if not text:
        return False
    return text in {
        "yes", "yes please", "yeah", "yep", "ok", "okay", "sure",
        "continue", "go ahead", "do it", "proceed",
    }


async def _handle_run_game(
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    package_json = workspace / "package.json"
    if not package_json.exists():
        console.print(
            Panel(
                "I could not find `package.json` in this workspace, so there is no game dev server to start yet.\n"
                "Build/scaffold the game first, then run this again.",
                title="Game Server",
                border_style="yellow",
            )
        )
        return

    log_dir = workspace / ".shamsu" / "dev-server"
    log_dir.mkdir(parents=True, exist_ok=True)
    if not _ensure_node_modules(workspace, console):
        return

    # Typecheck-and-fix before previewing: a dropped export or bad import would
    # otherwise start the server but leave a blank/crashing page in the browser.
    await _verify_and_repair_frontend(workspace, Path("."), console, session_logger=session_logger)

    relay = _start_background_command(
        "npm run dev:relay",
        workspace,
        log_dir / "relay.log",
        visible_console=True,
    )
    vite = _start_background_command(
        "npm run dev",
        workspace,
        log_dir / "vite.log",
        visible_console=True,
    )
    url = "http://localhost:5173"
    console.print(
        Panel(
            f"Game dev server started in separate terminal windows you can watch.\n\n"
            f"Open: {url}\n\n"
            f"Vite PID: {vite.pid}\n"
            f"Relay PID: {relay.pid}\n"
            f"Logs: {log_dir}",
            title="Game Running",
            border_style="green",
        )
    )
    _log_event(
        session_logger,
        "project.preview.started",
        {"url": url, "vite_pid": vite.pid, "relay_pid": relay.pid, "logs": str(log_dir)},
        f"Started game preview at {url}",
        workflow_id="game-preview",
    )

    # Read the Vite log back and, if it failed to boot cleanly, fix the errors.
    console.print("[dim]Watching the dev server start up for errors...[/dim]")
    errors = await _await_dev_server_settle(log_dir / "vite.log")
    if errors:
        console.print(Panel(errors, title="Vite reported errors on startup", border_style="yellow"))
        console.print("[cyan]Fixing the dev-server errors...[/cyan]")
        await _run_agent_chat(
            _build_frontend_repair_request(errors, Path(".")),
            workspace,
            console,
            session_logger=session_logger,
            force_long_running=True,
            auto_approve=True,
        )
        console.print(
            "[green]Applied fixes. Vite hot-reloads automatically - refresh the browser "
            f"at {url} (the dev window shows live logs).[/green]"
        )
    else:
        console.print(f"[green]Dev server looks healthy. Open {url} in your browser.[/green]")


def _start_background_command(
    command: str,
    cwd: Path,
    log_path: Path,
    visible_console: bool = False,
) -> subprocess.Popen:
    if visible_console and sys.platform == "win32":
        # Open a NEW visible console the user can watch AND tee output to the
        # log file so SHAMSU can read errors too. PowerShell Tee-Object writes
        # to both the window and the file; -NoExit keeps the window open if the
        # process stops so the final error stays on screen.
        log_path.write_text("", encoding="utf-8")
        # `cmd /c '<cmd> 2>&1'` merges stderr at the cmd level so PowerShell does
        # not wrap native stderr in NativeCommandError noise; Tee-Object then
        # shows output in the window and writes it (UTF-16) to the log.
        ps_command = f"cmd /c '{command} 2>&1' | Tee-Object -FilePath '{log_path}'"
        return subprocess.Popen(
            ["powershell", "-NoProfile", "-NoExit", "-Command", ps_command],
            cwd=cwd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    log = log_path.open("a", encoding="utf-8")
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    return subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )


_DEV_ERROR_SIGNALS = (
    "failed to resolve import", "internal server error", "pre-transform error",
    "could not resolve", "transform failed", "[plugin:", "has no exported member",
    "is not exported", "cannot find module", "cannot find name", "unexpected token",
    "syntaxerror", "referenceerror", "error ts", "tsc: error", "npm err!",
    "module not found", "failed to compile",
)
_DEV_READY_SIGNALS = ("ready in", "localhost:5173", "vite v", "local:   http")


def _dev_log_indicates_ready(text: str) -> bool:
    low = text.lower()
    return any(signal in low for signal in _DEV_READY_SIGNALS)


def _scan_dev_log_for_errors(text: str) -> str | None:
    """Return the tail of a dev-server log if it shows a real error, else None."""
    low = text.lower()
    if not any(signal in low for signal in _DEV_ERROR_SIGNALS):
        return None
    return text.strip()[-3000:]


def _read_text_safe(path: Path) -> str:
    """Read a log file, tolerating the UTF-16 that Windows PowerShell's
    Tee-Object writes (detected by BOM) as well as plain UTF-8."""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    return data.decode("utf-8", errors="replace")


async def _await_dev_server_settle(log_path: Path, timeout: float = 24.0) -> str | None:
    """Poll a dev-server log until it looks ready or reports an error.

    Returns the error text if the server failed to start cleanly, else None.
    """
    interval = 1.5
    for _ in range(max(1, int(timeout / interval))):
        await asyncio.sleep(interval)
        text = _read_text_safe(log_path)
        errors = _scan_dev_log_for_errors(text)
        if errors:
            return errors
        if _dev_log_indicates_ready(text):
            return None
    return None


def _looks_like_prd_build_request(user_input: str, workspace: Path) -> bool:
    """Detect a natural-language "build the product from this PRD" request.

    Conservative: requires BOTH a build verb + product noun AND a real PRD
    signal (the word prd/product requirements, an @-mentioned doc, or exactly
    one PRD file present). This keeps narrow prompts like "build the navbar"
    or "fix the build" from triggering a full autonomous product build.
    A terse "do the task"/"continue" also counts when exactly one PRD is present
    - in a PRD workspace that almost always means "build that PRD", and the
    build is approval-gated anyway so it is safe to route here.
    """
    if _looks_like_vague_action_request(user_input) and _resolve_build_prd(user_input, workspace) is not None:
        return True
    text = user_input.lower()
    words = re.sub(r"[^\w\s]", " ", text).split()
    has_build_verb = any(verb in text for verb in _PRD_BUILD_VERBS) or _has_fuzzy_word(
        words, _PRD_BUILD_VERBS
    )
    has_product_noun = any(noun in text for noun in _PRD_BUILD_NOUNS)
    if not (has_build_verb and has_product_noun):
        return False
    if "prd" in text or "product requirements" in text or "requirements document" in text:
        return True
    if _resolve_build_prd(user_input, workspace) is not None:
        return True
    return False


def _has_fuzzy_word(words: list[str], targets: tuple[str, ...], cutoff: float = 0.78) -> bool:
    for word in words:
        if difflib.get_close_matches(word, targets, n=1, cutoff=cutoff):
            return True
    return False


def _resolve_build_prd(user_input: str, workspace: Path) -> Path | None:
    """Resolve which PRD a build request refers to (relative to workspace).

    Order: explicit path in the prompt -> a resolved @-mention that is a PRD
    -> the single workspace PRD if exactly one exists. Returns None when
    ambiguous (multiple) or nothing is found.
    """
    explicit = _extract_prd_path_from_prompt(user_input)
    if explicit:
        try:
            resolved = _resolve_workspace_file(explicit, workspace)
        except SecurityError:
            return None
        if resolved.exists() and resolved.is_file():
            return resolved

    for mention in MentionResolver(workspace).resolve_all(user_input):
        if mention.resolved and mention.path is not None and is_prd_filename(mention.path.name):
            return workspace / mention.path

    candidates = _find_workspace_prd_files(workspace)
    if len(candidates) == 1:
        return workspace / candidates[0]
    return None


def _extract_prd_milestones(parsed) -> list[str]:
    lines = parsed.raw_text.splitlines() if parsed.raw_text else []
    milestones = [
        line.strip()
        for line in lines
        if re.match(r"^\s*(milestone|phase|step)\s*\d", line, re.I)
    ]
    return milestones


def _print_prd_build_plan(parsed, relative_path: Path, console: Console) -> None:
    section_names = list(parsed.sections.keys())
    milestones = _extract_prd_milestones(parsed)
    lines = [
        f"File: {relative_path.as_posix()}",
        f"Title: {parsed.title}",
        "",
        "Sections: " + (", ".join(section_names) if section_names else "none"),
    ]
    if milestones:
        lines.append("")
        lines.append("Milestones detected:")
        lines.extend(f"  - {item}" for item in milestones[:12])
        if len(milestones) > 12:
            lines.append(f"  ... {len(milestones) - 12} more")
    lines.append("")
    lines.append("I'll build this now, autonomously (long-running mode), writing files in")
    lines.append("your workspace until it's implemented. Type `exit` to stop.")
    console.print(Panel("\n".join(lines), title="PRD Build Plan"))


PRD_BUILD_FRAMING = (
    "Build complete, runnable product files. Do not create TODO-only stubs or placeholder implementations. "
    "Before rewriting any file that already exists, read it first with read_file and EXTEND it - never "
    "regenerate a file from scratch in a way that drops features implemented in earlier milestones. "
    "Keep the app wired together: if the project has a script.js, index.html must load it with "
    "<script src=\"script.js\"></script> and must NOT keep its own inline game logic or a leftover "
    "placeholder demo (e.g. a rotating-cube snippet). All logic lives in script.js. "
    "Use write_file for file changes and run_command to verify when possible. "
    "A milestone is done only when its acceptance criteria are implemented and the files are wired together "
    "and mutually consistent. If blocked, stop and explain exactly what input is needed."
)


def _ensure_git_repo(
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    """Initialize a git repo (and a basic .gitignore) if the workspace has none,
    so a build starts from a clean, revertable baseline."""
    if (workspace / ".git").exists():
        return
    try:
        result = subprocess.run(
            "git init",
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
    except (OSError, subprocess.SubprocessError):
        return
    if result.returncode != 0:
        return
    gitignore = workspace / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(
            "node_modules/\ndist/\n.shamsu/\n__pycache__/\n*.log\n", encoding="utf-8"
        )
    console.print("[dim]Initialized a git repository for this project.[/dim]")
    _log_event(
        session_logger,
        "project.git.init",
        {"workspace": str(workspace)},
        "Initialized git repository for the build",
        workflow_id="prd-build",
    )


def _ensure_node_modules(workspace: Path, console: Console) -> bool:
    """Ensure dependencies are installed. Returns True if node_modules is ready."""
    if (workspace / "node_modules").exists():
        return True
    console.print("[dim]Installing dependencies (npm install) - first run only...[/dim]")
    try:
        result = subprocess.run(
            "npm install",
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        console.print(f"[yellow]Could not run npm install: {exc}[/yellow]")
        return False
    if result.returncode != 0:
        console.print(
            Panel(
                (result.stderr or result.stdout or "npm install failed").strip()[-3000:],
                title="npm install failed",
                border_style="red",
            )
        )
        return False
    return True


def _run_frontend_typecheck(workspace: Path) -> tuple[bool, str]:
    """Run `tsc --noEmit` for a real compile check. Returns (ok, output)."""
    try:
        result = subprocess.run(
            "npx --no-install tsc --noEmit",
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=300,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run tsc: {exc}"
    return result.returncode == 0, ((result.stdout or "") + (result.stderr or "")).strip()


def _build_frontend_repair_request(errors: str, relative_path: Path) -> str:
    return (
        "The project does NOT compile. Your ONLY task right now is to make `tsc --noEmit` pass by fixing "
        "every TypeScript error below. Do NOT add features, do NOT start a new milestone.\n\n"
        "Critical rules:\n"
        "- Read each failing file AND the files that import it before editing.\n"
        "- App.tsx imports createInitialState, createInputState, and updateGameState from ./game/rules - "
        "every export another file imports MUST exist and keep a compatible signature. Never delete an export "
        "that something imports.\n"
        "- Keep the frontend wired to the game logic: App.tsx must import and render the game state; do not "
        "orphan rules.ts/entities.ts.\n"
        "- Fix ALL errors, use write_file for each change, then stop.\n\n"
        f"PRD file: {relative_path.as_posix()}\n\n"
        f"`tsc --noEmit` output:\n{errors[-4000:]}"
    )


async def _verify_and_repair_frontend(
    workspace: Path,
    relative_path: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    max_attempts: int = 3,
) -> bool:
    """Compile-gate a JS/TS project: typecheck, and if it fails, feed the errors
    back to the agent to fix, looping up to `max_attempts`. Returns True when it
    compiles cleanly. A non-node project (no package.json) is treated as OK."""
    if not (workspace / "package.json").exists():
        return True
    if not (workspace / "tsconfig.json").exists():
        # Not a TypeScript project: nothing for tsc to gate on.
        return True
    if not _ensure_node_modules(workspace, console):
        console.print("[yellow]Skipping compile check - dependencies are not installed.[/yellow]")
        return False
    for attempt in range(1, max_attempts + 1):
        console.print(f"[dim]Checking the game compiles (tsc, attempt {attempt}/{max_attempts})...[/dim]")
        ok, output = _run_frontend_typecheck(workspace)
        if ok:
            console.print(
                "[green]OK: The game compiles cleanly - frontend and game logic are wired together.[/green]"
            )
            _log_event(
                session_logger, "project.typecheck.ok", {"attempt": attempt},
                "Frontend typecheck passed", workflow_id="prd-build",
            )
            return True
        console.print(
            Panel(output[-3000:] or "tsc reported errors.", title=f"Compile errors (attempt {attempt})", border_style="yellow")
        )
        _log_event(
            session_logger, "project.typecheck.failed", {"attempt": attempt},
            "Frontend typecheck failed", workflow_id="prd-build",
        )
        if attempt == max_attempts:
            console.print(
                "[red]Still not compiling after repair attempts. The errors above are what's blocking the "
                "game from showing - tell me to keep fixing and I'll continue.[/red]"
            )
            return False
        console.print("[cyan]Fixing the compile errors before moving on...[/cyan]")
        await _run_agent_chat(
            _build_frontend_repair_request(output, relative_path),
            workspace,
            console,
            session_logger=session_logger,
            force_long_running=True,
            auto_approve=True,
        )
    return False


async def _handle_prd_build_request(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    prd_path = _resolve_build_prd(user_input, workspace)
    if prd_path is None:
        candidates = _find_workspace_prd_files(workspace)
        if len(candidates) > 1:
            console.print("[yellow]I found multiple PRD files - which one should I build from?[/yellow]")
            for path in candidates[:10]:
                console.print(f"- {path.as_posix()}")
            console.print("Name one, e.g. `build the product from \"<file>\"`.")
        else:
            console.print(
                "[yellow]I couldn't find a PRD to build from.[/yellow] "
                "Add a `.md`, `.txt`, or `.pdf` PRD (e.g. named `*prd*` or `Product Requirements*`), "
                "then ask again."
            )
        return

    try:
        parsed = parse_prd_file(prd_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]")
        return

    try:
        relative_path = prd_path.relative_to(workspace)
    except ValueError:
        relative_path = prd_path

    _ensure_git_repo(workspace, console, session_logger)

    project = build_project_spec(parsed)
    if project.category == Category.MULTIPLAYER_GAME.value:
        if not _multiplayer_template_present(workspace):
            console.print(
                Panel(
                    "Detected category: multiplayer-game\n"
                    "Using the v2.3 multiplayer template before model edits.\n"
                    "SHAMSU will create the project folder structure and copy the boilerplate "
                    "into this workspace, then run the template Definition of Done checks.",
                    title="Template Build",
                )
            )
            search, _uses_real_index = _build_search_agent(workspace, session_logger)
            result = await FullDjangoPipeline(
                workspace,
                search=search,
                session_logger=session_logger,
                approval_func=lambda _request: True,
                long_running=is_long_running_enabled(workspace),
            ).run(prd_path, target_dir=".")
            _print_full_pipeline_result(result, console)
            if not result.success:
                return
            console.print(
                "[green]Template is ready. Now filling the game requirements from the PRD.[/green]"
            )
        else:
            # Setup is already done in this workspace (the template files exist),
            # so don't re-run or re-announce the scaffold step every turn - just
            # continue filling the game from the PRD and the current code.
            console.print(
                "[dim]Continuing the game build - setup already done; reading the current files and the PRD.[/dim]"
            )
        await _run_agent_chat(
            _build_multiplayer_game_request(parsed, relative_path),
            workspace,
            console,
            session_logger=session_logger,
            force_long_running=True,
            auto_approve=True,
        )
        # Gate on a real compile: the model just edited the game logic, so make
        # sure it still typechecks (no dropped exports, no broken imports)
        # before declaring victory. On failure, feed the exact errors back to
        # the agent to fix; this is what stops "kept adding features while the
        # frontend was crashing".
        await _verify_and_repair_frontend(
            workspace, relative_path, console, session_logger=session_logger
        )
        return

    _print_prd_build_plan(parsed, relative_path, console)
    _log_event(
        session_logger,
        "prd.build.planned",
        {"path": str(prd_path), "title": parsed.title, "sections": list(parsed.sections)},
        f"Planned PRD build for {prd_path.name}",
        workflow_id="prd-build",
    )

    # The build request itself ("build the product from this prd") is the
    # consent, and the plan above was shown for review - so start the build
    # directly and let it write files without further prompts. This avoids the
    # fragile mid-flow input() approval that could silently auto-deny on some
    # interactive terminals.
    console.print(
        "[green]Building now - I'll read the PRD and write files in your workspace. "
        "Type `exit` to stop.[/green]"
    )
    milestones = _extract_prd_milestones(parsed)
    if not milestones:
        await _run_agent_chat(
            _build_prd_build_request(parsed, relative_path),
            workspace,
            console,
            session_logger=session_logger,
            force_long_running=True,
            auto_approve=True,
        )
        return

    task = _create_prd_build_task(user_input, parsed.title, milestones)
    save_task(task, workspace)
    console.print(f"[dim]Tracking PRD build task: {task.task_id}[/dim]")
    prd_text = parsed.raw_text or _render_sections(parsed)
    for index, milestone in enumerate(milestones):
        step = task.steps[index]
        task = mark_step_running(task, step.id)
        save_task(task, workspace)
        console.print(f"[dim]  -> Milestone {index + 1}/{len(milestones)}: {milestone}[/dim]")
        try:
            await _run_agent_chat(
                _build_prd_milestone_request(parsed.title, relative_path, prd_text, milestone, index + 1, len(milestones)),
                workspace,
                console,
                session_logger=session_logger,
                force_long_running=True,
                auto_approve=True,
            )
        except Exception as exc:
            task = mark_step_failed(task, step.id, str(exc))
            save_task(task, workspace)
            raise
        task = mark_step_done(task, step.id, "Agent completed this milestone build pass.")
        if index < len(milestones) - 1:
            if not advance_phase(task, f"milestone-{index + 2}"):
                save_task(task, workspace)
                console.print(Panel(task.next_action, title="PRD Build Paused", border_style="yellow"))
                return
        save_task(task, workspace)
    console.print(f"[green]PRD milestone build flow complete. Task: {task.task_id}[/green]")


def _multiplayer_template_present(workspace: Path) -> bool:
    required = [
        "package.json",
        "src/App.tsx",
        "src/game/entities.ts",
        "src/game/rules.ts",
        "src/ui/Hud.tsx",
        "src/net/room.ts",
        "server/relay.ts",
    ]
    return all((workspace / path).exists() for path in required)


def _build_multiplayer_game_request(parsed, relative_path: Path) -> str:
    return (
        "You are now past scaffold setup. The multiplayer-game template files already exist in the workspace. "
        "Do real coding work now.\n\n"
        "Rules:\n"
        "- Read the PRD and the existing template files before writing.\n"
        "- Do not ask the user to provide index.html, style.css, script.js, or starter files.\n"
        "- Do not replace the Colyseus relay, React-Three-Fiber scene, lobby, menu, or render loop plumbing.\n"
        "- Fill and adapt the marked template holes in src/game/entities.ts, src/game/rules.ts, and src/ui/Hud.tsx.\n"
        "- Implement the requested gameplay requirements, player rules, scoring, collisions/spawning, HUD values, and end condition.\n"
        "- Use write_file for every file change. Run commands to verify when possible.\n"
        "- Do not claim success unless files were actually written or verified by tool results.\n\n"
        f"PRD file: {relative_path.as_posix()}\n\n"
        f"PRD content:\n{parsed.raw_text or _render_sections(parsed)}"
    )


def _build_continue_game_request() -> str:
    return (
        "Continue the multiplayer game implementation from the previous task. "
        "Do real coding work now: read the existing files, complete unfinished gameplay requirements, "
        "and write the changed files with write_file. Do not print code blocks as the final answer. "
        "Summarize the edited files and what changed after tool results confirm the writes."
    )


def _build_prd_build_request(parsed, relative_path: Path) -> str:
    return (
        f"{PRD_BUILD_FRAMING}\n\n"
        "Build the complete product described by the following PRD. Create all necessary files "
        "in the workspace, working milestone by milestone. Do not claim work you did not do.\n\n"
        f"=== PRD: {relative_path.as_posix()} ===\n"
        f"{parsed.raw_text or _render_sections(parsed)}"
    )


def _build_prd_milestone_request(
    title: str,
    relative_path: Path,
    prd_text: str,
    milestone: str,
    milestone_index: int,
    milestone_count: int,
) -> str:
    return (
        f"{PRD_BUILD_FRAMING}\n\n"
        f"Project: {title}\n"
        f"PRD file: {relative_path.as_posix()}\n"
        f"Current milestone {milestone_index}/{milestone_count}: {milestone}\n\n"
        "FIRST list and read the files already in the workspace (index.html, style.css, script.js, etc.) so "
        "you build ON TOP of the previous milestones instead of replacing their work. THEN implement only this "
        "milestone by editing and extending those files. Every feature from earlier milestones must keep "
        "working, index.html must load script.js (with no inline game logic left behind), and you must write "
        "complete runnable files. Verify with run_command when possible.\n\n"
        f"Full PRD context:\n{prd_text}"
    )


def _create_prd_build_task(user_request: str, title: str, milestones: list[str]) -> MilestoneTask:
    steps = [
        TaskStep(
            id=index + 1,
            description=f"{title}: {milestone}",
            type="file_create",
            specialist="coder",
            phase=f"milestone-{index + 1}",
            depends_on=[index] if index else [],
        )
        for index, milestone in enumerate(milestones)
    ]
    return create_task(user_request=user_request, steps=steps, phase="milestone-1")


def _render_sections(parsed) -> str:
    parts = []
    for name, lines in parsed.sections.items():
        parts.append(f"## {name}")
        parts.extend(lines)
    return "\n".join(parts)


async def _stream_answer(
    console: Console,
    llm: LLMManager,
    pack: ContextPack,
    title: str,
    session_logger: SessionLogger | None,
    workflow_id: str,
    thinking_status: Any = None,
) -> tuple[bool, str]:
    """Stream a specialist answer token-by-token as plain flowing text.

    Returns (streamed, text). `streamed` is False when nothing was rendered
    (immediate failure before any token) so the caller can fall back to the
    non-streaming path. Stops `thinking_status` on the first token to avoid a
    nested Rich Live conflict with the spinner.
    """
    state = {"started": False}
    chunks: list[str] = []

    def on_token(token: str) -> None:
        if not state["started"]:
            if thinking_status is not None:
                thinking_status.stop()
            console.print(f"[bold cyan]{title}[/bold cyan]")
            state["started"] = True
        chunks.append(token)
        console.file.write(token)
        console.file.flush()

    try:
        await llm.run_specialist_stream("qa", pack, on_token)
    except Exception:
        if state["started"]:
            console.file.write("\n")
            console.file.flush()
        else:
            raise
    if state["started"]:
        console.file.write("\n")
        console.file.flush()
        text = "".join(chunks).strip()
        _log_assistant_message(session_logger, text, workflow_id=workflow_id)
        return True, text
    return False, ""


async def _run_qa(
    user_input: str,
    workspace: Path,
    console: Console,
    llm: LLMManager,
    extra_context: str = "",
    session_logger: SessionLogger | None = None,
    thinking_status: Any = None,
) -> None:
    qa_workflow, _uses_real_index = _build_workspace_qa_workflow(workspace, session_logger)
    request = _append_agent_context(user_input, extra_context)
    # Stream when the manager supports it (real LLMManager). Test doubles that
    # only implement run_specialist fall through to the Coordinator path below,
    # preserving their exact behavior.
    if hasattr(llm, "run_specialist_stream"):
        preview = qa_workflow.build_prompt(request)
        try:
            streamed, _text = await _stream_answer(
                console, llm, preview.pack, "Answer", session_logger, "qa", thinking_status
            )
        except Exception:
            streamed = False
        if streamed:
            if _should_show_context_preview() and preview.prompt and _preview_contains_context(preview.prompt):
                console.print(Panel(preview.prompt, title="Context Preview"))
            return
    result = await Coordinator(llm=llm, qa_workflow=qa_workflow).handle(request)
    if result.answer:
        title = f"Answer ({result.model_used})" if result.model_used else "Answer"
        console.print(Panel(result.answer, title=title))
        _log_assistant_message(session_logger, result.answer, workflow_id="qa")
    elif result.fallback_reason:
        console.print(f"[yellow]{result.fallback_reason}[/yellow]")
    if _should_show_context_preview() and result.preview and _preview_contains_context(result.preview):
        console.print(Panel(result.preview, title="Context Preview"))


async def _run_general_chat(
    user_input: str,
    console: Console,
    llm: LLMManager,
    extra_context: str = "",
    session_logger: SessionLogger | None = None,
    thinking_status: Any = None,
) -> None:
    pack = ContextPack(
        task_id="general-chat",
        step_id=1,
        specialist="qa",
        user_request=user_input,
        prd_context=(
            "No indexed project context is attached. "
            "Answer as a general local assistant. "
            "Do not claim you saw code, tests, or files unless they were actually provided. "
            + NO_LIVE_TOOLS_NOTICE
            + (f" {extra_context}" if extra_context else "")
        ),
    )
    if hasattr(llm, "run_specialist_stream"):
        try:
            streamed, _text = await _stream_answer(
                console, llm, pack, "Chat", session_logger, "general-chat", thinking_status
            )
        except Exception:
            streamed = False
        if streamed:
            return
    response = await llm.run_specialist("qa", pack)
    title = f"Chat ({response.model_used})" if response.model_used else "Chat"
    body = response.raw.strip()
    console.print(Panel(body, title=title))
    _log_assistant_message(session_logger, body, workflow_id="general-chat")


async def _run_agent_chat(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    force_long_running: bool = False,
    auto_approve: bool = False,
) -> None:
    # auto_approve is used for an explicitly user-consented PRD build: the user
    # already approved building the whole product, so the agent's file writes
    # and verification commands during that build run without further prompts
    # (this also sidesteps the fragile mid-flow input() approval on Windows).
    approval_func = (lambda _request: True) if auto_approve else ask_approval
    tools = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console, approval_func),
    )
    long_running = force_long_running or is_long_running_enabled(workspace)
    activities: list[str] = []

    def on_activity(msg: str) -> None:
        activities.append(msg)
        console.print(f"[dim]  -> {msg}[/dim]")

    result = await AgentChatLoop(
        workspace,
        session_logger=session_logger,
        tools=tools,
        long_running=long_running,
        on_activity=on_activity,
    ).run(user_input)
    body = result.final.strip() or "No response returned."
    console.print(Panel(_agent_display_summary(body, activities), title="Agent"))
    _log_assistant_message(session_logger, body, workflow_id="agent-chat")


def _should_show_context_preview() -> bool:
    return os.environ.get("SHAMSU_SHOW_CONTEXT", "").lower() in {"1", "true", "yes"}


def _agent_display_summary(body: str, activities: list[str]) -> str:
    written = _written_files_from_activities(activities)
    if "```" not in body and len(body) <= 1600:
        return body

    lines: list[str] = []
    if written:
        lines.append("Edited files:")
        lines.extend(f"- {path}" for path in written)
        lines.append("")
    actions = _summary_bullets_from_text(body)
    if actions:
        lines.append("What changed:")
        lines.extend(f"- {item}" for item in actions[:6])
        lines.append("")
    lines.append("Full generated code is in the edited files. Detailed raw output is kept in the session log.")
    return "\n".join(lines).strip()


def _written_files_from_activities(activities: list[str]) -> list[str]:
    files: list[str] = []
    for item in activities:
        if item.startswith("Writing "):
            path = item.removeprefix("Writing ").strip()
            if path and path not in files:
                files.append(path)
    return files


def _summary_bullets_from_text(body: str) -> list[str]:
    clean = re.sub(r"```.*?```", "", body, flags=re.S)
    bullets: list[str] = []
    for line in clean.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        stripped = re.sub(r"^\d+\.\s*", "", stripped)
        stripped = stripped.lstrip("-* ").strip()
        if stripped and len(stripped) <= 180:
            bullets.append(stripped)
    return bullets


async def _run_web_assist(
    user_input: str,
    console: Console,
    llm: LLMManager,
    web_tool: WebTool,
    session_logger: SessionLogger | None = None,
) -> None:
    result = web_tool.search(
        user_input,
        reason=(
            "SHAMSU thinks this request needs current or external information from the web."
        ),
    )
    fetches: list[WebFetchResult] = []
    if not result.approved:
        await _run_general_chat(
            user_input,
            console,
            llm,
            extra_context=(
                "External web access was denied. Answer with general knowledge only and mention that current details may be stale."
            ),
        )
        return
    if result.error:
        console.print(f"[yellow]Web search failed: {result.error}[/yellow]")
        await _run_general_chat(
            user_input,
            console,
            llm,
            extra_context="Web lookup failed. Answer locally and mention that external lookup was unavailable.",
        )
        return
    top_hits = result.hits[:3]
    if top_hits:
        approval_manager = getattr(web_tool, "approval_manager", None)
        read_approved = True
        if approval_manager is not None:
            approval_manager.session_logger = session_logger
            read_approved = approval_manager.ask(
                ApprovalRequest(
                    action_type="web_search",
                    description="Fetch and read the top web search results.",
                    risk_level="medium",
                    preview="\n".join(f"- {hit.title}: {hit.url}" for hit in top_hits),
                    reason="SHAMSU wants to read the top results once to answer accurately.",
                )
            )
        if read_approved:
            for hit in top_hits:
                try:
                    fetch = web_tool.fetch(
                        hit.url,
                        reason="SHAMSU already has approval to read the top search results.",
                        require_approval=False,
                    )
                except TypeError:
                    fetch = web_tool.fetch(
                        hit.url,
                        reason="SHAMSU already has approval to read the top search results.",
                    )
                if fetch.approved and not fetch.error and _is_useful_web_fetch(fetch):
                    fetches.append(fetch)
    await _print_web_answer(user_input, result, fetches, console, llm, session_logger=session_logger)


async def _run_browser_assist(
    user_input: str,
    console: Console,
    llm: LLMManager,
    browser_tool: BrowserTool,
) -> None:
    url = _extract_url_from_prompt(user_input) or browser_tool.discover_local_url()
    if not url:
        console.print(
            "[yellow]I could not find a running local app to open.[/yellow] "
            "Mention a URL like `http://127.0.0.1:8000`, or start your app first."
        )
        return
    opened = browser_tool.open(
        url,
        reason="SHAMSU wants to inspect the local app in a browser for preview or debugging.",
    )
    if not opened.ok:
        if "denied" in opened.message.lower():
            console.print("[yellow]Browser access denied. Staying in local-only mode.[/yellow]")
            return
        console.print(Panel(opened.message, title="Browser Unavailable", border_style="red"))
        return
    pack = ContextPack(
        task_id="browser-inspect",
        step_id=1,
        specialist="qa",
        user_request=user_input,
        prd_context=(
            f"Browser page URL: {opened.url}\n"
            f"Browser page title: {opened.title}\n"
            f"Visible page text:\n{opened.visible_text}"
        ),
    )
    response = await llm.run_specialist("qa", pack)
    console.print(
        Panel(
            f"{response.raw.strip()}\n\nURL: {opened.url}\nTitle: {opened.title}",
            title=f"Browser Inspection ({response.model_used})" if response.model_used else "Browser Inspection",
        )
    )


async def _print_web_answer(
    query: str,
    result: WebSearchResult,
    fetches: list[WebFetchResult],
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
) -> None:
    if result.error:
        console.print(f"[yellow]Web search failed: {result.error}[/yellow]")
        return
    if not result.hits:
        console.print("[yellow]No web results found.[/yellow]")
        return
    if fetches:
        context = "\n\n".join(
            f"Source: {item.url}\nTitle: {item.title}\n{item.text[:2500]}"
            for item in fetches
        )
        pack = ContextPack(
            task_id="web-qa",
            step_id=1,
            specialist="qa",
            user_request=query,
            prd_context=(
                "Answer the user's question first. Use these web sources. "
                "Cite the sources by URL in a brief source list.\n\n"
                f"{context}"
            ),
        )
        response = await llm.run_specialist("qa", pack)
        body = response.raw.strip()
    else:
        context = "\n".join(
            f"Source: {hit.url}\nTitle: {hit.title}\nSnippet: {hit.snippet or '(no snippet)'}"
            for hit in result.hits[:5]
        )
        pack = ContextPack(
            task_id="web-qa-snippets",
            step_id=1,
            specialist="qa",
            user_request=query,
            prd_context=(
                "SHAMSU could not extract readable page bodies from the web results. "
                "Answer the user's question from the search result titles/snippets and general knowledge. "
                "Be direct, mention uncertainty when snippets are insufficient, and cite the URLs.\n\n"
                f"{context}"
            ),
        )
        response = await llm.run_specialist("qa", pack)
        body = response.raw.strip() or "I found sources, but could not synthesize an answer from the snippets."
    sources = "\n".join(f"- {hit.title}: {hit.url}" for hit in result.hits[:5])
    message = f"{body}\n\nSources:\n{sources}"
    console.print(Panel(message, title="Web Answer"))
    _log_assistant_message(session_logger, message, workflow_id="web")


def _is_useful_web_fetch(fetch: WebFetchResult) -> bool:
    text = fetch.text.strip()
    if len(text) < 120:
        return False
    lowered = text.lower()
    navigation_markers = sum(
        marker in lowered
        for marker in ("cookie", "subscribe", "sign in", "menu", "advertisement")
    )
    return navigation_markers < 4


def _print_web_fetch(fetch: WebFetchResult, console: Console) -> None:
    if not fetch.approved:
        console.print("[yellow]Web fetch denied by user.[/yellow]")
        return
    if fetch.error:
        console.print(Panel(fetch.error, title="Web Fetch Failed", border_style="red"))
        return
    preview = fetch.text[:3000] if fetch.text else "(no visible text extracted)"
    console.print(Panel(f"URL: {fetch.url}\nTitle: {fetch.title}\n\n{preview}", title="Web Page"))


def _print_browser_result(result, console: Console) -> None:
    if not result.ok:
        console.print(Panel(result.message, title="Browser Action Failed", border_style="red"))
        return
    body = result.message or result.visible_text[:3000] or "Browser action completed."
    if result.screenshot_path:
        body = f"{body}\n\nScreenshot: {result.screenshot_path}"
    if result.url:
        body = f"URL: {result.url}\nTitle: {result.title}\n\n{body}"
    console.print(Panel(body, title="Browser"))


def _is_casual_prompt(user_input: str) -> bool:
    text = re.sub(r"[^\w\s]", "", user_input.lower()).strip()
    return text in {
        "hi",
        "hello",
        "hey",
        "yo",
        "sup",
        "hiya",
        "good morning",
        "good afternoon",
        "good evening",
    }


def _is_general_chat_prompt(user_input: str) -> bool:
    text = user_input.strip().lower()
    if not text:
        return True
    if _is_casual_prompt(text):
        return True
    project_markers = (
        "this project",
        "this repo",
        "this codebase",
        "this code",
        "workspace",
        "index",
        "file ",
        ".py",
        ".md",
        "function",
        "class",
        "module",
        "traceback",
        "stack trace",
        "failing test",
        "test failure",
        "bug",
        "fix ",
        "edit ",
        "readme",
        "endpoint",
        "serializer",
        "viewset",
        "model ",
        "django",
        "auth",
        "login",
    )
    return not any(marker in text for marker in project_markers)


def _is_project_local_prompt(text: str) -> bool:
    return not _is_general_chat_prompt(text)


def _looks_like_react_prompt(user_input: str) -> bool:
    text = user_input.lower()
    return any(
        phrase in text
        for phrase in (
            "create file",
            "create a file",
            "write file",
            "write a file",
            "save as ",
            "run command",
            "run this command",
            "run the tests",
            "run tests",
            "what did you just",
            "what did you make",
            "what did you create",
        )
    ) or bool(re.search(r"\b(create|write|make)\s+[\w./\\ -]+\.[A-Za-z0-9_]+\b", user_input, re.I))


def _print_ready_message(workspace: Path, console: Console) -> None:
    console.print(
        "[green]SHAMSU is ready.[/green] "
        f"Workspace: [bold]{workspace}[/bold]\n"
        "Run `index` for project-aware answers, `models status` for local AI status, "
        "or ask me to edit, test, audit, document, or generate a Django project."
    )


def _preview_contains_context(preview: str) -> bool:
    if "# File:" in preview:
        return True
    for section in ("Context", "Errors / test output"):
        match = re.search(
            rf"## {re.escape(section)}\n(?P<body>.*?)(?=\n## |\Z)",
            preview,
            flags=re.S,
        )
        if match and match.group("body").strip():
            return True
    return False


async def _run_code_edit(
    user_input: str,
    workspace: Path,
    search: SearchAgent | EmptySearchAgent,
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
) -> None:
    _warn_if_dirty_before_edit(workspace, console)
    kwargs = {}
    if session_logger:
        kwargs["patch_engine"] = PatchEngine(
            workspace,
            session_logger=session_logger,
            approval_manager=_make_approval_manager(workspace, session_logger, console),
        )
    result = await CodeEditWorkflow(workspace, search=search, llm=llm, **kwargs).run(
        _strip_forced_prefix(user_input, "edit")
    )
    if getattr(result, "used_full_rewrite", False):
        console.print("[dim]The diff didn't parse cleanly, so I rewrote the file(s) in full instead.[/dim]")
    _print_patch_result("Code Edit", result.applied, result.changed_files, result.error, console)


async def _run_bug_fix(
    user_input: str,
    workspace: Path,
    search: SearchAgent | EmptySearchAgent,
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
) -> None:
    _warn_if_dirty_before_edit(workspace, console)
    kwargs = {}
    if session_logger:
        kwargs["patch_engine"] = PatchEngine(
            workspace,
            session_logger=session_logger,
            approval_manager=_make_approval_manager(workspace, session_logger, console),
        )
    result = await BugFixWorkflow(workspace, search=search, llm=llm, **kwargs).run(
        _strip_forced_prefix(user_input, "fix")
    )
    if getattr(result, "used_full_rewrite", False):
        console.print("[dim]The diff didn't parse cleanly, so I rewrote the file(s) in full instead.[/dim]")
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
    _warn_if_dirty_before_edit(workspace, console)
    kwargs = {}
    if session_logger:
        approval_manager = _make_approval_manager(workspace, session_logger, console)
        kwargs["patch_engine"] = PatchEngine(
            workspace, session_logger=session_logger, approval_manager=approval_manager
        )
        kwargs["command_runner"] = CommandRunner(
            workspace, session_logger=session_logger, approval_manager=approval_manager
        )
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
    _warn_if_dirty_before_edit(workspace, console)
    result = await DocumentationWorkflow(
        search=search,
        llm=llm,
        workspace_root=workspace,
        **(
            {
                "patch_engine": PatchEngine(
                    workspace,
                    session_logger=session_logger,
                    approval_manager=_make_approval_manager(workspace, session_logger, console),
                )
            }
            if session_logger
            else {}
        ),
    ).apply_readme_update(request=_strip_forced_prefix(user_input, "docs"))
    _print_patch_result("Documentation", result.applied, result.changed_files, result.error, console)


def _warn_if_dirty_before_edit(workspace: Path, console: Console) -> None:
    warning = GitTool(workspace).warn_if_dirty()
    if warning and warning != "Workspace is not a git repository.":
        console.print(f"[yellow]{warning}[/yellow]")


async def _handle_django_fix_tests(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    parts = user_input.split(maxsplit=2)
    project_dir = parts[2].strip() if len(parts) > 2 else "."
    search, _uses_real_index = _build_search_agent(workspace, session_logger)
    llm = _make_llm_manager(session_logger, console)
    result = await ErrorFeedbackLoop(
        workspace,
        search=search,
        llm=llm,
        patch_engine=PatchEngine(
            workspace,
            session_logger=session_logger,
            approval_manager=_make_approval_manager(workspace, session_logger, console),
        ),
        session_logger=session_logger,
        long_running=is_long_running_enabled(workspace),
    ).run(project_dir)
    _print_django_test_result(result.final_result, console)
    if result.success:
        console.print(f"[green]Django tests passed after {len(result.iterations)} fix attempt(s).[/green]")
        return
    console.print(Panel(result.error or "Django tests still failing.", title="Fix Loop Stopped", border_style="red"))


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
    normalized = _normalize_command_input(user_input)
    prefix = f"{command} "
    if normalized.lower().startswith(prefix):
        return normalized[len(prefix):].strip()
    return user_input


def _normalize_command_input(user_input: str) -> str:
    stripped = user_input.strip()
    if stripped.startswith("/"):
        return stripped[1:].strip()
    return stripped


def _thinking_status_for_input(user_input: str) -> str:
    normalized = _normalize_command_input(user_input).lower()
    if normalized.startswith("parse-prd ") or "prd" in normalized:
        return "[dim]Checking workspace PRD files...[/dim]"
    if normalized.startswith("web ") or _looks_like_web_needed_prompt(user_input):
        return "[dim]Checking whether web lookup is needed...[/dim]"
    if normalized.startswith("browse ") or _looks_like_browser_needed_prompt(user_input):
        return "[dim]Inspecting in the browser...[/dim]"
    if normalized.startswith("search ") or normalized.startswith("symbols "):
        return "[dim]Searching indexed workspace context...[/dim]"
    if normalized.startswith(("edit ", "fix ", "test-gen ", "audit ", "docs ")):
        return "[dim]Thinking through the requested workflow...[/dim]"
    return "[dim]Thinking...[/dim]"


def _install_console_status_tracker(console: Console) -> None:
    """Track active Rich statuses so blocking prompts can pause them."""
    if getattr(console, "_shamsu_status_tracker_installed", False):
        return
    original_status = console.status

    class _TrackedStatus:
        def __init__(self, status) -> None:
            self._status = status

        def __enter__(self):
            active = getattr(console, "_shamsu_active_statuses", None)
            if active is None:
                active = []
                setattr(console, "_shamsu_active_statuses", active)
            active.append(self._status)
            return self._status.__enter__()

        def __exit__(self, exc_type, exc_val, exc_tb):
            try:
                return self._status.__exit__(exc_type, exc_val, exc_tb)
            finally:
                active = getattr(console, "_shamsu_active_statuses", [])
                if self._status in active:
                    active.remove(self._status)

        def __getattr__(self, name: str):
            return getattr(self._status, name)

    def tracked_status(*args, **kwargs):
        return _TrackedStatus(original_status(*args, **kwargs))

    console.status = tracked_status
    setattr(console, "_shamsu_status_tracker_installed", True)


def _print_startup_banner(workspace: Path, console: Console) -> None:
    model = model_for_role("qa")
    autonomy = "on" if is_long_running_enabled(workspace) else "off"
    runtime = status_text(collect_status())
    body = Text()
    body.append("SHAMSU v0.3.0", style="bold")
    body.append("  |  Local AI coding agent\n", style="dim")
    body.append("Workspace: ", style="dim")
    body.append(f"{workspace}\n")
    body.append("Model: ", style="dim")
    body.append(f"{model}", style="cyan")
    body.append("  |  Autonomy: ", style="dim")
    body.append(autonomy, style=("green" if autonomy == "on" else "yellow"))
    body.append("\nRuntime: ", style="dim")
    body.append(runtime, style="dim")
    console.print(Panel(body, title="SHAMSU", border_style="cyan"))


def _bottom_toolbar(workspace: Path) -> str:
    autonomy = "on" if is_long_running_enabled(workspace) else "off"
    model = model_for_role("qa")
    return f" {workspace}  |  model: {model}  |  autonomy: {autonomy}  |  /help  /exit "


class CachedBottomToolbar:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.value = _bottom_toolbar(workspace)

    def refresh(self) -> None:
        self.value = _bottom_toolbar(self.workspace)

    def __call__(self) -> str:
        return self.value


def _make_input_key_bindings() -> KeyBindings:
    """Enter submits; Alt+Enter (Meta+Enter) inserts a newline for deliberate
    multi-line input. Combined with multiline=True this also lets pasted,
    multi-line text (tracebacks, PRDs) land intact without submitting early."""
    kb = KeyBindings()

    @kb.add("enter")
    def _(event) -> None:
        buffer = event.current_buffer
        completion_state = buffer.complete_state
        if completion_state is not None and completion_state.current_completion is not None:
            buffer.apply_completion(completion_state.current_completion)
            return
        buffer.validate_and_handle()

    @kb.add("escape", "enter")
    def _(event) -> None:
        event.current_buffer.insert_text("\n")

    return kb


def _make_prompt_session(workspace: Path, bottom_toolbar: Callable[[], str] | None = None) -> PromptSession | None:
    style = Style.from_dict(
        {
            "prompt": "ansigreen bold",
            "workspace": "ansiblue",
            "bottom-toolbar": "bg:#222222 #aaaaaa",
        }
    )
    try:
        return PromptSession(
            history=InMemoryHistory(),
            style=style,
            completer=SlashCommandCompleter(workspace),
            complete_while_typing=True,
            bottom_toolbar=bottom_toolbar or CachedBottomToolbar(workspace),
            multiline=True,
            key_bindings=_make_input_key_bindings(),
            prompt_continuation=lambda width, line_number, is_soft_wrap: "".ljust(width),
        )
    except NoConsoleScreenBufferError:
        return None


def _force_utf8_stdio() -> None:
    """Best-effort: make stdout/stderr UTF-8 so non-ASCII output (arrow/box glyphs)
    never crashes on a Windows console, even if PYTHONUTF8 wasn't set by the
    launcher (e.g. run directly via `python -m shamsu.cli.repl`)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def main(argv: list[str] | None = None) -> None:
    _force_utf8_stdio()
    args = parse_args(argv)
    console = Console()
    _install_console_status_tracker(console)

    try:
        workspace = resolve_workspace(args.workspace)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(2)

    # Track this session so the last one to exit can free SHAMSU's Ollama
    # footprint (stop a SHAMSU-started server, or unload SHAMSU's models - incl.
    # the keep_alive=-1 router - from a shared/tray-app server). Best-effort.
    session_pid = register_session()
    atexit.register(shutdown_if_last_session, session_pid)

    _print_startup_banner(workspace, console)
    ancestor_workspace = find_ancestor_workspace(workspace)
    if ancestor_workspace is not None:
        console.print(
            f"[yellow]Note: a parent directory already has a SHAMSU workspace at "
            f"{ancestor_workspace}. Continuing here will create a separate workspace at "
            f"{workspace}. Pass --workspace {ancestor_workspace} to use the existing one "
            f"instead, or run `/doctor` for more detail.[/yellow]"
        )
    session_manager = SessionManager(workspace)
    try:
        session_logger = _start_session(args, workspace, console)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(2)
    console.print("[dim]Type a prompt, or `/help` for commands.[/dim]\n")
    web_tool = WebTool(
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
    )
    browser_tool = BrowserTool(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
    )
    bottom_toolbar = CachedBottomToolbar(workspace)
    session = _make_prompt_session(workspace, bottom_toolbar)
    command_router = CommandRouter(SYSTEM_COMMANDS)

    while True:
        try:
            if session is None:
                raw_input_text = input("shamsu> ")
            else:
                raw_input_text = session.prompt([("class:prompt", "shamsu> ")])
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            sys.exit(0)

        # Strip a stray leading BOM that piped stdin can prepend; it is
        # not whitespace, so .strip() alone leaves it and it would break slash
        # commands and pollute previews.
        user_input = raw_input_text.lstrip("\ufeff").strip()
        if not user_input:
            continue
        previous_user_prompt = session_logger.metadata.last_user_prompt
        session_logger.log(
            "user.prompt",
            {"prompt": user_input},
            "User submitted prompt",
            workflow_id="repl",
        )
        if user_input.startswith("/"):
            route = command_router.route(user_input)
            if not route.valid:
                console.print(f"[red]{route.error}[/red]")
                if route.suggestions:
                    console.print("Did you mean: " + ", ".join(route.suggestions))
                continue
            normalized_input = route.normalized
        else:
            normalized_input = _normalize_command_input(user_input)
        lowered_input = normalized_input.lower()
        if lowered_input in {"exit", "quit"}:
            print("Goodbye.")
            break
        if lowered_input == "help":
            _print_help(console)
            continue
        if lowered_input == "index":
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_index(workspace, console, session_logger=session_logger)
            continue
        if lowered_input == "status":
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_status(workspace, console, session_logger=session_logger)
            continue
        if lowered_input == "doctor":
            _handle_doctor(workspace, console)
            continue
        if lowered_input.startswith("search "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_search(normalized_input, workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("symbols "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_symbols(normalized_input, workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("parse-prd "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_parse_prd(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("plan-prd "):
            _handle_plan_prd(normalized_input, workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("generate-django "):
            _handle_generate_django(normalized_input, workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("generate-prd "):
            asyncio.run(_handle_generate_prd(normalized_input, workspace, console, session_logger))
            continue
        if lowered_input.startswith("models"):
            _handle_models(normalized_input, console)
            continue
        if lowered_input.startswith("web "):
            _handle_web(normalized_input, console, web_tool, _make_llm_manager(session_logger, console))
            continue
        if lowered_input.startswith("browse "):
            _handle_browse(normalized_input, console, browser_tool)
            continue
        if lowered_input.startswith("django"):
            if lowered_input.startswith("django fix-tests"):
                asyncio.run(_handle_django_fix_tests(normalized_input, workspace, console, session_logger))
            else:
                _handle_django(normalized_input, workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("sessions"):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                session_logger = _handle_sessions(normalized_input, session_manager, session_logger, console)
                web_tool.session_logger = session_logger
                browser_tool.session_logger = session_logger
            continue
        if lowered_input.startswith("permissions"):
            _handle_permissions(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("tasks"):
            _handle_tasks(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("autonomy"):
            _handle_autonomy(normalized_input, workspace, console)
            bottom_toolbar.refresh()
            continue
        if lowered_input == "log" or lowered_input.startswith("log "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_log(normalized_input, session_logger, console)
            continue

        with console.status(_thinking_status_for_input(user_input), spinner="dots") as thinking:
            asyncio.run(
                _handle_request(
                    user_input,
                    workspace,
                    console,
                    web_tool,
                    browser_tool,
                    previous_user_prompt=previous_user_prompt,
                    session_logger=session_logger,
                    thinking_status=thinking,
                )
            )

    browser_tool.close()


if __name__ == "__main__":
    main()
