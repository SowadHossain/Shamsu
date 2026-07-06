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

from shamsu.abstract.service import REQUIRED_TOOL_MESSAGE, AbstractService
from shamsu.agents.audit_workflow import AuditWorkflow
from shamsu.diagnostics import doctor as diagnostics_doctor
from shamsu.diagnostics import setup as diagnostics_setup
from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.diagnostics.setup import DiagnosticsWorkspace
from shamsu.agents.bugfix_workflow import BugFixWorkflow
from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.agents.code_edit_workflow import CodeEditWorkflow
from shamsu.agents.doc_workflow import DocumentationWorkflow
from shamsu.agents.error_feedback_loop import ErrorFeedbackLoop
from shamsu.agents.full_pipeline import FullDjangoPipeline, FullPipelineResult
from shamsu.agents.orchestrator import AgentOrchestrator
from shamsu.cli.command_router import CommandRouter
from shamsu.agents.qa_workflow import NO_LIVE_TOOLS_NOTICE, QAWorkflow
from shamsu.agents.task_harness import append_task_handoff, build_task_plan, plan_log_payload
from shamsu.agents.test_generation_workflow import TestGenerationWorkflow
from shamsu.core.coordinator import Coordinator
from shamsu.llm.manager import LLMManager, ModelPullProgress
from shamsu.memory.service import MemoryService, REQUIRED_MEMORY_MESSAGE
from shamsu.prd.input import PRDParseError, is_prd_filename, parse_prd_file
from shamsu.prd.project import build_project_spec
from shamsu.prd.state import create_generation_state, save_generation_state
from shamsu.registry.schema import Category
from shamsu.retriever.search import NullSearchAgent, SearchAgent
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
from shamsu.runtime.models import (
    DEFAULT_TIER,
    ModelTier,
    active_tier,
    initialize_model_tier,
    model_for_role,
    set_model_tier,
    tier_ever_configured,
    tier_model_specs,
)
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
from shamsu.safety.approval import ask_approval, ask_approval_menu, ask_tier_choice
from shamsu.safety.autonomy import is_long_running_enabled, set_long_running_enabled
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.permission_store import PermissionMemory
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.patch import git_apply as patch_git_apply
from shamsu.patch import types as patch_types
from shamsu.patch.engine import PatchEngine
from shamsu.patch.preview import print_diff_preview
from shamsu.session.manager import SessionLogger, SessionManager
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.tools.browser import BrowserTool
from shamsu.tools.dev_server import DevServerManager, infer_dev_command, is_dev_server_command
from shamsu.tools.web import WebFetchResult, WebSearchResult, WebTool
from shamsu.tools.codebase_memory import CodebaseMemoryAdapter
from shamsu.tools.django import DjangoSetupResult, DjangoSetupRunner, DjangoTestRunner
from shamsu.tools.executor import CommandRunner
from shamsu.tools.git import GitTool
from shamsu.tools.workspace import MentionResolver, WorkspaceTool
from shamsu.ui.progress import ProgressReporter
from shamsu.types import (
    ApprovalRequest,
    ContextPack,
    ProjectSpec,
    RoutingDecision,
    TaskStep,
    TaskStepStatus,
)

if sys.platform == "win32":
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError
else:
    NoConsoleScreenBufferError = RuntimeError

DEFAULT_ASK_APPROVAL = ask_approval

EmptySearchAgent = NullSearchAgent


