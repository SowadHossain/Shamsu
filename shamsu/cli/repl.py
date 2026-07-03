"""
Minimal REPL shell.

The selected workspace is the sandbox boundary for project reads and indexes.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import shlex
import sqlite3
import sys
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from shamsu.agents.audit_workflow import AuditWorkflow
from shamsu.agents.bugfix_workflow import BugFixWorkflow
from shamsu.agents.code_edit_workflow import CodeEditWorkflow
from shamsu.agents.doc_workflow import DocumentationWorkflow
from shamsu.agents.error_feedback_loop import ErrorFeedbackLoop
from shamsu.agents.full_pipeline import FullDjangoPipeline, FullPipelineResult
from shamsu.agents.orchestrator import AgentOrchestrator
from shamsu.agents.qa_workflow import QAWorkflow
from shamsu.agents.test_generation_workflow import TestGenerationWorkflow
from shamsu.core.coordinator import Coordinator
from shamsu.indexer.walker import FileWalker
from shamsu.llm.manager import LLMManager
from shamsu.prd.input import PRDParseError, parse_prd_file
from shamsu.prd.project import build_project_spec
from shamsu.prd.state import create_generation_state, save_generation_state
from shamsu.retriever.search import SearchAgent
from shamsu.runtime.ollama import (
    collect_status,
    pull_model_streaming,
    repair_runtime,
    start_ollama,
    status_text,
    wait_until_running,
)
from shamsu.safety.approval import ask_approval
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.patch.engine import PatchEngine
from shamsu.session.manager import SessionLogger, SessionManager
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import WebFetchResult, WebSearchResult, WebTool
from shamsu.tools.django import DjangoSetupResult, DjangoSetupRunner, DjangoTestRunner
from shamsu.tools.executor import CommandRunner
from shamsu.tools.git import GitTool
from shamsu.tools.workspace import WorkspaceTool
from shamsu.types import ApprovalRequest, ContextPack, ProjectSpec, RoutingDecision, SearchResult

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


SYSTEM_COMMANDS = (
    "/help",
    "/index",
    "/status",
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
                    "  change the CLI banner text",
                    "  fix this traceback: <paste error>",
                    "  write tests for the parser",
                    "  audit this project for security issues",
                    "  update the README",
                    "",
                    "Commands:",
                    "  /index                    Index the current workspace",
                    "  /status                   Show index counts",
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
    search, _uses_real_index = _build_search_agent(workspace)
    result = await FullDjangoPipeline(
        workspace,
        search=search,
        session_logger=session_logger,
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
    table = Table(title="Full Django Pipeline")
    table.add_column("Step")
    table.add_column("Status")
    table.add_row("Project", result.project.project_name if result.project else "not built")
    table.add_row("Target", str(result.target_dir))
    table.add_row("Files", str(len(result.written_files or [])))
    table.add_row("Diagnostics", str(len(result.diagnostics or [])))
    if result.setup_result:
        table.add_row("Setup", "ok" if result.setup_result.ok else "failed")
    if result.test_result:
        table.add_row("Tests", f"{result.test_result.passed} passed, {result.test_result.failed} failed")
    table.add_row("Result", "success" if result.success else "failed")
    console.print(table)
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


def _handle_models(
    user_input: str,
    console: Console,
    approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
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
            start_ollama(Path(status.ollama_path))
            wait_until_running()
            status = collect_status(Path(status.ollama_path))
        if not status.missing_models:
            console.print("[green]All required local models are installed.[/green]")
            return
        if not _approve_model_download(status.missing_models, console, approval_func):
            console.print("[yellow]Model download cancelled.[/yellow]")
            return
        results = _pull_models_with_progress(Path(status.ollama_path), status.missing_models, console)
        _print_model_pull_results(results, console)
        _print_runtime_status(console)
        return
    if command == "repair":
        status = repair_runtime(pull_models=False)
        if status.ollama_found and not status.server_running:
            console.print("[yellow]Starting local Ollama...[/yellow]")
            start_ollama(Path(status.ollama_path))
            wait_until_running()
            status = collect_status(Path(status.ollama_path))
        if status.ollama_found and status.server_running and status.missing_models:
            if not _approve_model_download(status.missing_models, console, approval_func):
                console.print("[yellow]Model download cancelled.[/yellow]")
                _print_runtime_status(console, status=status)
                return
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


def _approve_model_download(
    missing_models: list[str],
    _console: Console,
    approval_func: Callable[[ApprovalRequest], bool],
) -> bool:
    return approval_func(
        ApprovalRequest(
            action_type="run_command",
            description=f"Download {len(missing_models)} missing local Ollama model(s).",
            risk_level="medium",
            preview="\n".join(f"- {model}" for model in missing_models),
            reason=(
                "SHAMSU needs these local models for offline inference. "
                "Re-running `models pull` resumes partial Ollama downloads."
            ),
        )
    )


def _pull_models_with_progress(
    ollama_path: Path,
    models: list[str],
    console: Console,
) -> dict[str, int]:
    results: dict[str, int] = {}
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as progress:
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
    if _is_casual_prompt(user_input):
        _print_ready_message(workspace, console)
        _log_assistant_message(session_logger, "SHAMSU is ready.", workflow_id="agent")
        return
    if _looks_like_workspace_location_prompt(effective_input):
        _print_workspace_location(workspace, console)
        return
    if _looks_like_workspace_files_prompt(effective_input):
        _print_workspace_files(workspace, console)
        return
    if _looks_like_workspace_prd_request(effective_input):
        _handle_workspace_prd_request(workspace, console)
        return
    if _looks_like_browser_needed_prompt(effective_input):
        await _run_browser_assist(effective_input, console, llm=LLMManager(session_logger=session_logger), browser_tool=browser_tool)
        return
    if _looks_like_web_needed_prompt(effective_input):
        await _run_web_assist(
            effective_input,
            console,
            llm=LLMManager(session_logger=session_logger),
            web_tool=web_tool,
            session_logger=session_logger,
        )
        return
    if _looks_like_django_generation_request(effective_input):
        generate_command = f"generate-django {_extract_prd_path_from_prompt(effective_input)}"
        _handle_generate_django(generate_command, workspace, console, session_logger=session_logger)
        return
    if _looks_like_prd_plan_request(effective_input):
        plan_command = f"plan-prd {_extract_prd_path_from_prompt(effective_input)}"
        _handle_plan_prd(plan_command, workspace, console, session_logger=session_logger)
        return
    search, uses_real_index = _build_search_agent(workspace)
    llm = LLMManager(session_logger=session_logger)
    if not uses_real_index:
        decision = _keyword_decision(effective_input)
        if decision.intent in {"qa", "explain"}:
            if _is_general_chat_prompt(effective_input):
                await _run_general_chat(effective_input, console, llm, extra_context=agent_context, session_logger=session_logger)
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
            await _run_qa(effective_input, workspace, console, llm, extra_context=agent_context, session_logger=session_logger)
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
    if any(word in text for word in ("package", "api docs", "release notes", "version", "breaking change")) and not _is_project_local_prompt(text):
        return True
    return False


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
        if path.suffix.lower() not in {".md", ".markdown", ".txt", ".pdf"}:
            continue
        if "prd" not in path.name.lower():
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
            "Add a `.md`, `.txt`, or `.pdf` file with `prd` in the name, then ask again or run `/parse-prd <file>`."
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


async def _run_qa(
    user_input: str,
    workspace: Path,
    console: Console,
    llm: LLMManager,
    extra_context: str = "",
    session_logger: SessionLogger | None = None,
) -> None:
    qa_workflow, _uses_real_index = _build_workspace_qa_workflow(workspace)
    request = _append_agent_context(user_input, extra_context)
    result = await Coordinator(llm=llm, qa_workflow=qa_workflow).handle(request)
    if result.answer:
        title = f"Answer ({result.model_used})" if result.model_used else "Answer"
        console.print(Panel(result.answer, title=title))
        _log_assistant_message(session_logger, result.answer, workflow_id="qa")
    elif result.fallback_reason:
        console.print(f"[yellow]{result.fallback_reason}[/yellow]")
    if result.preview and _preview_contains_context(result.preview):
        console.print(Panel(result.preview, title="Context Preview"))


async def _run_general_chat(
    user_input: str,
    console: Console,
    llm: LLMManager,
    extra_context: str = "",
    session_logger: SessionLogger | None = None,
) -> None:
    pack = ContextPack(
        task_id="general-chat",
        step_id=1,
        specialist="qa",
        user_request=user_input,
        prd_context=(
            "No indexed project context is attached. "
            "Answer as a general local assistant. "
            "Do not claim you saw code, tests, or files unless they were actually provided."
            + (f" {extra_context}" if extra_context else "")
        ),
    )
    response = await llm.run_specialist("qa", pack)
    title = f"Chat ({response.model_used})" if response.model_used else "Chat"
    body = response.raw.strip()
    console.print(Panel(body, title=title))
    _log_assistant_message(session_logger, body, workflow_id="general-chat")


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
    for hit in result.hits[:3]:
        fetch = web_tool.fetch(
            hit.url,
            reason="SHAMSU wants to read the matching page to answer your question accurately.",
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
        body = "\n".join(f"- {hit.title}\n  {hit.url}" for hit in result.hits[:5])
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
    _warn_if_dirty_before_edit(workspace, console)
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
    _warn_if_dirty_before_edit(workspace, console)
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
    _warn_if_dirty_before_edit(workspace, console)
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
    search, _uses_real_index = _build_search_agent(workspace)
    llm = LLMManager(session_logger=session_logger)
    result = await ErrorFeedbackLoop(
        workspace,
        search=search,
        llm=llm,
        patch_engine=PatchEngine(workspace, session_logger=session_logger),
        session_logger=session_logger,
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
            completer=SlashCommandCompleter(workspace),
            complete_while_typing=True,
            bottom_toolbar=f"Workspace: {workspace} | /help | /index | /exit",
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
    console.print("[dim]Type a prompt, or `/help` for commands.[/dim]\n")
    web_tool = WebTool(session_logger=session_logger)
    browser_tool = BrowserTool(workspace, session_logger=session_logger)
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
        normalized_input = _normalize_command_input(user_input)
        lowered_input = normalized_input.lower()
        if lowered_input in {"exit", "quit"}:
            print("Goodbye.")
            break
        previous_user_prompt = session_logger.metadata.last_user_prompt
        session_logger.log(
            "user.prompt",
            {"prompt": user_input},
            "User submitted prompt",
            workflow_id="repl",
        )
        if lowered_input == "help":
            _print_help(console)
            continue
        if lowered_input == "index":
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_index(workspace, console)
            continue
        if lowered_input == "status":
            _handle_status(workspace, console)
            continue
        if lowered_input.startswith("search "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_search(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("symbols "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_symbols(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("parse-prd "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_parse_prd(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("plan-prd "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_plan_prd(normalized_input, workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("generate-django "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_generate_django(normalized_input, workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("generate-prd "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                asyncio.run(_handle_generate_prd(normalized_input, workspace, console, session_logger))
            continue
        if lowered_input.startswith("models"):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_models(normalized_input, console)
            continue
        if lowered_input.startswith("web "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_web(normalized_input, console, web_tool, LLMManager(session_logger=session_logger))
            continue
        if lowered_input.startswith("browse "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_browse(normalized_input, console, browser_tool)
            continue
        if lowered_input.startswith("django"):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
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
        if lowered_input == "log" or lowered_input.startswith("log "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_log(normalized_input, session_logger, console)
            continue

        with console.status(_thinking_status_for_input(user_input), spinner="dots"):
            asyncio.run(
                _handle_request(
                    user_input,
                    workspace,
                    console,
                    web_tool,
                    browser_tool,
                    previous_user_prompt=previous_user_prompt,
                    session_logger=session_logger,
                )
            )

    browser_tool.close()


if __name__ == "__main__":
    main()