SYSTEM_COMMANDS = (
    "/help",
    "/doctor",
    "/abstract status",
    "/abstract setup",
    "/abstract repair",
    "/abstract build",
    "/abstract refresh",
    "/abstract query ",
    "/abstract exports ",
    "/abstract imports ",
    "/abstract symbols ",
    "/abstract who-uses ",
    "/abstract impact ",
    "/diagnostics status",
    "/diagnostics setup",
    "/diagnostics repair",
    "/diagnostics last",
    "/diagnostics parse",
    "/diagnostics explain",
    "/diagnostics sources",
    "/memory status",
    "/memory setup",
    "/memory repair",
    "/memory remember ",
    "/memory search ",
    "/memory recent",
    "/memory forget ",
    "/memory summarize-session ",
    "/parse-prd ",
    "/plan-prd ",
    "/generate-django ",
    "/generate-prd ",
    "/models status",
    "/models pull",
    "/models repair",
    "/models tier",
    "/models tier light",
    "/models tier default",
    "/models tier heavy",
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
    "/trace status",
    "/trace on",
    "/trace off",
    "/trace verbose",
    "/diagnostics setup",
    "/diagnostics repair",
    "/diagnostics status",
    "/diagnostics last",
    "/diagnostics parse",
    "/diagnostics explain",
    "/diagnostics sources",
    "/patch status",
    "/patch preview ",
    "/patch apply ",
    "/patch rollback ",
    "/patch journal",
    "/patch last",
    "/patch diff ",
    "/patch trash",
    "/patch clean-trash",
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
                    "  /doctor                   Diagnose install/workspace health (read-only)",
                    "  /abstract status          Show Codebase-Memory MCP + index health",
                    "  /abstract setup           Install the local Codebase-Memory MCP tool",
                    "  /abstract repair          Re-check health and rebuild the index if needed",
                    "  /abstract build|refresh   Build/refresh the workspace code-memory index",
                    "  /abstract query <text>    Structural search over the code graph",
                    "  /abstract exports|imports <file>",
                    "  /abstract symbols <name-or-file>",
                    "  /abstract who-uses <symbol>   Callers/importers of a symbol",
                    "  /abstract impact <symbol>     Edit impact / blast radius",
                    "  /diagnostics status      Show deterministic parser/compactor health",
                    "  /diagnostics setup       Initialize local diagnostics config",
                    "  /diagnostics repair      Re-check diagnostics helper setup",
                    "  /diagnostics last        Show latest compact ErrorPacket",
                    "  /diagnostics parse       Re-parse latest raw command log",
                    "  /diagnostics explain     Explain root selection policy",
                    "  /diagnostics sources     Show parser/helper chain for latest log",
                    "  /memory status           Show Graphiti long-term memory health",
                    "  /memory setup            Install/configure local Graphiti memory",
                    "  /memory repair           Re-check and repair Graphiti config",
                    "  /memory remember <text>  Store explicit durable memory",
                    "  /memory search <query>   Search Graphiti memory",
                    "  /memory forget <query>   Forget/mark memory via Graphiti adapter",
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
                    "  /models tier [light|default|heavy]  Show or switch model tier",
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
                    "  /diagnostics status       Show diagnostic helper availability (Drain3, etc.)",
                    "  /diagnostics setup        Install optional local diagnostic helpers",
                    "  /diagnostics repair       Re-check diagnostic helpers and print repair steps",
                    "  /diagnostics last         Show the latest parsed ErrorPacket",
                    "  /diagnostics parse        Re-parse the latest command output",
                    "  /diagnostics explain      Explain the deterministic root-cause selection",
                    "  /diagnostics sources      Show which parser handled the latest output",
                    "  /patch status             Show git-apply availability, trash, last transaction",
                    "  /patch preview <file>     Preview a diff or change-request JSON without applying it",
                    "  /patch apply <file>       Apply a change-request JSON through the mutation engine",
                    "  /patch rollback <id>      Restore every file a transaction touched",
                    "  /patch journal            List all recorded transactions",
                    "  /patch last               Show the most recent transaction",
                    "  /patch diff <id>          Show the diff applied by a transaction",
                    "  /patch trash              List files moved to .shamsu/trash",
                    "  /patch clean-trash        Permanently delete everything in trash (with approval)",
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


def _build_search_agent(
    workspace: Path,
    session_logger: SessionLogger | None = None,
) -> tuple[SearchAgent | EmptySearchAgent, bool]:
    """Codebase-Memory MCP-backed search, auto-building/refreshing the index
    via the same gate `/abstract` and AgentOrchestrator use. Falls back to a
    no-op agent (not a local SQLite index) when the tool is unavailable."""
    gate = AbstractService(workspace).ensure_ready()
    if gate.allowed:
        return SearchAgent(workspace), True
    return EmptySearchAgent(), False


def _build_workspace_qa_workflow(
    workspace: Path,
    session_logger: SessionLogger | None = None,
) -> tuple[QAWorkflow, bool]:
    search, uses_real_index = _build_search_agent(workspace, session_logger)
    return QAWorkflow(search=search), uses_real_index


def _handle_parse_prd(user_input: str, workspace: Path, console: Console) -> None:
    _, _, path_text = user_input.partition(" ")
    cleaned_path = path_text.strip().strip('"').strip("'")
    if not cleaned_path:
        console.print("[red]Usage: parse-prd <file>[/red]")
        return
    try:
        file_path = _resolve_workspace_file(cleaned_path, workspace)
    except SecurityError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        return
    if not file_path.exists() or not file_path.is_file():
        console.print(f"[red]File not found: {file_path}[/red]")
        return
    try:
        parsed = parse_prd_file(file_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
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
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        return
    if not file_path.exists() or not file_path.is_file():
        console.print(f"[red]File not found: {file_path}[/red]")
        return
    try:
        parsed = parse_prd_file(file_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
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
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        return
    if not file_path.exists() or not file_path.is_file():
        console.print(f"[red]File not found: {file_path}[/red]")
        return
    try:
        parsed = parse_prd_file(file_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
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
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
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


def _ensure_code_memory_ready_at_startup(workspace: Path, console: Console) -> None:
    """Startup-time Codebase-Memory MCP check: auto-build/refresh the index so
    the first real prompt doesn't pay that cost, and surface the exact
    required-tool message immediately if the tool itself is unavailable.

    Best-effort: an unexpected error here must never prevent SHAMSU from
    starting - it's surfaced as a warning, and `/abstract repair` / `/doctor`
    remain available to investigate."""
    try:
        service = AbstractService(workspace)
        health = service.adapter.healthcheck(workspace)
        if not health.ok:
            console.print(f"[yellow]{REQUIRED_TOOL_MESSAGE}[/yellow]")
            return
        index = service.index_status()
        if not index.exists:
            label = "[dim]Code memory: building...[/dim]"
        elif index.stale:
            label = "[dim]Code memory: refreshing...[/dim]"
        else:
            console.print("[dim]Code memory: ready[/dim]")
            return
        with console.status(label, spinner="dots"):
            gate = service.ensure_ready()
        if gate.allowed:
            console.print("[dim]Code memory: ready[/dim]")
        else:
            console.print("[yellow]Code memory: failed, run /abstract repair[/yellow]")
    except Exception as exc:
        console.print(
            f"[yellow]Code memory: startup check failed ({exc}). Run /abstract repair or /doctor.[/yellow]"
        )


def _handle_doctor(workspace: Path, console: Console) -> None:
    report = run_doctor(workspace=workspace)
    console.print(format_report(report))


def _handle_abstract(user_input: str, workspace: Path, console: Console) -> None:
    _, _, rest = user_input.partition(" ")
    parts = rest.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""
    service = AbstractService(workspace)

    if subcommand == "status":
        status = service.status()
        console.print(f"Available: {status.health.available} ({status.health.message})")
        console.print(f"Index: {'stale' if status.index.stale else 'fresh'} - {status.index.message}")
        console.print(f"Normal code-agent mode allowed: {status.normal_mode_allowed}")
        return
    if subcommand == "setup":
        result = service.setup()
        console.print("[green]Setup complete.[/green]" if result.get("ok") else f"[red]Setup failed: {result.get('error', result)}[/red]")
        return
    if subcommand == "repair":
        result = service.repair()
        console.print("[green]Repair complete.[/green]" if result.get("ok") else f"[red]Repair failed: {result.get('message', result)}[/red]")
        return
    if subcommand == "build":
        console.print(service.adapter.index_workspace(workspace))
        return
    if subcommand == "refresh":
        console.print(service.adapter.refresh_workspace(workspace))
        return
    if subcommand == "query":
        if not argument:
            console.print("[red]Usage: /abstract query <query>[/red]")
            return
        console.print(service.adapter.query(workspace, argument))
        return
    if subcommand == "exports":
        console.print(service.adapter.get_exports(workspace, argument))
        return
    if subcommand == "imports":
        console.print(service.adapter.get_imports(workspace, argument))
        return
    if subcommand == "symbols":
        console.print(service.adapter.get_symbols(workspace, argument))
        return
    if subcommand == "who-uses":
        console.print(service.adapter.get_references(workspace, argument))
        return
    if subcommand == "impact":
        console.print(service.adapter.get_impact(workspace, argument))
        return
    console.print(
        "[red]Usage: /abstract status|setup|repair|build|refresh|query|exports|imports|symbols|who-uses|impact[/red]"
    )



def _handle_diagnostics(user_input: str, workspace: Path, console: Console) -> None:
    _, _, rest = user_input.partition(" ")
    parts = rest.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else "status"
    ws = DiagnosticsWorkspace(workspace)
    if subcommand == "status":
        console.print(diagnostics_doctor.format_report(diagnostics_doctor.check(workspace)))
        return
    if subcommand == "setup":
        console.print(diagnostics_setup.setup(workspace))
        return
    if subcommand == "repair":
        console.print(diagnostics_doctor.repair(workspace))
        return
    if subcommand == "last":
        packet = ws.last_packet()
        if not packet:
            console.print("[yellow]No ErrorPacket recorded yet.[/yellow]")
            return
        console.print(Panel(_format_diagnostic_packet(packet), title="Latest ErrorPacket"))
        return
    if subcommand == "parse":
        packet = ws.last_packet()
        raw_log = packet.get("raw_log_path") if packet else ""
        if not raw_log or not Path(raw_log).exists():
            console.print("[yellow]No raw diagnostic log is available to parse.[/yellow]")
            return
        text = Path(raw_log).read_text(encoding="utf-8", errors="replace")
        parsed = DiagnosticDigest(workspace).run(
            packet.get("command", "unknown"),
            packet.get("cwd", workspace),
            int(packet.get("exit_code", 1)),
            text,
            "",
            raw_log_path=raw_log,
        )
        console.print(Panel(parsed.to_model_context(), title="Re-parsed ErrorPacket"))
        return
    if subcommand == "explain":
        packet = ws.last_packet()
        if not packet:
            console.print("No ErrorPacket recorded yet.")
            return
        console.print(_explain_diagnostic_packet(packet))
        return
    if subcommand == "sources":
        packet = ws.last_packet()
        if not packet:
            console.print("No ErrorPacket recorded yet.")
            return
        console.print("Parser chain: " + ", ".join(packet.get("parser_chain", []) or ["unknown"]))
        return
    console.print("[red]Usage: /diagnostics status|setup|repair|last|parse|explain|sources[/red]")

def _explain_diagnostic_packet(packet: dict[str, Any]) -> str:
    lines = ["Deterministic root cause selection:"]
    roots = packet.get("root_diagnostics", [])
    if not roots:
        lines.append("- No root diagnostics were recorded in the latest ErrorPacket.")
        return "\n".join(lines)
    lines.append("- Native/SARIF/errorformat parsing runs before fallback parsers; no LLM parses raw logs first.")
    lines.append("- Syntax and import/export errors are ranked before cascading noise.")
    for item in roots[:5]:
        code = f" {item.get('code')}" if item.get("code") else ""
        lines.append(f"- Root cause: {item.get('category')}{code} {item.get('message', '')}".strip())
    return "\n".join(lines)
def _format_diagnostic_packet(packet: dict[str, Any]) -> str:
    lines = [packet.get("summary", "No summary.")]
    roots = packet.get("root_diagnostics", [])
    if roots:
        lines.append("Root diagnostics:")
        for item in roots[:5]:
            loc = item.get("file") or item.get("module") or "unknown"
            if item.get("line"):
                loc += f":{item.get('line')}"
            code = f" {item.get('code')}" if item.get("code") else ""
            lines.append(f"- {item.get('category')}{code} {loc} {item.get('message')}")
    snippets = packet.get("recommended_snippets", [])
    if snippets:
        lines.append("Recommended snippets:")
        for snippet in snippets[:8]:
            lines.append(f"- {snippet.get('file')}:{snippet.get('line_start')}-{snippet.get('line_end')} {snippet.get('reason', '')}")
    facts = packet.get("related_code_facts", [])
    if facts:
        lines.append("Related code facts:")
        lines.extend(f"- {fact}" for fact in facts[:8])
    if packet.get("raw_log_path"):
        lines.append(f"Raw log: {packet.get('raw_log_path')}")
    return "\n".join(lines)
def _ensure_graphiti_ready_at_startup(workspace: Path, console: Console) -> None:
    try:
        service = MemoryService(workspace)
        gate = service.ensure_ready()
        if gate.allowed:
            console.print("[dim]Graphiti memory: ready[/dim]")
        else:
            console.print(f"[yellow]{REQUIRED_MEMORY_MESSAGE}[/yellow]")
    except Exception as exc:
        console.print(f"[yellow]Graphiti memory: startup check failed ({exc}). Run /memory repair or /doctor.[/yellow]")


def _memory_command_allowed(normalized_input: str) -> bool:
    lowered = normalized_input.lower()
    return (
        lowered in {"help", "doctor", "exit", "quit"}
        or lowered.startswith("memory")
        or lowered.startswith("diagnostics")
        or lowered.startswith("patch")
    )


def _handle_memory(user_input: str, workspace: Path, console: Console) -> None:
    _, _, rest = user_input.partition(" ")
    parts = rest.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else "status"
    argument = parts[1].strip() if len(parts) > 1 else ""
    service = MemoryService(workspace)

    if subcommand == "status":
        status = service.status()
        console.print(f"Available: {status.health.available} ({status.health.message})")
        console.print(f"Config: {status.health.config_path or service._config_path()}")
        console.print(f"Workspace memory path: {status.memory_path}")
        console.print(f"Normal agent mode allowed: {status.normal_mode_allowed}")
        return
    if subcommand == "setup":
        result = service.setup()
        if result.get("ok"):
            console.print("[green]Graphiti setup complete.[/green]")
        else:
            reason = result.get("error") or result.get("manual_steps") or result.get("message") or result
            console.print(f"[red]Graphiti setup failed: {reason}[/red]")
        return
    if subcommand == "repair":
        result = service.repair()
        if result.get("ok"):
            console.print("[green]Graphiti repair complete.[/green]")
        else:
            reason = result.get("manual_steps") or result.get("message") or result.get("error") or result
            console.print(f"[red]Graphiti repair failed: {reason}[/red]")
        return
    if subcommand == "remember":
        if not argument:
            console.print("[red]Usage: /memory remember <text>[/red]")
            return
        result = service.remember(argument)
        if result.get("ok"):
            console.print("[green]Memory stored.[/green]" if not result.get("deduped") else "[green]Memory already existed.[/green]")
        else:
            console.print(f"[yellow]Memory not stored: {result.get('reason') or result.get('error') or result}[/yellow]")
        return
    if subcommand in {"search", "recent"}:
        query = argument or "recent durable SHAMSU memory"
        result = service.search(query, limit=8)
        if not result.get("ok"):
            console.print(f"[red]Memory search failed: {result.get('error') or result}[/red]")
            return
        rows = result.get("results", [])
        if not rows:
            console.print("[dim]No Graphiti memories found.[/dim]")
            return
        table = Table(title="Graphiti Memory")
        table.add_column("ID")
        table.add_column("Memory")
        for item in rows[:8]:
            table.add_row(str(item.get("id") or item.get("uuid") or ""), str(item.get("text") or item.get("fact") or item)[:240])
        console.print(table)
        return
    if subcommand == "forget":
        if not argument:
            console.print("[red]Usage: /memory forget <memory-id-or-query>[/red]")
            return
        result = service.forget(argument)
        console.print("[green]Forget request completed.[/green]" if result.get("ok") else f"[yellow]Forget request not completed: {result.get('error') or result}[/yellow]")
        return
    if subcommand == "summarize-session":
        if not argument:
            console.print("[red]Usage: /memory summarize-session <session-id>[/red]")
            return
        result = service.summarize_session(argument)
        console.print("[green]Session summary stored.[/green]" if result.get("ok") else f"[yellow]Session summary not stored: {result.get('error') or result}[/yellow]")
        return
    console.print("[red]Usage: /memory status|setup|repair|remember|search|recent|forget|summarize-session[/red]")


def _record_task_memory(
    workspace: Path,
    text: str,
    kind: str = "task_summary",
    session_logger: SessionLogger | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    try:
        result = MemoryService(workspace).remember(text, kind, metadata)
    except Exception as exc:
        _log_event(session_logger, "memory.write_failed", {"error": str(exc)}, "Graphiti memory write failed", workflow_id="memory")
        return
    _log_event(
        session_logger,
        "memory.write",
        {"ok": bool(result.get("ok")), "kind": kind, "skipped": bool(result.get("skipped")), "deduped": bool(result.get("deduped"))},
        "Graphiti memory write evaluated",
        workflow_id="memory",
    )
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


def _trace_config_path(workspace: Path) -> Path:
    return Sandbox(workspace).validate(Path(".shamsu") / "trace.json")


def _trace_mode(workspace: Path) -> str:
    path = _trace_config_path(workspace)
    if not path.exists():
        return "normal"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "normal"
    mode = str(data.get("mode", "normal")).lower()
    return mode if mode in {"quiet", "normal", "verbose"} else "normal"


def _set_trace_mode(workspace: Path, mode: str) -> None:
    path = _trace_config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": mode}, indent=2), encoding="utf-8")


def _handle_trace(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=1)
    command = parts[1].strip().lower() if len(parts) > 1 else "status"
    if command == "on":
        _set_trace_mode(workspace, "normal")
        console.print("[green]Trace mode is on.[/green]")
        return
    if command == "off":
        _set_trace_mode(workspace, "quiet")
        console.print("[yellow]Trace mode is quiet.[/yellow]")
        return
    if command == "verbose":
        _set_trace_mode(workspace, "verbose")
        console.print("[green]Trace mode is verbose.[/green]")
        return
    if command == "status":
        console.print(f"Trace mode: [bold]{_trace_mode(workspace)}[/bold]")
        return
    console.print("[red]Usage: trace status|on|off|verbose[/red]")


_ROOT_CAUSE_EXPLANATIONS = {
    "missing_export": "an import/export mismatch was found; these are treated as the root cause because they typically cascade into many downstream type/symbol errors.",
    "import_export_mismatch": "an import/export mismatch was found; these are treated as the root cause because they typically cascade into many downstream type/symbol errors.",
    "runtime_missing_export": "a browser/runtime module failed to provide an expected export; treated as root cause ahead of any downstream errors it causes.",
    "module_not_found": "a module could not be resolved at all; nothing downstream can be trusted until this resolves, so it is treated as root cause.",
    "syntax_error": "a syntax error was found; syntax errors are prioritized ahead of type errors because the file cannot be parsed correctly until they're fixed.",
    "type_error": "a type error was found with no higher-priority (syntax/import) error present.",
    "test_failure": "a test failure was found with no higher-priority compiler/import error present.",
    "exception": "an unhandled exception's final frame was identified as the most specific failure point.",
}


def _handle_diagnostics(user_input: str, workspace: Path, console: Console) -> None:
    _, _, rest = user_input.partition(" ")
    parts = rest.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else ""
    ws = DiagnosticsWorkspace(workspace)

    if subcommand == "setup":
        result = diagnostics_setup.setup(workspace)
        console.print("[green]Diagnostics setup complete.[/green]" if result.get("ok") else f"[yellow]Diagnostics setup finished with issues: {result}[/yellow]")
        return
    if subcommand == "repair":
        result = diagnostics_doctor.repair(workspace)
        console.print(result.get("manual_steps", ""))
        return
    if subcommand == "status":
        payload = diagnostics_doctor.check(workspace)
        console.print(diagnostics_doctor.format_report(payload))
        return
    if subcommand in {"last", "parse", "explain", "sources"}:
        packet = ws.last_packet()
        if not packet:
            console.print("[yellow]No ErrorPacket recorded yet. Run a command first (e.g. a build/test).[/yellow]")
            return
        if subcommand == "last":
            console.print(f"[bold]{packet.get('summary', '')}[/bold]")
            console.print(f"Command: {packet.get('command', '')} (exit {packet.get('exit_code')})")
            for record in packet.get("root_diagnostics", []):
                location = f"{record.get('file', '')}:{record.get('line', '')}" if record.get("file") else ""
                console.print(f"- [{record.get('category')}] {record.get('code', '')} {location} {record.get('message', '')}".strip())
            if packet.get("target_files"):
                console.print("Target files: " + ", ".join(packet["target_files"]))
            for snippet in packet.get("recommended_snippets", []):
                console.print(f"Recommended snippet: {snippet['file']} lines {snippet['line_start']}-{snippet['line_end']} ({snippet['reason']})")
            return
        if subcommand == "sources":
            console.print("Parser chain: " + (", ".join(packet.get("parser_chain", [])) or "none (no diagnostics extracted)"))
            return
        if subcommand == "explain":
            root = packet.get("root_diagnostics", [])
            if not root:
                console.print("No root diagnostic was selected (command succeeded or nothing was extracted).")
                return
            category = root[0].get("category", "")
            reason = _ROOT_CAUSE_EXPLANATIONS.get(category, "no higher-priority category matched, so this was the first diagnostic after deduping/grouping.")
            console.print(f"Root cause selection: {reason}")
            console.print(f"Diagnostic: [{category}] {root[0].get('code', '')} {root[0].get('message', '')}")
            return
        if subcommand == "parse":
            reparsed = _reparse_last_command(workspace, ws)
            if reparsed is None:
                console.print("[yellow]No recent command output found in session logs to re-parse.[/yellow]")
                return
            console.print(f"[green]Re-parsed.[/green] {reparsed.summary}")
            return
    console.print("[red]Usage: /diagnostics setup|repair|status|last|parse|explain|sources[/red]")


def _reparse_last_command(workspace: Path, ws: "DiagnosticsWorkspace"):
    raw_log_path = Path((ws.last_packet() or {}).get("raw_log_path", ""))
    if not raw_log_path.is_file():
        return None
    command_event = None
    for line in reversed(raw_log_path.read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") in {"command.finished", "command.failed"}:
            command_event = event
            break
    if not command_event:
        return None
    payload = command_event.get("payload", {})
    digest = DiagnosticDigest(workspace, memory_adapter=CodebaseMemoryAdapter())
    packet = digest.run(
        payload.get("command", ""),
        workspace,
        payload.get("exit_code", 0),
        payload.get("stdout", ""),
        payload.get("stderr", ""),
        raw_log_path=str(raw_log_path),
    )
    ws.save_packet(packet.to_dict())
    return packet


def _handle_patch(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    _, _, rest = user_input.partition(" ")
    parts = rest.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else "status"
    argument = parts[1].strip() if len(parts) > 1 else ""

    engine = PatchEngine(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
    )

    if subcommand == "status":
        last = engine.journal.last()
        trash_count = len(engine.trash.list_entries())
        console.print(f"git apply available: {patch_git_apply.available(workspace)}")
        console.print(f"Trashed file(s): {trash_count}")
        if last:
            console.print(f"Last transaction: {last['transaction_id']} ({last['status']}) - {last['reason']}")
        else:
            console.print("[dim]No transactions recorded yet.[/dim]")
        return

    if subcommand == "preview":
        if not argument:
            console.print("[red]Usage: /patch preview <path-to-diff-or-change-request-json>[/red]")
            return
        _patch_preview(argument, workspace, console, engine)
        return

    if subcommand == "apply":
        if not argument:
            console.print("[red]Usage: /patch apply <path-to-change-request-json>[/red]")
            return
        _patch_apply(argument, workspace, console, engine)
        return

    if subcommand == "rollback":
        if not argument:
            console.print("[red]Usage: /patch rollback <transaction-id>[/red]")
            return
        request = ApprovalRequest(
            action_type="file_delete",
            description=f"Roll back transaction {argument}.",
            risk_level="high",
            working_dir=str(workspace),
            reason="Rollback restores backed-up files, overwriting current content.",
        )
        if not engine.approval_manager.ask(request):
            console.print("[yellow]Rollback denied.[/yellow]")
            return
        ok, message = engine.rollback_transaction(argument)
        console.print(f"[green]{message}[/green]" if ok else f"[red]{message}[/red]")
        return

    if subcommand == "journal":
        entries = engine.journal.entries()
        if not entries:
            console.print("[dim]No transactions recorded yet.[/dim]")
            return
        table = Table(title="Patch Journal")
        table.add_column("Transaction")
        table.add_column("Status")
        table.add_column("Files")
        table.add_column("Verification")
        table.add_column("Reason")
        for entry in reversed(entries):
            verification = entry.get("verification") or {}
            verification_text = (
                f"exit {verification.get('exit_code')}" if verification.get("ran") else "not run"
            )
            table.add_row(
                entry.get("transaction_id", ""),
                entry.get("status", ""),
                str(len(entry.get("touched_files", []))),
                verification_text,
                entry.get("reason", ""),
            )
        console.print(table)
        return

    if subcommand == "last":
        last = engine.journal.last()
        if not last:
            console.print("[dim]No transactions recorded yet.[/dim]")
            return
        console.print(f"[bold]{last['transaction_id']}[/bold] ({last['status']})")
        console.print(f"Reason: {last.get('reason', '')}")
        console.print("Files: " + (", ".join(last.get("touched_files", [])) or "none"))
        verification = last.get("verification") or {}
        if verification.get("ran"):
            console.print(f"Verification: `{verification.get('command')}` exit {verification.get('exit_code')}")
        return

    if subcommand == "diff":
        if not argument:
            console.print("[red]Usage: /patch diff <transaction-id>[/red]")
            return
        patch_text = engine.transactions.load_patch(argument)
        if not patch_text.strip():
            console.print(f"[yellow]No stored diff for transaction {argument}.[/yellow]")
            return
        try:
            print_diff_preview(patch_text, console=console, sandbox=engine.sandbox)
        except Exception:
            console.print(patch_text)
        return

    if subcommand == "trash":
        entries = engine.trash.list_entries()
        if not entries:
            console.print("[dim]Trash is empty.[/dim]")
            return
        table = Table(title="Trash")
        table.add_column("Transaction")
        table.add_column("Path")
        table.add_column("Size")
        for item in entries:
            table.add_row(item.transaction_id, item.relative_path, str(item.size_bytes))
        console.print(table)
        return

    if subcommand == "clean-trash":
        entries = engine.trash.list_entries()
        if not entries:
            console.print("[dim]Trash is already empty.[/dim]")
            return
        request = ApprovalRequest(
            action_type="file_delete",
            description=f"Permanently delete {len(entries)} trashed file(s).",
            risk_level="high",
            working_dir=str(workspace),
            reason="Clean-trash permanently removes files SHAMSU previously moved to .shamsu/trash.",
        )
        if not engine.approval_manager.ask(request):
            console.print("[yellow]Clean-trash denied.[/yellow]")
            return
        removed = engine.trash.clean()
        console.print(f"[green]Permanently removed {removed} trashed file(s).[/green]")
        return

    console.print(
        "[red]Usage: /patch status|preview|apply|rollback|journal|last|diff|trash|clean-trash[/red]"
    )


def _patch_preview(argument: str, workspace: Path, console: Console, engine: "PatchEngine") -> None:
    try:
        target = Sandbox(workspace).validate(argument)
    except SecurityError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    if not target.is_file():
        console.print(f"[red]File not found: {argument}[/red]")
        return
    raw = target.read_text(encoding="utf-8")
    payload = None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict) and "change_plan" in payload:
        try:
            change_plan = patch_types.parse_change_plan(payload["change_plan"])
        except patch_types.ChangeRequestError as exc:
            console.print(f"[red]Invalid change_plan: {exc}[/red]")
            return
        console.print(f"[bold]Reason:[/bold] {change_plan.reason}")
        console.print(f"Destructive: {change_plan.destructive}")
        for op in change_plan.operations:
            console.print(f"- {op.op}: {op.path}" + (f" -> {op.dest_path}" if op.dest_path else ""))
        patch_text = payload.get("patch", "")
        if patch_text.strip():
            print_diff_preview(patch_text, console=console, sandbox=engine.sandbox)
        return
    print_diff_preview(raw, console=console, sandbox=engine.sandbox)


def _patch_apply(argument: str, workspace: Path, console: Console, engine: "PatchEngine") -> None:
    try:
        target = Sandbox(workspace).validate(argument)
    except SecurityError as exc:
        console.print(f"[red]{exc}[/red]")
        return
    if not target.is_file():
        console.print(f"[red]File not found: {argument}[/red]")
        return
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        console.print(f"[red]Change request must be JSON matching the change_plan/patch contract: {exc}[/red]")
        return
    result = engine.execute_change_request(payload)
    if result.ok:
        body = "\n".join(f"- {path}" for path in result.touched_files) or "No files reported."
        if result.verification and result.verification.ran:
            body += f"\n\nVerification: `{result.verification.command}` exit {result.verification.exit_code}"
        console.print(Panel(body, title=f"Applied ({result.transaction_id})", border_style="green"))
    else:
        detail = result.error
        if result.verification and result.verification.ran and not result.verification.passed:
            detail += f"\n\nVerification: `{result.verification.command}` exit {result.verification.exit_code}"
            if result.verification.stalled:
                detail += "\n[yellow]This failure signature matches the previous run - repeated failure, not retrying blindly.[/yellow]"
        console.print(Panel(detail, title=f"Patch Rejected ({result.transaction_id or 'no transaction'})", border_style="red"))


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


def _handle_models(
    user_input: str,
    console: Console,
    workspace: Path | None = None,
) -> None:
    parts = user_input.split(maxsplit=1)
    command = parts[1].strip().lower() if len(parts) > 1 else "status"
    if command == "status":
        _print_runtime_status(console)
        return
    if command == "tier" or command.startswith("tier "):
        tier_arg = command[len("tier"):].strip()
        _handle_models_tier(tier_arg, console, workspace)
        return
    if command == "pull":
        status = collect_status()
        if not status.ollama_found:
            console.print(
                "[red]Ollama was not found. Run `models repair` after installing Ollama.[/red]"
            )
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
    console.print("[red]Usage: models status|pull|repair|tier[/red]")


def _handle_models_tier(tier_arg: str, console: Console, workspace: Path | None) -> None:
    if not tier_arg:
        current = active_tier()
        console.print(f"[cyan]Active tier:[/cyan] {current.value}")
        for tier in ModelTier:
            specs = tier_model_specs(tier)
            marker = "*" if tier is current else " "
            models = ", ".join(spec.name for spec in specs)
            console.print(f"  {marker} {tier.value:8} {models}")
        console.print("Usage: /models tier light|default|heavy")
        return
    try:
        requested = ModelTier(tier_arg)
    except ValueError:
        console.print(
            f"[red]Unknown tier: {tier_arg}. Choose one of: "
            + ", ".join(tier.value for tier in ModelTier)
            + "[/red]"
        )
        return
    if workspace is None:
        console.print("[red]No workspace available to persist the tier choice.[/red]")
        return
    set_model_tier(workspace, requested)
    console.print(f"[green]Switched to {requested.value} tier.[/green]")
    _pull_missing_models_for_active_tier(console)


def _pull_missing_models_for_active_tier(console: Console) -> None:
    """Download whatever the currently active tier is missing, starting the
    local Ollama server first if needed. Shared by `/models tier <name>` and
    the first-run tier prompt - typing/answering either is the consent to
    download directly, no second approval gate."""
    status = collect_status()
    if not status.ollama_found:
        console.print("[yellow]Ollama was not found. Run `models repair` once Ollama is installed.[/yellow]")
        return
    if not status.server_running:
        console.print("[yellow]Starting local Ollama...[/yellow]")
        serve_pid = start_ollama(Path(status.ollama_path))
        if serve_pid:
            claim_ollama_ownership(serve_pid)
        wait_until_running()
        status = collect_status(Path(status.ollama_path))
    if not status.missing_models:
        console.print("[green]All models for this tier are already installed.[/green]")
        _print_runtime_status(console, status=status)
        return
    console.print(
        f"[cyan]Downloading {status.tier} tier model(s):[/cyan] " + ", ".join(status.missing_models)
    )
    results = _pull_models_with_progress(Path(status.ollama_path), status.missing_models, console)
    _print_model_pull_results(results, console)
    status = collect_status(Path(status.ollama_path))
    _print_runtime_status(console, status=status)


def _maybe_prompt_first_run_tier(workspace: Path, console: Console) -> None:
    """Ask which model tier to use the first time SHAMSU runs in a workspace,
    then download that tier's models with visible progress - installers never
    download models themselves; this is the one proactive download, and only
    once per workspace. Skipped entirely once a tier has been chosen before,
    or when SHAMSU_MODEL_TIER already pins one explicitly."""
    if tier_ever_configured(workspace):
        return
    if os.environ.get("SHAMSU_MODEL_TIER", "").strip():
        return
    tier_name = ask_tier_choice(console)
    tier = ModelTier(tier_name) if tier_name else DEFAULT_TIER
    set_model_tier(workspace, tier)
    console.print(f"[green]Using {tier.value} tier.[/green] (change anytime with /models tier)")
    _pull_missing_models_for_active_tier(console)


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
    table.add_row("Tier", status.tier)
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
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
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
    if _looks_like_dev_server_prompt(effective_input):
        _handle_dev_server(effective_input, workspace, console, session_logger=session_logger)
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
                    "[yellow]Codebase-Memory MCP is not ready. Run `/abstract setup` for project-specific QA.[/yellow]"
                )
                console.print(
                    "I can still do general local chat without it, but for this workspace-specific question "
                    "I need Codebase-Memory MCP set up first."
                )
            return
        console.print(
            "[yellow]Codebase-Memory MCP is not ready. Run `/abstract setup` for project-specific QA.[/yellow]"
        )
    decision = await _route_prompt(effective_input, llm)
    _print_decision(decision, console)
    task_plan = build_task_plan(decision, effective_input)
    harness_input = append_task_handoff(effective_input, task_plan, agent_context)

    try:
        _log_event(
            session_logger,
            "workflow.started",
            {"intent": decision.intent, "prompt": user_input, "effective_prompt": effective_input},
            f"Workflow started: {decision.intent}",
            workflow_id=decision.intent,
        )
        _log_event(
            session_logger,
            "workflow.plan",
            plan_log_payload(task_plan),
            f"Task harness selected {task_plan.mode} mode",
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
                    harness_input,
                    workspace,
                    console,
                    session_logger=session_logger,
                    auto_approve=is_long_running_enabled(workspace),
                )
            else:
                await _run_qa(effective_input, workspace, console, llm, extra_context=agent_context, session_logger=session_logger, thinking_status=thinking_status)
        elif decision.intent == "code_edit":
            await _run_code_edit(harness_input, workspace, search, console, llm, session_logger)
        elif decision.intent == "bug_fix":
            await _run_bug_fix(harness_input, workspace, search, console, llm, session_logger)
        elif decision.intent == "audit":
            await _run_audit(harness_input, search, console, llm)
        elif decision.intent == "test_gen":
            await _run_test_generation(harness_input, workspace, search, console, llm, session_logger)
        elif decision.intent == "doc_gen":
            await _run_docs(harness_input, workspace, search, console, llm, session_logger)
        else:
            console.print("[yellow]Project generation is not wired into this CLI yet.[/yellow]")
        _log_event(
            session_logger,
            "workflow.finished",
            {"intent": decision.intent},
            f"Workflow finished: {decision.intent}",
            workflow_id=decision.intent,
        )
        memory_kind = "bug_lesson" if decision.intent == "bug_fix" else "task_summary"
        _record_task_memory(
            workspace,
            f"Task summary: {decision.intent} completed for request: {effective_input[:700]}",
            memory_kind,
            session_logger,
            {"intent": decision.intent},
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
            _handle_models("models repair", console, workspace)
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
        decision = await llm.route(user_input, "Indexed workspace selected in SHAMSU CLI.")
        if decision.intent in {"qa", "explain"} and decision.confidence < 0.6 and (
            _looks_like_command_like_prompt(user_input) or _looks_like_trouble_report(user_input)
        ):
            return _keyword_decision(user_input)
        return decision
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
    elif _looks_like_trouble_report(user_input) or any(word in text for word in ("traceback", "exception", "error:", "failing", "fix ", "repair")):
        intent = "bug_fix"
    elif any(word in text for word in ("write tests", "generate tests", "test for", "pytest", "run tests", "test ")):
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
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
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
    "resolve", "correct", "patch", "run", "start", "stop", "restart",
    "execute", "launch", "open", "deploy", "compile", "rebuild",
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
    "does not provide an export named", "no exported member", "stack trace", " at ",
    "ts2305", "ts2724", "ts2339", "ts2307", "ts(", "vite:", "[plugin:",
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


def _looks_like_dev_server_prompt(user_input: str) -> bool:
    normalized = _normalize_command_input(user_input).strip()
    text = normalized.lower()
    if is_dev_server_command(normalized):
        return True
    return any(
        phrase in text
        for phrase in (
            "run dev",
            "start dev",
            "dev server",
            "start server",
            "run the code in dev",
            "open dev",
            "launch dev",
        )
    )


def _looks_like_command_like_prompt(user_input: str) -> bool:
    text = user_input.lower()
    return _looks_like_dev_server_prompt(user_input) or any(
        phrase in text
        for phrase in (
            "run ",
            "repair",
            "fix",
            "compile",
            "build",
            "test",
            "one part at a time",
            "start server",
            "dev server",
        )
    )


def _extract_dev_command(user_input: str, workspace: Path) -> str:
    normalized = _normalize_command_input(user_input).strip()
    if is_dev_server_command(normalized):
        return normalized
    return infer_dev_command(workspace)


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

    url = "http://localhost:5173"
    if (workspace / "client").is_dir() and (workspace / "server").is_dir():
        # Monorepo: one `npm run dev` starts both the server and the client
        # (concurrently), in one terminal window teed to a log.
        dev = _start_background_command("npm run dev", workspace, log_dir / "dev.log", visible_console=True)
        settle_log = log_dir / "dev.log"
        console.print(
            Panel(
                f"Game dev server started in a terminal window you can watch.\n\n"
                f"Open: {url}\n\n"
                f"Dev PID: {dev.pid}\n"
                f"Logs: {log_dir}",
                title="Game Running",
                border_style="green",
            )
        )
        _log_event(
            session_logger, "project.preview.started",
            {"url": url, "dev_pid": dev.pid, "logs": str(log_dir)},
            f"Started game preview at {url}", workflow_id="game-preview",
        )
    else:
        relay = _start_background_command("npm run dev:relay", workspace, log_dir / "relay.log", visible_console=True)
        vite = _start_background_command("npm run dev", workspace, log_dir / "vite.log", visible_console=True)
        settle_log = log_dir / "vite.log"
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
            session_logger, "project.preview.started",
            {"url": url, "vite_pid": vite.pid, "relay_pid": relay.pid, "logs": str(log_dir)},
            f"Started game preview at {url}", workflow_id="game-preview",
        )

    # Read the dev-server log back and, if it failed to boot cleanly, fix errors.
    console.print("[dim]Watching the dev server start up for errors...[/dim]")
    errors = await _await_dev_server_settle(settle_log)
    if errors:
        console.print(Panel(errors, title="Vite reported errors on startup", border_style="yellow"))
        console.print("[cyan]Fixing the dev-server errors...[/cyan]")
        await _run_agent_chat(
            _build_frontend_repair_request(errors, workspace, Path(".")),
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


def _handle_dev_server(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    command = _extract_dev_command(user_input, workspace)
    progress = ProgressReporter(console, session_logger, title="Dev server", max_steps=3)
    progress.step("Validating dev-server command")
    manager = DevServerManager(
        workspace,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
        session_logger=session_logger,
    )
    progress.step(f"Launching dev server in a new terminal: {command}")
    result = manager.start(command)
    if not result.launched and not result.duplicate:
        progress.failed(result.message or "Dev server was not launched")
        console.print(Panel(result.message, title="Dev Server", border_style="red"))
        return
    if result.duplicate:
        progress.done("Matching dev server already appears to be running")
    else:
        progress.done("Dev server launched")
    console.print(
        Panel(
            "\n".join(
                [
                    "Launching dev server in a new CMD/terminal window...",
                    "",
                    f"Command:\n{result.command}",
                    "",
                    f"Likely URL:\n{result.url}",
                    "",
                    "Control returned to SHAMSU. I can keep working while the dev server runs.",
                ]
            ),
            title="Dev Server",
            border_style="green" if result.launched or result.duplicate else "yellow",
        )
    )


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


def _typecheck_command(workspace: Path) -> str | None:
    """The command that compile-checks this project, or None if there is nothing
    to check. A client/ + server/ monorepo builds both packages; a single-package
    TS project runs tsc directly."""
    if (workspace / "client" / "tsconfig.json").exists() and (workspace / "server").is_dir():
        return "npm run build"
    if (workspace / "tsconfig.json").exists():
        return "npx --no-install tsc --noEmit"
    return None


def _run_frontend_typecheck(workspace: Path) -> tuple[bool, str]:
    """Compile-check the project. Returns (ok, output). No TS project -> ok."""
    command = _typecheck_command(workspace)
    if command is None:
        return True, ""
    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=600,
            creationflags=(subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run the build: {exc}"
    return result.returncode == 0, ((result.stdout or "") + (result.stderr or "")).strip()


_TS_ERROR_LOCATION_RE = re.compile(
    r"(?P<path>[\w./\\-]+\.(?:ts|tsx|js|jsx))\((?P<line>\d+),(?P<col>\d+)\)"
)


def _extract_error_snippets(
    workspace: Path, errors: str, context_lines: int = 6, max_snippets: int = 6
) -> str:
    """Read the lines around each tsc error location and return them as a
    labelled block. Small models frequently guess at broken syntax when only
    given the error message; showing the actual surrounding code lets them fix
    it instead of guessing."""
    seen: set[tuple[str, int]] = set()
    snippets: list[str] = []
    for match in _TS_ERROR_LOCATION_RE.finditer(errors):
        path_text = match.group("path")
        line_no = int(match.group("line"))
        key = (path_text, line_no)
        if key in seen:
            continue
        seen.add(key)
        try:
            target = (workspace / path_text).resolve()
            target.relative_to(workspace.resolve())
        except (OSError, ValueError):
            continue
        if not target.is_file():
            continue
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        start = max(line_no - context_lines, 1)
        end = min(line_no + context_lines, len(lines))
        numbered = "\n".join(
            f"{n:>5}{'>' if n == line_no else ' '} {lines[n - 1]}"
            for n in range(start, end + 1)
        )
        snippets.append(f"--- {path_text} around line {line_no} ---\n{numbered}")
        if len(snippets) >= max_snippets:
            break
    return "\n\n".join(snippets)


def _build_frontend_repair_request(errors: str, workspace: Path, relative_path: Path) -> str:
    snippets = _extract_error_snippets(workspace, errors)
    snippet_section = (
        f"\nActual code around each error location (fix what's really there, don't guess):\n{snippets}\n"
        if snippets
        else ""
    )
    return (
        "The project does NOT compile. Your ONLY task right now is to make the build pass by fixing "
        "every TypeScript error below. Do NOT add features, do NOT start a new milestone.\n\n"
        "Critical rules:\n"
        "- Read each failing file AND the files that import it before editing.\n"
        "- Never delete or rename an export that another file imports; keep signatures compatible.\n"
        "- Keep the app wired together (screens import game logic; do not orphan modules).\n"
        "- Fix ALL errors, use write_file for each change, then stop.\n\n"
        f"Build output:\n{errors[-4000:]}\n"
        f"{snippet_section}"
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
    if _typecheck_command(workspace) is None:
        # Not a TypeScript project: nothing to compile-gate on.
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
            _build_frontend_repair_request(output, workspace, relative_path),
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
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
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
        "client/package.json",
        "client/src/App.tsx",
        "client/src/game/entities.ts",
        "client/src/game/rules.ts",
        "client/src/ui/Hud.tsx",
        "server/src/index.ts",
        "server/src/db.ts",
    ]
    return all((workspace / path).exists() for path in required)


def _build_multiplayer_game_request(parsed, relative_path: Path) -> str:
    return (
        "You are now past scaffold setup. The multiplayer-game template is a working monorepo "
        "(client/ + server/) that already builds, runs, and passes its Definition of Done with a "
        "placeholder game. Do real coding work now.\n\n"
        "Rules:\n"
        "- Read the PRD and the existing template files before writing.\n"
        "- Do not ask the user for starter files. Menus, lobby, Colyseus networking, the game loop, "
        "Rapier physics, the HUD frame, and the SQLite leaderboard are already built - do not replace them.\n"
        "- Fill and adapt ONLY the marked holes: client/src/game/entities.ts (// HOLE:entity.player, "
        "// HOLE:entity.world), client/src/game/rules.ts (// HOLE:rule.update, // HOLE:rule.win, "
        "// HOLE:rule.score), and client/src/ui/Hud.tsx (// HOLE:ui.hud).\n"
        "- Never delete or rename an export another file imports; keep the project compiling.\n"
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
    trace_mode = _trace_mode(workspace)
    progress = ProgressReporter(
        None if trace_mode == "quiet" else console,
        session_logger,
        title="Agent",
        max_steps=50 if long_running else 20,
        verbose=trace_mode == "verbose",
    )
    progress.step("Waiting for model response")

    def on_activity(msg: str) -> None:
        activities.append(msg)
        progress.step(msg)

    result = await AgentChatLoop(
        workspace,
        session_logger=session_logger,
        tools=tools,
        long_running=long_running,
        on_activity=on_activity,
        progress=progress,
    ).run(user_input)
    body = result.final.strip() or "No response returned."
    if result.stopped:
        progress.warning("Agent stopped before completing all requested work")
    else:
        progress.done("Agent finished")
    console.print(Panel(_agent_display_summary(body, activities), title="Agent"))
    _log_assistant_message(session_logger, body, workflow_id="agent-chat")
    _record_task_memory(
        workspace,
        f"Task summary: agent-chat completed for request: {user_input[:500]}",
        "task_summary",
        session_logger,
        {"workflow": "agent-chat"},
    )


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
        "Run `/abstract status` for code-memory health, `models status` for local AI status, "
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
    _print_patch_result(
        "Bug Fix",
        result.applied,
        result.changed_files,
        result.error,
        console,
        verification_status=getattr(result, "verification_status", ""),
    )


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
    _print_patch_result(
        "Test Generation",
        result.applied,
        result.changed_files,
        result.error,
        console,
    )


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
    _print_patch_result(
        "Documentation",
        result.applied,
        result.changed_files,
        result.error,
        console,
    )


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
    verification_status: str = "",
) -> None:
    if applied:
        files = "\n".join(f"- {path}" for path in changed_files) or "No files reported."
        body = files
        if verification_status:
            body = f"Applied changes:\n{files}\n\nVerified results:\n{verification_status}"
        console.print(Panel(body, title=f"{title} Applied", border_style="green"))
        return
    console.print(
        Panel(error or "No changes applied.", title=f"{title} Not Applied", border_style="yellow")
    )


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
    if normalized.startswith("abstract "):
        return "[dim]Querying Codebase-Memory MCP...[/dim]"
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
    tier = active_tier().value
    autonomy = "on" if is_long_running_enabled(workspace) else "off"
    runtime = status_text(collect_status())
    body = Text()
    body.append("SHAMSU v0.3.0", style="bold")
    body.append("  |  Local AI coding agent\n", style="dim")
    body.append("Workspace: ", style="dim")
    body.append(f"{workspace}\n")
    body.append("Model: ", style="dim")
    body.append(f"{model}", style="cyan")
    body.append(f"  ({tier} tier)", style="dim")
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
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        sys.exit(2)

    # Resolve the active model tier (env var > persisted workspace choice >
    # default) before anything reads model_for_role(), including the banner.
    initialize_model_tier(workspace)
    # First run in this workspace: ask which tier, then download it here (with
    # progress) rather than at install time or silently mid-conversation.
    _maybe_prompt_first_run_tier(workspace, console)

    # Track this session so the last one to exit can free SHAMSU's Ollama
    # footprint (stop a SHAMSU-started server, or unload SHAMSU's models - incl.
    # the keep_alive=-1 router - from a shared/tray-app server). Best-effort.
    session_pid = register_session()
    atexit.register(shutdown_if_last_session, session_pid)

    _print_startup_banner(workspace, console)
    _ensure_graphiti_ready_at_startup(workspace, console)
    _ensure_code_memory_ready_at_startup(workspace, console)
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
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
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
        if not _memory_command_allowed(lowered_input):
            memory_gate = MemoryService(workspace).ensure_ready()
            if not memory_gate.allowed:
                console.print(Panel(memory_gate.reason or REQUIRED_MEMORY_MESSAGE, title="Graphiti Memory Required", border_style="red"))
                continue
        if lowered_input in {"exit", "quit"}:
            print("Goodbye.")
            break
        if lowered_input == "help":
            _print_help(console)
            continue
        if lowered_input == "doctor":
            _handle_doctor(workspace, console)
            continue
        if lowered_input.startswith("memory"):
            _handle_memory(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("abstract"):
            _handle_abstract(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("diagnostics"):
            _handle_diagnostics(normalized_input, workspace, console)
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
            _handle_models(normalized_input, console, workspace)
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
        if lowered_input.startswith("trace"):
            _handle_trace(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("diagnostics"):
            _handle_diagnostics(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("patch"):
            _handle_patch(normalized_input, workspace, console, session_logger=session_logger)
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
