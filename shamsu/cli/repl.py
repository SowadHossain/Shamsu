"""
Minimal REPL shell.

The selected workspace is the sandbox boundary for project reads and indexes.
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import atexit
import contextlib
import difflib
import inspect
import itertools
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import sys
import time
import traceback
from dataclasses import replace
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
from shamsu.action_ledger import store as action_ledger_store
from shamsu.action_ledger.config import load_config as load_action_ledger_config
from shamsu.action_ledger.context import clear_current_run, get_current_run, set_current_run
from shamsu.action_ledger.ledger import ActionLedger, start_run
from shamsu.agents.audit_workflow import AuditWorkflow
from shamsu.diagnostics import doctor as diagnostics_doctor
from shamsu.diagnostics import setup as diagnostics_setup
from shamsu.diagnostics.digest import DiagnosticDigest
from shamsu.diagnostics.setup import DiagnosticsWorkspace
from shamsu.agents.bugfix_workflow import BugFixWorkflow
from shamsu.agents.chat_loop import edit_tools_for_target, AgentChatLoop, AgentLoopResult, _thinking_preview
from shamsu.agents.code_edit_workflow import CodeEditWorkflow
from shamsu.agents.doc_workflow import DocumentationWorkflow
from shamsu.agents.error_feedback_loop import ErrorFeedbackLoop
from shamsu.agents.freeform_generator import (
    FILE_CONTENT_SCHEMA,
    _loads as _loads_freeform_json,
    _sanitize_generated_content,
)
from shamsu.agents.full_pipeline import FullDjangoPipeline, FullPipelineResult
from shamsu.agents.orchestrator import AgentOrchestrator
from shamsu.agents.plan_mode import PlanningWorkflow
from shamsu.agents.planner import deterministic_user_decision
from shamsu.cli.command_router import CommandRouter
from shamsu.cli.arguments import parse_args
from shamsu.cli.approval_ui import (
    get_permission_memory as _get_permission_memory,
    make_approval_manager,
)
from shamsu.cli.request_lifecycle import (
    finish_current_run as _finish_current_run,
    log_assistant_message as _log_assistant_message,
    log_event as _log_event,
)
from shamsu.cli.session_commands import handle_logs as _modular_handle_logs
from shamsu.cli.session_commands import handle_run as _modular_handle_run
from shamsu.cli.session_commands import handle_runs as _modular_handle_runs
from shamsu.context.manager import ContextBudgetManager
from shamsu.agents.qa_workflow import NO_LIVE_TOOLS_NOTICE, QAWorkflow
from shamsu.agents.task_harness import (
    TaskPlan,
    append_task_handoff,
    build_task_plan,
    plan_log_payload,
)
from shamsu.agents.task_execution_workflow import TaskExecutionResult, TaskExecutionWorkflow
from shamsu.agents.test_generation_workflow import TestGenerationWorkflow
from shamsu.core.coordinator import Coordinator
from shamsu.indexer.policy import walk_workspace_files
from shamsu.llm.manager import LLMManager, LLMStalledError, ModelPullProgress
from shamsu.memory.service import MemoryService, REQUIRED_MEMORY_MESSAGE
from shamsu.integrations.telegram.local import (
    handle_remote_control_command,
    redact_remote_control_command,
)
from shamsu.memory.queue import flush_memory_queues, get_memory_queue
from shamsu.context.progress import render_progress_checklist
from shamsu.prd.contract import extract_contract
from shamsu.prd import headings as prd_headings
from shamsu.verify import contract
from shamsu.verify.gate import default_verify_command, stack_of, verify_only
from shamsu.prd.input import (
    PRDParseError,
    extract_document_reference,
    is_prd_filename,
    parse_prd_file,
)
from shamsu.prd.execution import (
    attach_task_id,
    block_milestone,
    checkpoint_milestone,
    first_incomplete_milestone_index,
    initialize_prd_execution,
    load_milestone_preflight,
    mark_milestone_running,
    milestone_lines_from_state,
    model_preflight_schema,
    prd_execution_root,
    record_milestone_preflight,
    record_milestone_repair,
    record_milestone_rollback,
    render_preflight_context,
    validate_model_preflight,
)
from shamsu.prd.project import build_project_spec
from shamsu.prd.requirements import (
    compile_requirement_ledger,
    is_complex_prd_contract,
    save_prd_execution_artifacts,
)
from shamsu.prd.state import create_generation_state, save_generation_state, state_path
from shamsu.plans.contracts import (
    TaskContract,
    contract_prompt,
    contracts_from_markdown,
    load_plan_contracts,
    request_scope_expansion,
    run_file_preflight,
    validate_contract,
    write_plan_contracts,
)
from shamsu.plans.store import parse_plan_steps, plan_has_no_steps, read_plan
from shamsu.routing.operations import (
    OperationPlan,
    OperationStep,
    parse_operation_plan,
    recover_original_prompt,
)
from shamsu.retriever.search import NullSearchAgent, SearchAgent
from shamsu.tasks.state import (
    MilestoneTask,
    advance_phase,
    create_task,
    list_task_ids,
    load_task,
    mark_step_done,
    mark_step_failed,
    mark_step_blocked,
    mark_step_running,
    save_task,
)
from shamsu.taskmaster.service import TaskmasterService
from shamsu.taskmaster.types import TaskmasterTask
from shamsu.skills.cli import handle_skills_command
from shamsu.skills.loader import discover_skills
from shamsu.skills.selector import render_skill_context
from shamsu.skills.types import SelectedSkill, SkillSelection
from shamsu.runtime.doctor import find_ancestor_workspace, format_report, run_doctor
from shamsu.runtime.models import (
    DEFAULT_TIER,
    ModelTier,
    active_model_override,
    active_tier,
    clear_model_override,
    initialize_model_tier,
    model_for_role,
    role_should_think,
    set_model_override,
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
from shamsu.runtime.run_control import active_run_ids, cancel_run
from shamsu.runtime.session_registry import claim_ollama_ownership, register_session
from shamsu.safety import dry_run, read_only
from shamsu.safety.approval import (
    ask_approval,
    ask_approval_menu,
    ask_tier_choice,
    prompt_is_active,
)
from shamsu.safety.autonomy import is_long_running_enabled, set_long_running_enabled
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.patch import git_apply as patch_git_apply
from shamsu.patch import types as patch_types
from shamsu.patch.engine import PatchEngine
from shamsu.patch.preview import print_diff_preview
from shamsu.diagnostics import swallowed
from shamsu.patch.rollback import latest_undoable_transaction, rollback_transaction
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.audit import SessionAuditLog
from shamsu.session.manager import SessionLogger, SessionManager
from shamsu.session.memory import is_affirmative, is_negative, strip_filler_prefix
from shamsu.templates.django.writer import DjangoProjectWriter
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.tools.browser import BrowserTool
from shamsu.tools.dev_server import (
    DevServerManager,
    extract_dev_command_from_sentence,
    infer_dev_command,
    is_dev_server_command,
)
from shamsu.tools.web import (
    WebFetchResult,
    WebSearchFetchResult,
    WebSearchResult,
    WebServiceManager,
    WebTool,
    build_evidence_answer_prompt,
)
from shamsu.tools.codebase_memory import CodebaseMemoryAdapter
from shamsu.tools.django import DjangoSetupResult, DjangoSetupRunner, DjangoTestRunner
from shamsu.tools.executor import CommandRunner
from shamsu.tools.git import GitTool
from shamsu.tools.workspace import DOCUMENT_EXTENSIONS, MentionResolver, WorkspaceTool
from shamsu.ui.progress import ProgressReporter
from shamsu.ui.trace import emit_trace, read_trace_mode, write_trace_mode
from shamsu.agents.clarification import classify_reply, format_question, resolve_answer
from shamsu.types import (
    ApprovalRequest,
    ContextPack,
    ProjectSpec,
    RoutingDecision,
    TaskStep,
    TaskStepStatus,
    ToolResult,
)

if sys.platform == "win32":
    from prompt_toolkit.output.win32 import NoConsoleScreenBufferError
else:
    NoConsoleScreenBufferError = RuntimeError

EmptySearchAgent = NullSearchAgent


def _make_approval_manager(
    workspace: Path,
    session_logger: SessionLogger | None,
    console: Console,
    approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
):
    """Compatibility facade; approval policy/UI ownership lives in approval_ui."""
    return make_approval_manager(
        workspace,
        session_logger,
        console,
        approval_func,
        menu_prompt_func=ask_approval_menu,
    )


# Subcommands that claim the bare word "run" in the REPL dispatcher (see the
# main loop below) - anything else starting with "run" (e.g. "run the tests")
# is ordinary English and falls through to the normal agent request path.
_RUN_SUBCOMMANDS = frozenset(
    {
        "last",
        "show",
        "timeline",
        "decisions",
        "tools",
        "commands",
        "context",
        "diff",
        "validate",
        "export",
        "clean",
        "narrative",
        "prompt",
        "cot",
    }
)


SYSTEM_COMMANDS = (
    "/help",
    "/remote_control",
    "/remote_control status",
    "/remote_control connect",
    "/remote_control configure ",
    "/remote_control disconnect",
    "/remote_control repair",
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
    "/plan",
    "/plan ",
    "/plan off",
    "/proceed",
    "/plan-prd ",
    "/build ",
    "/generate-django ",
    "/generate-prd ",
    "/models status",
    "/models pull",
    "/models repair",
    "/models tier",
    "/models tier light",
    "/models tier default",
    "/models tier heavy",
    "/models use ",
    "/models use tier",
    "/web search ",
    "/web setup",
    "/web status",
    "/web start",
    "/web stop",
    "/web restart",
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
    "/compact",
    "/compact clear",
    "/sessions list",
    "/sessions new",
    "/sessions current",
    "/sessions show ",
    "/sessions resume ",
    "/sessions rename ",
    "/sessions close",
    "/sessions export ",
    "/sessions trace",
    "/sessions summary",
    "/sessions memory",
    "/sessions search ",
    "/sessions fork",
    "/sessions fork ",
    "/sessions history ",
    "/sessions tree",
    "/permissions list",
    "/permissions clear",
    "/skills",
    "/skills list",
    "/skills show ",
    "/skills explain ",
    "/skills suggest ",
    "/mcp status",
    "/mcp servers",
    "/mcp tools ",
    "/mcp config",
    "/mcp reload",
    "/mcp auth logout ",
    "/milestones list",
    "/milestones show ",
    "/taskmaster status",
    "/taskmaster setup",
    "/taskmaster repair",
    "/prd parse ",
    "/prd status",
    "/prd reparse",
    "/tasks",
    "/tasks list",
    "/tasks next",
    "/tasks show ",
    "/tasks execute ",
    "/tasks continue",
    "/tasks mark-done ",
    "/tasks mark-blocked ",
    "/tasks mark-failed ",
    "/tasks dependencies ",
    "/tasks plan",
    "/autonomy status",
    "/autonomy on",
    "/autonomy off",
    "/trace status",
    "/trace on",
    "/trace off",
    "/trace normal",
    "/trace verbose",
    "/trace raw",
    "/debug on",
    "/debug off",
    "/debug status",
    "/audit-log tail",
    "/audit-log show ",
    "/audit-log grep ",
    "/audit-log export",
    "/audit-log open",
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
    "/undo",
    "/patch journal",
    "/patch last",
    "/patch diff ",
    "/patch trash",
    "/patch clean-trash",
    "/log",
    "/log tail",
    "/logs",
    "/logs open",
    "/runs",
    "/run last",
    "/run narrative ",
    "/run report ",
    "/run prompt ",
    "/run cot ",
    "/run show ",
    "/run timeline ",
    "/run decisions ",
    "/run tools ",
    "/run commands ",
    "/run context ",
    "/run diff ",
    "/run validate ",
    "/run export ",
    "/run clean",
    "/context status",
    "/context budget",
    "/context meter",
    "/context inspect",
    "/context compact",
    "/context show",
    "/edit ",
    "/fix ",
    "/test-gen ",
    "/audit ",
    "/docs ",
    "/exit",
)

_MODEL_COMPLETION_CACHE: tuple[float, tuple[str, ...]] = (0.0, ())


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
        if lowered.startswith("/models use "):
            fragment = text[len("/models use ") :]
            candidates = ["tier", *_installed_model_completion_names()]
            seen: set[str] = set()
            for candidate in candidates:
                if candidate in seen:
                    continue
                seen.add(candidate)
                if candidate.lower().startswith(fragment.lower()):
                    yield Completion(candidate, start_position=-len(fragment))
            return
        for command in SYSTEM_COMMANDS:
            if command.startswith(lowered):
                yield Completion(command, start_position=-len(text))


def _installed_model_completion_names() -> tuple[str, ...]:
    global _MODEL_COMPLETION_CACHE
    now = time.monotonic()
    cached_at, cached = _MODEL_COMPLETION_CACHE
    if now - cached_at < 5.0:
        return cached
    try:
        status = collect_status()
    except Exception:
        return cached
    models = tuple(status.installed_models if status.server_running else ())
    _MODEL_COMPLETION_CACHE = (now, models)
    return models


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
                    "  plan a dark-mode toggle    (make a plan first, review, then `proceed`)",
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
                    "  /memory status           Show local project memory and optional Graphiti mirror",
                    "  /memory setup            Install/configure optional Graphiti mirror",
                    "  /memory repair           Re-check and repair optional Graphiti config",
                    "  /memory remember <text>  Store explicit durable memory",
                    "  /memory search <query>   Search local project memory",
                    "  /memory forget <query>   Forget/mark local project memory",
                    "  /parse-prd <file>         Parse a Markdown, TXT, or PDF PRD",
                    "  /plan                     Plan mode: your next prompt gets planned, not built",
                    "  /plan <task>              Plan that task now; review .shamsu/plans/*.md",
                    "  /plan off                 Leave plan mode",
                    "  /proceed                  Execute the last plan you approved",
                    "  /plan-prd <file>          Preview and approve a project plan",
                    "  /generate-django <file>   Generate deterministic Django backend files",
                    "  /generate-prd <file> --output <dir>",
                    "  /django setup [dir]       Install generated deps and run migrations",
                    "  /django test [dir]        Run generated Django tests",
                    "  /django fix-tests [dir]   Run tests and apply bug-fix loop",
                    "  /context status           Show model context windows and calibration",
                    "  /context budget           Show last model call's token budget",
                    "  /context meter            Live context meter, counters and efficiency",
                    "  /context inspect          Detailed budget breakdown",
                    "  /context compact          Show auto-compact threshold and last status",
                    "  /context show             Show observability + what the working trace surfaces",
                    "  /models status            Show local Ollama/model status",
                    "  /models pull              Pull missing local models",
                    "  /models repair            Start Ollama and pull missing models",
                    "  /models tier [light|default|heavy]  Show or switch model tier",
                    "  /models use <model>|tier   Pin an installed Ollama model or return to tiers",
                    "  /web search <query>       Search the web with approval",
                    "  /web open <url>           Fetch and summarize a web page",
                    "  /web summarize <url>      Alias for /web open",
                    "  /browse open <url>        Open a page in the local browser",
                    "  /browse read              Read the current browser page",
                    "  /browse click <selector>  Click the current page",
                    "  /browse type <selector> <text>",
                    "  /browse screenshot        Save a browser screenshot",
                    "  /sessions list            List workspace sessions",
                    "  /sessions new [title]     Start a fresh session, keeping the current one",
                    "  /sessions current         Show current session",
                    "  /sessions show <id>       Show session metadata",
                    "  /sessions resume <id>     Resume another session",
                    "  /sessions rename <id> <title>",
                    "  /sessions close [id]      Close a session",
                    "  /sessions export <id>     Export redacted session bundle",
                    "  /sessions trace [id]      Show structured action log (no hidden reasoning)",
                    "  /sessions summary [id]    Show the session summary",
                    "  /sessions memory [id]     Show local session memory records",
                    "  /sessions search <query>  Search titles, summaries, messages, memory",
                    "  /permissions list         Show remembered 'always allow' decisions",
                    "  /permissions clear        Forget all remembered approval decisions",
                    "  /skills list              Show bundled, user, and workspace skills",
                    "  /skills show <name>       Show one skill's instructions and policy",
                    "  /skills explain <prompt>  Preview deterministic skill selection",
                    "  /skills suggest <prompt>  Alias for explain; shows skill names to use",
                    "  /mcp status              Connect to configured external MCP servers",
                    "  /mcp tools [server]      List discovered external tools",
                    "  /mcp config              Show MCP config locations and safe settings",
                    "  /mcp reload              Reload config and reconnect servers",
                    "  /mcp auth logout <name>  Clear a server's OAuth credentials",
                    "  /milestones list          List tracked internal multi-step milestones",
                    "  /milestones show <id>     Show a milestone's steps, phase, and blockers",
                    "  /taskmaster status        Show Taskmaster install/provider health",
                    "  /taskmaster setup         Install/configure local Taskmaster (Ollama-only)",
                    "  /taskmaster repair        Re-check and repair Taskmaster config",
                    "  /prd parse <file>         Parse a PRD into a Taskmaster task graph",
                    "  /prd status               Show last-parsed PRD and task graph summary",
                    "  /prd reparse [file]       Force Taskmaster to reparse the PRD",
                    "  /tasks                    List Taskmaster tasks (status/priority/deps)",
                    "  /tasks next               Show the next unblocked Taskmaster task",
                    "  /tasks show <id>          Show one Taskmaster task's detail",
                    '  /tasks continue [--all] [--verify "<cmd>"]  Run the next task through SHAMSU',
                    "  /tasks mark-done <id>     Explicitly accept a task without verification",
                    "  /tasks mark-blocked <id> <reason>",
                    "  /tasks mark-failed <id> <reason>",
                    "  /tasks dependencies <id>  Show a task's dependencies and their status",
                    "  /tasks plan               Show the task order / dependency graph",
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
                    "  /undo                     Undo the most recent file change",
                    "  /patch rollback <id>      Restore every file a transaction touched",
                    "  /patch journal            List all recorded transactions",
                    "  /patch last               Show the most recent transaction",
                    "  /patch diff <id>          Show the diff applied by a transaction",
                    "  /patch trash              List files moved to .shamsu/trash",
                    "  /patch clean-trash        Permanently delete everything in trash (with approval)",
                    "  /trace on|off|verbose|raw Set the visible working trace (raw = debug: route, plan, tool calls/outputs, raw content)",
                    "  /debug on|off             Toggle a rich debug trace (route, model, plan, tool calls, tool outputs, file changes)",
                    "  /log tail                 Show recent session events",
                    "  /logs                     Where this project's logs live, and the detail level",
                    "  /logs open                Show every log path under .shamsu",
                    "  /audit-log tail [n]       Tail the detailed per-step audit trail (.shamsu/audit)",
                    "  /audit-log show <session> Show one session's full audit trail",
                    "  /audit-log grep <query>   Search the audit trail",
                    "  /audit-log export [path]  Export the audit trail to JSONL",
                    "  /audit-log open           Show the audit log locations",
                    "  /runs [n]                 List recent ActionLedger runs (local debug/audit log)",
                    "  /run last                 Show the latest run's summary",
                    "  /run report [run-id]      Read the human report (narrative is an alias)",
                    "  /run prompt [run-id]      Show verbose-mode model prompt evidence",
                    "  /run cot [run-id]         Show verbose-mode reasoning evidence",
                    "  /run show <run-id>        Show a run's manifest and summary",
                    "  /run timeline <run-id>    Show a run's chronological events",
                    "  /run decisions <run-id>   Show a run's decision summaries",
                    "  /run tools <run-id>       Show a run's tool calls and outcomes",
                    "  /run commands <run-id>    Show a run's commands, exit codes, log paths",
                    "  /run context <run-id>     Show a run's safe context preview",
                    "  /run diff <run-id>        Show a run's patch/mutation references",
                    "  /run validate <run-id>    Validate a run's artifact integrity",
                    "  /run export <run-id>      Export a run to a redacted zip + markdown report",
                    "  /run clean                Delete runs older than the retention window (asks for approval)",
                    "  /edit <request>           Force code-edit workflow",
                    "  /fix <bug/traceback>      Force bug-fix workflow",
                    "  /test-gen <request>       Force test-generation workflow",
                    "  /audit <request>          Force audit workflow",
                    "  /docs <request>           Force README documentation workflow",
                "  /help                     Show commands",
                "  /remote_control           Enable Telegram remote control",
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
    """Search with external graph retrieval plus local semantic degradation."""
    gate = AbstractService(workspace).ensure_ready()
    status = gate.status
    uses_external_index = bool(
        status and status.health.ok and status.index.exists and not status.index.stale
    )
    search = SearchAgent(workspace)
    search.external_enabled = uses_external_index
    return search, uses_external_index


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
    if not spec.generation_ready:
        console.print(f"[yellow]Needs input: {spec.clarification_question}[/yellow]")
        return
    request = ApprovalRequest(
        action_type="file_write",
        description="Record this PRD project plan as approved for future generation.",
        risk_level="medium",
        preview=_project_plan_summary(spec),
        working_dir=str(workspace),
        reason="M3 only stores resume metadata; it does not generate project files.",
        target_paths=[state_path(workspace).relative_to(workspace).as_posix()],
    )
    approved = _make_approval_manager(workspace, session_logger, console, approval_func).ask(
        request
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
    spec = build_project_spec(parsed, request_text="Use the Django backend blueprint.")
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
    if not spec.generation_ready:
        console.print(f"[yellow]Generation stopped: {spec.clarification_question}[/yellow]")
        return
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
        generate=_pipeline_generate(session_logger),
    ).run(prd_path, target_dir=output_dir)
    _print_full_pipeline_result(result, console)


def _pipeline_generate(session_logger: SessionLogger | None):
    """A blocking (system, user, schema) -> raw-JSON generator backed by the coder
    model, so the freeform/scaffold pipeline can drive generation from its worker
    thread. Without this the FREEFORM strategy has no model wired and cannot build
    - the reason the template scaffolds used to be the only working path."""
    ledger = get_current_run()
    timeout_seconds = float(os.environ.get("SHAMSU_FREEFORM_MODEL_TIMEOUT_SECONDS", "180"))

    def _generate(system: str, user: str, schema: dict) -> str:
        async def _call() -> str:
            return await asyncio.wait_for(
                LLMManager(session_logger=session_logger, action_ledger=ledger).generate_structured(
                    "coder",
                    system,
                    user,
                    schema,
                    num_predict=_structured_num_predict_for(system, schema),
                ),
                timeout=timeout_seconds,
            )

        try:
            return asyncio.run(_call())
        except asyncio.TimeoutError as exc:
            if ledger:
                ledger.log_event(
                    "freeform_model_call_timed_out",
                    role="coder",
                    timeout_seconds=timeout_seconds,
                )
            raise TimeoutError(
                f"Freeform model call timed out after {timeout_seconds:.0f}s"
            ) from exc

    return _generate


def _freeform_num_predict() -> int:
    raw = os.environ.get("SHAMSU_FREEFORM_NUM_PREDICT", "8192")
    try:
        value = int(raw)
    except ValueError:
        return 8192
    return max(1024, value)


def _structured_num_predict_for(system: str, schema: dict) -> int:
    if "STRICT DEBUG MODE" in (system or ""):
        return _env_int_at_least("SHAMSU_REPAIR_NUM_PREDICT", 1024, 256)
    properties = (schema or {}).get("properties")
    if isinstance(properties, dict) and "files" in properties:
        return _env_int_at_least("SHAMSU_FREEFORM_PLAN_NUM_PREDICT", 2048, 512)
    return _freeform_num_predict()


def _env_int_at_least(name: str, default: int, minimum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return max(minimum, value)


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
        table.add_row(
            "Tests", f"{result.test_result.passed} passed, {result.test_result.failed} failed"
        )
    if result.dod_result:
        if result.dod_result.required_failures:
            dod_status = "failed: " + ", ".join(
                item.item_id for item in result.dod_result.required_failures
            )
        elif result.dod_result.required_unverified:
            dod_status = "unverified: " + ", ".join(
                item.item_id for item in result.dod_result.required_unverified
            )
        else:
            dod_status = "ok"
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
        fields = ", ".join(f"{field.name}:{field.django_type}" for field in entity.fields)
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
    contract = getattr(spec, "prd_contract", None)
    lines = [
        f"Project: {spec.project_name}",
        f"App: {spec.app_name}",
        f"Theme: {spec.theme}",
        f"Status: {'ready' if spec.generation_ready else 'needs input'}",
        f"Entities: {len(spec.entities)}",
        f"Endpoints: {len(spec.endpoints)}",
        f"Pages: {len(spec.pages)}",
        f"Files planned: {len(spec.generation_order)}",
    ]
    if contract is not None:
        lines.append(f"Extraction confidence: {contract.extraction_confidence:.0%}")
        lines.extend(f"Extraction warning: {item}" for item in contract.extraction_warnings)
    lines.extend(f"Assumption: {item}" for item in spec.assumptions)
    if spec.clarification_question:
        lines.append(f"Question: {spec.clarification_question}")
    return "\n".join(lines)


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
            service.status()
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
        queue_status = get_memory_queue(workspace).status()
        console.print(
            f"Codebase-Memory available: {status.health.available} ({status.health.message})"
        )
        console.print(f"Mirror queue: {queue_status['pending']}/{queue_status['capacity']} pending")
        console.print(
            f"Index: {'stale' if status.index.stale else 'fresh'} - {status.index.message}"
        )
        console.print(f"Normal code-agent mode allowed: {status.normal_mode_allowed}")
        console.print(
            f"Retrieval mode: {status.retrieval_mode}{' (degraded)' if status.degraded else ''}"
        )
        return
    if subcommand == "setup":
        result = service.setup()
        console.print(
            "[green]Setup complete.[/green]"
            if result.get("ok")
            else f"[red]Setup failed: {result.get('error', result)}[/red]"
        )
        return
    if subcommand == "repair":
        result = service.repair()
        console.print(
            "[green]Repair complete.[/green]"
            if result.get("ok")
            else f"[red]Repair failed: {result.get('message', result)}[/red]"
        )
        return
    if subcommand == "build":
        console.print(service.build())
        return
    if subcommand == "refresh":
        console.print(service.refresh())
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
    lines.append(
        "- Native/SARIF/errorformat parsing runs before fallback parsers; no LLM parses raw logs first."
    )
    lines.append("- Syntax and import/export errors are ranked before cascading noise.")
    for item in roots[:5]:
        code = f" {item.get('code')}" if item.get("code") else ""
        lines.append(
            f"- Root cause: {item.get('category')}{code} {item.get('message', '')}".strip()
        )
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
            lines.append(
                f"- {snippet.get('file')}:{snippet.get('line_start')}-{snippet.get('line_end')} {snippet.get('reason', '')}"
            )
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
        # Graphiti is an optional mirror. This is a no-op unless explicitly
        # enabled/configured; SQLite project memory is the normal path.
        service.ensure_backend_started()
        if service.healthcheck().ok:
            console.print("[dim]Project memory: SQLite ready; Graphiti mirror ready[/dim]")
        else:
            console.print("[dim]Project memory: SQLite ready[/dim]")
    except Exception as exc:
        console.print(
            f"[yellow]Project memory startup check failed ({exc}). Local memory will retry on use.[/yellow]"
        )


def _memory_command_allowed(normalized_input: str) -> bool:
    lowered = normalized_input.lower()
    return (
        lowered in {"help", "doctor", "exit", "quit"}
        or lowered.startswith("memory")
        or lowered.startswith("diagnostics")
        or lowered.startswith("patch")
        or lowered.startswith("taskmaster")
    )


def _handle_memory(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    _, _, rest = user_input.partition(" ")
    parts = rest.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else "status"
    argument = parts[1].strip() if len(parts) > 1 else ""
    service = MemoryService(workspace)

    if subcommand == "status":
        status = service.status()
        console.print("Local SQLite: available")
        console.print(f"Graphiti mirror: {'enabled' if service.graphiti_enabled else 'disabled'}")
        console.print(f"Graphiti health: {status.health.available} ({status.health.message})")
        console.print(f"Config: {status.health.config_path or service._config_path()}")
        console.print(f"Workspace memory path: {status.memory_path}")
        console.print(f"Normal agent mode allowed: {status.normal_mode_allowed}")
        return
    if subcommand == "setup":
        result = service.setup()
        if result.get("ok"):
            console.print("[green]Graphiti setup complete.[/green]")
        else:
            reason = (
                result.get("error") or result.get("manual_steps") or result.get("message") or result
            )
            console.print(f"[red]Graphiti setup failed: {reason}[/red]")
        return
    if subcommand == "repair":
        result = service.repair()
        if result.get("ok"):
            console.print("[green]Graphiti repair complete.[/green]")
        else:
            reason = (
                result.get("manual_steps") or result.get("message") or result.get("error") or result
            )
            console.print(f"[red]Graphiti repair failed: {reason}[/red]")
        return
    if subcommand == "remember":
        if not argument:
            console.print("[red]Usage: /memory remember <text>[/red]")
            return
        # When there's an active session, route through the session bridge so the
        # explicit memory is also recorded in the session's local memory.jsonl.
        if session_logger is not None:
            bridge = session_logger.save_long_term_memory(
                "user_preference",
                argument,
                {"reason": "explicit_remember", "explicit": True, "confidence": 1.0},
            )
            outcome = bridge.get("long_term") or {}
            if outcome.get("ok"):
                message = (
                    "Memory already existed."
                    if outcome.get("deduped")
                    else (
                        "Memory stored locally; Graphiti mirror queued."
                        if outcome.get("queued")
                        else "Memory stored locally."
                    )
                )
                console.print(f"[green]{message}[/green]")
            elif bridge.get("local"):
                console.print(
                    "[green]Saved to session memory (long-term backend unavailable).[/green]"
                )
            else:
                console.print(
                    f"[yellow]Memory not stored: {outcome.get('reason') or outcome.get('error') or 'skipped'}[/yellow]"
                )
            return
        result = get_memory_queue(workspace).enqueue(
            argument,
            "user_preference",
            {"reason": "explicit_remember", "explicit": True, "confidence": 1.0},
        )
        if result.get("ok"):
            console.print(
                "[green]Memory stored.[/green]"
                if not result.get("deduped")
                else "[green]Memory already existed.[/green]"
            )
        else:
            console.print(
                f"[yellow]Memory not stored: {result.get('reason') or result.get('error') or result}[/yellow]"
            )
        return
    if subcommand in {"search", "recent"}:
        query = argument or "recent durable SHAMSU memory"
        result = service.search(query, limit=8)
        if not result.get("ok"):
            console.print(f"[red]Memory search failed: {result.get('error') or result}[/red]")
            return
        rows = result.get("results", [])
        if not rows:
            console.print("[dim]No project memories found.[/dim]")
            return
        table = Table(title="Project Memory")
        table.add_column("ID")
        table.add_column("Memory")
        for item in rows[:8]:
            if isinstance(item, dict):
                memory_id = str(item.get("id") or item.get("uuid") or "")
                text = str(item.get("text") or item.get("fact") or item)
            else:
                memory_id = ""
                text = str(item)
            table.add_row(
                memory_id,
                text[:240],
            )
        console.print(table)
        return
    if subcommand == "forget":
        if not argument:
            console.print("[red]Usage: /memory forget <memory-id-or-query>[/red]")
            return
        result = service.forget(argument)
        console.print(
            "[green]Forget request completed.[/green]"
            if result.get("ok")
            else f"[yellow]Forget request not completed: {result.get('error') or result}[/yellow]"
        )
        return
    if subcommand == "summarize-session":
        if not argument:
            console.print("[red]Usage: /memory summarize-session <session-id>[/red]")
            return
        result = service.summarize_session(argument)
        console.print(
            "[green]Session summary stored.[/green]"
            if result.get("ok")
            else f"[yellow]Session summary not stored: {result.get('error') or result}[/yellow]"
        )
        return
    console.print(
        "[red]Usage: /memory status|setup|repair|remember|search|recent|forget|summarize-session[/red]"
    )


def _record_task_memory(
    workspace: Path,
    text: str,
    kind: str = "task_summary",
    session_logger: SessionLogger | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist only evidence-backed automatic memory, then queue its mirror."""
    ledger = get_current_run()
    outcome = ledger.evidence_outcome() if ledger is not None else "unknown"
    events = action_ledger_store.load_events(workspace, ledger.run_id) if ledger is not None else []
    event_types = {str(event.get("type", "")) for event in events}
    verified = "verification_passed" in event_types
    mutation_recorded = any(
        str(event.get("type", "")) == "patch_apply_succeeded"
        or (
            str(event.get("type", "")) == "mutation_finished"
            and str(event.get("status", "")) == "applied"
        )
        for event in events
    )
    details = {
        **(metadata or {}),
        "automatic": True,
        "outcome": outcome,
        "verified": verified,
        "source_run_id": ledger.run_id if ledger is not None else "",
        "run_id": ledger.run_id if ledger is not None else "",
    }
    if outcome not in {"success", "success_unverified"} or not mutation_recorded:
        result = {
            "ok": False,
            "skipped": True,
            "reason": f"outcome={outcome}, mutation={mutation_recorded}",
        }
        _log_event(
            session_logger,
            "memory.write_skipped",
            result,
            "Automatic memory skipped",
            workflow_id="memory",
        )
        return result
    if kind == "bug_lesson" and not verified:
        kind = "task_summary"
    if outcome == "success_unverified":
        memory_text = f"Task outcome (success_unverified): {_memory_request_text(text)} Changes were applied but not verified."
        details["confidence"] = 0.65
    else:
        prefix = "Verified task outcome" if verified else "Task outcome (success)"
        memory_text = f"{prefix}: {_memory_request_text(text)}"
        details["confidence"] = 0.95 if verified else 0.8
    if session_logger is not None:
        result = session_logger.save_long_term_memory(kind, memory_text, details)
        queued = result.get("long_term") or {}
    else:
        queued = get_memory_queue(workspace).enqueue(memory_text, kind, details)
        result = {"local": bool(queued.get("local")), "long_term": queued}
    _log_event(
        session_logger,
        "memory.write",
        {
            "ok": bool(queued.get("ok")),
            "kind": kind,
            "outcome": outcome,
            "verified": verified,
            "queued": bool(queued.get("queued")),
        },
        "Automatic memory evaluated",
        workflow_id="memory",
    )
    return result


def _memory_request_text(text: str) -> str:
    """Keep automatic memory about the request, not injected retrieval context."""
    clean = str(text or "").strip()
    for marker in (
        "\n\nAdditional SHAMSU context:",
        "\n\nTask execution handoff:",
        "\n\nRelevant long-term memory:",
    ):
        clean = clean.split(marker, 1)[0].strip()
    return clean[:700]


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
        table.add_row(
            str(step.id), step.phase, step.description, step.status.value, step.error or ""
        )
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


# Trace mode is owned by shamsu.ui.trace (single source of truth, shared with
# the chat loop). These thin wrappers keep the existing REPL call sites stable.
def _trace_mode(workspace: Path) -> str:
    return read_trace_mode(workspace)


def _set_trace_mode(workspace: Path, mode: str) -> None:
    write_trace_mode(workspace, mode)


def _make_trace_emitter(
    console: Console,
    workspace: Path,
    session_logger: SessionLogger | None,
) -> Callable[[str, str, dict | None, str], None]:
    """Bind emit_trace to this workspace/console so the chat loop can surface
    structured trace events (route/plan/blockers/clarification) without knowing
    about the console or the persisted trace mode."""

    def _emit(
        event_type: str, message: str, payload: dict | None = None, level: str = "normal"
    ) -> None:
        emit_trace(console, session_logger, workspace, event_type, message, payload, level)

    return _emit


def _resolve_pending_question(
    pending: dict[str, Any],
    reply: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger,
) -> str | None:
    """Interpret `reply` as the answer to a stored pending question.

    Returns a rewritten prompt to dispatch (original request + resolved answer),
    or None when nothing further should run (cancel / decline). The pending
    question is always cleared here so a stale question never lingers.
    """
    answer = resolve_answer(pending, reply)
    resolved_value = answer.value or reply
    session_logger.clear_pending_question(answered=True, answer=resolved_value)
    emit_trace(
        console,
        session_logger,
        workspace,
        "clarification.answered",
        resolved_value,
        {"kind": answer.kind},
        level="normal",
    )
    if answer.kind == "cancel":
        console.print("[yellow]Cancelled. What would you like to do next?[/yellow]")
        return None
    if answer.kind == "negative":
        console.print(
            "[yellow]Understood - I won't proceed with that. Tell me how you'd like to continue.[/yellow]"
        )
        return None
    if not answer.resolved:
        console.print(
            "[yellow]I couldn't match that to the question. Let's continue normally.[/yellow]"
        )
        return None
    created_from = str(pending.get("created_from_prompt", "")).strip()
    question = str(pending.get("question", "")).strip()
    if created_from:
        return f'{created_from}\n\n(Answering the earlier question "{question}": {resolved_value})'
    return resolved_value


# Two interrupts inside this window mean "I really want out", not "cancel again".
_DOUBLE_INTERRUPT_SECONDS = 2.0


class _RequestRunner:
    """Runs one REPL request on a session-lifetime loop, cancellable with Ctrl+C.

    Every request used to get its own ``asyncio.run``. Ctrl+C during one raised
    KeyboardInterrupt straight through it, and because that is not an
    ``Exception`` the loop's catch-all never saw it: the interrupt escaped
    ``while True``, ended ``main()``, and took plan mode, pending actions, and
    the in-flight run's state with it. A cancelled operation is not a cancelled
    session.

    The first Ctrl+C now asks the active run to stop and cancels the request
    task; the run's own cancel path marks it CANCELLED and the prompt comes
    back. A second Ctrl+C within :data:`_DOUBLE_INTERRUPT_SECONDS` exits, so
    quitting is still two keystrokes away.
    """

    def __init__(self, console: Console) -> None:
        self.console = console
        self.cancelled = False
        self._loop = asyncio.new_event_loop()
        self._task: asyncio.Task[Any] | None = None
        self._last_interrupt = 0.0

    def run(self, coro: Any) -> bool:
        """Run ``coro`` to completion. Returns False when the user cancelled it."""
        self.cancelled = False
        try:
            previous = signal.signal(signal.SIGINT, self._on_interrupt)
        except ValueError:
            # Not the main thread (embedded/test use): no signal handling here,
            # but the request must still run.
            previous = None
        try:
            self._loop.run_until_complete(self._guarded(coro))
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)
        return not self.cancelled

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._loop.close()

    async def _guarded(self, coro: Any) -> Any:
        self._task = self._loop.create_task(coro)
        # Windows' proactor loop can sit inside an IOCP wait that never returns
        # to Python bytecode, and a signal handler only runs between bytecodes -
        # so without a periodic wakeup Ctrl+C would not be seen until the wait
        # ended on its own, which is exactly when it is least useful.
        pump = self._loop.create_task(self._signal_pump())
        try:
            return await self._task
        except asyncio.CancelledError:
            self.cancelled = True
            self.console.print(
                "\n[yellow]Cancelled that operation.[/yellow] "
                "[dim]The session is still here - your plan and pending actions are intact.[/dim]"
            )
            return None
        finally:
            pump.cancel()
            # Await the cancelled children so their CancelledError is retrieved
            # here rather than surfacing later as "Task exception was never
            # retrieved" noise on an otherwise healthy session.
            with contextlib.suppress(asyncio.CancelledError):
                await pump
            if self._task is not None and self._task.done() and not self._task.cancelled():
                with contextlib.suppress(Exception):
                    self._task.exception()
            self._task = None

    async def _signal_pump(self) -> None:
        while True:
            await asyncio.sleep(0.15)

    def _on_interrupt(self, _signum: int, _frame: Any) -> None:
        now = time.monotonic()
        if now - self._last_interrupt < _DOUBLE_INTERRUPT_SECONDS:
            raise KeyboardInterrupt
        self._last_interrupt = now
        # Ask the run to stop first. Setting its cancel_event before cancelling
        # the task lets the agent loop take its own graceful exit - recording a
        # CANCELLED status and returning partial work - instead of unwinding
        # from whatever await it happened to be sitting on.
        for run_id in active_run_ids():
            with contextlib.suppress(Exception):
                cancel_run(run_id)
        task = self._task
        if task is not None and not task.done():
            self._loop.call_soon_threadsafe(task.cancel)
        self.console.print(
            "[dim]Stopping the current operation... (Ctrl+C again to leave SHAMSU)[/dim]"
        )


_REQUEST_RUNNER: _RequestRunner | None = None


def _run_request(coro: Any, console: Console | None = None) -> bool:
    """Run a REPL request under Ctrl+C cancellation. False when cancelled.

    Falls back to a plain ``asyncio.run`` when no session runner exists, so
    tests and non-REPL callers keep working unchanged.
    """
    runner = _REQUEST_RUNNER
    if runner is None:
        asyncio.run(coro)
        return True
    if console is not None:
        runner.console = console
    return runner.run(coro)


def _report_request_error(
    exc: Exception,
    console: Console,
    session_logger: SessionLogger | None,
) -> None:
    """Last-resort handler: one failed request must never kill the whole REPL.

    Before this existed, every ledger-tracked handler re-raised after logging
    and `main()` had no outer catch - a single Ollama stall or handler bug took
    down the entire session, losing plan mode and pending state (gap A1).
    KeyboardInterrupt/SystemExit are not `Exception` subclasses, so Ctrl+C and
    /exit keep their existing behavior untouched."""
    if isinstance(exc, LLMStalledError):
        body = (
            f"{exc}\n\n"
            "The model stopped producing output. Check that Ollama is still running "
            "(`ollama ps`), or raise SHAMSU_LLM_IDLE_TIMEOUT if this machine is just slow."
        )
    else:
        body = (
            f"{type(exc).__name__}: {exc}\n\n"
            "That request failed, but the session is fine - you can keep working. "
            "The full traceback is in the session log."
        )
    console.print(Panel(body, title="Request failed", border_style="red"))
    if session_logger is not None:
        try:
            session_logger.log(
                "request.failed",
                {
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "traceback": traceback.format_exc()[-4000:],
                },
                f"Request failed: {exc}",
                workflow_id="repl",
            )
        except Exception:
            pass


def _continuation_clarification(user_input: str, previous_prompt: str) -> str | None:
    """A bare 'yes'/'continue' with no pending question and nothing prior to
    continue should not invent a task - ask what to continue instead."""
    kind = classify_reply(user_input)
    if kind in {"affirmative", "continue"} and not previous_prompt.strip():
        return "There's nothing pending to continue. What would you like me to do?"
    return None


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
    if command == "raw":
        _set_trace_mode(workspace, "raw")
        console.print(
            "[green]Trace mode is raw - shows route, plan, tool calls/args, tool outputs, "
            "and raw model-visible content.[/green]"
        )
        return
    if command == "normal":
        _set_trace_mode(workspace, "normal")
        console.print("[green]Trace mode is normal.[/green]")
        return
    if command == "status":
        console.print(f"Trace mode: [bold]{_trace_mode(workspace)}[/bold]")
        return
    console.print("[red]Usage: trace status|on|off|normal|verbose|raw[/red]")


def _handle_debug(user_input: str, workspace: Path, console: Console) -> None:
    """`/debug on|off` toggles a rich trace (verbose) on or off - a friendly
    alias over the trace mode so users don't have to remember trace levels."""
    parts = user_input.split(maxsplit=1)
    command = parts[1].strip().lower() if len(parts) > 1 else "status"
    if command == "on":
        _set_trace_mode(workspace, "verbose")
        console.print(
            "[green]Debug on: route, model, plan, tool calls, tool outputs, and file "
            "changes will be shown. Use `/trace raw` for raw model content.[/green]"
        )
        return
    if command == "off":
        _set_trace_mode(workspace, "normal")
        console.print("[yellow]Debug off (trace mode: normal).[/yellow]")
        return
    if command == "status":
        console.print(f"Trace mode: [bold]{_trace_mode(workspace)}[/bold]")
        return
    console.print("[red]Usage: debug on|off|status[/red]")


def _audit_root(workspace: Path) -> Path:
    return workspace / ".shamsu" / "audit"


def _read_jsonl(path: Path, limit: int | None = None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    if limit is not None:
        return records[-limit:]
    return records


def _format_audit_event(event: dict[str, Any]) -> str:
    ts = str(event.get("timestamp", ""))[:19]
    etype = str(event.get("event_type", "?"))
    message = str(event.get("message", "")).strip()
    extras = ""
    if etype == "tool.call":
        extras = (
            f" {event.get('tool', '')} {json.dumps(event.get('arguments', {}), default=str)[:200]}"
        )
    elif etype == "tool.result":
        extras = f" {event.get('tool', '')} ok={event.get('ok')}"
    elif etype == "file.change":
        extras = f" {event.get('action', '')} {event.get('filepath', '')}"
    elif etype == "route.selected":
        extras = f" {event.get('route', '')}"
    return f"[dim]{ts}[/dim] [cyan]{etype}[/cyan] {message}{extras}".rstrip()


def _handle_audit_log(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    """Inspect the detailed per-step audit trail under .shamsu/audit/.

    Subcommands: tail [n], show <session-id>, grep <query>, export [path], open.
    """
    parts = user_input.split(maxsplit=2)
    sub = parts[1].strip().lower() if len(parts) > 1 else "tail"
    arg = parts[2].strip() if len(parts) > 2 else ""
    root = _audit_root(workspace)
    events_path = root / "events.jsonl"

    if sub == "tail":
        limit = int(arg) if arg.isdigit() else 40
        events = _read_jsonl(events_path, limit=limit)
        if not events:
            console.print("[dim]No audit events recorded yet.[/dim]")
            return
        for event in events:
            console.print(_format_audit_event(event))
        return

    if sub == "show":
        if not arg:
            console.print("[red]Usage: audit-log show <session-id>[/red]")
            return
        session_path = root / "sessions" / f"{arg}.jsonl"
        events = _read_jsonl(session_path)
        if not events:
            console.print(f"[yellow]No audit events for session {arg}.[/yellow]")
            return
        for event in events:
            console.print(_format_audit_event(event))
        return

    if sub == "grep":
        if not arg:
            console.print("[red]Usage: audit-log grep <query>[/red]")
            return
        needle = arg.lower()
        matches = [
            event
            for event in _read_jsonl(events_path)
            if needle in json.dumps(event, default=str).lower()
        ]
        if not matches:
            console.print(f"[dim]No audit events matched '{arg}'.[/dim]")
            return
        for event in matches[-100:]:
            console.print(_format_audit_event(event))
        return

    if sub == "export":
        target = Path(arg) if arg else (workspace / "audit-export.jsonl")
        events = _read_jsonl(events_path)
        try:
            target.write_text(
                "\n".join(json.dumps(event, default=str) for event in events) + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            console.print(f"[red]Could not export audit log: {exc}[/red]")
            return
        console.print(f"[green]Exported {len(events)} audit event(s) to {target}[/green]")
        return

    if sub == "open":
        console.print(f"Audit log directory: [bold]{root}[/bold]")
        console.print(f"  events:   {events_path}")
        console.print(f"  sessions: {root / 'sessions'}")
        console.print(f"  artifacts:{root / 'artifacts'}")
        console.print(f"  context:  {root / 'context-packs'}")
        return

    console.print(
        "[red]Usage: audit-log tail [n] | show <session-id> | grep <query> | export [path] | open[/red]"
    )


# -- ActionLedger CLI (/runs, /run): local human-facing debug/audit trail. --
# See agent context/prompts/audit_log.md. This inspects <workspace>/.shamsu/runs/
# only - never Graphiti, never Codebase-Memory MCP, never fed back into a model.


def _handle_runs(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=1)
    limit = 20
    if len(parts) > 1 and parts[1].strip().isdigit():
        limit = int(parts[1].strip())
    runs = action_ledger_store.list_runs(workspace, limit=limit)
    if not runs:
        console.print("[dim]No runs recorded yet.[/dim]")
        return
    table = Table(title="ActionLedger Runs")
    table.add_column("Run ID")
    table.add_column("Started")
    table.add_column("Status")
    table.add_column("Prompt")
    for item in runs:
        table.add_row(item.run_id, item.started_at, item.status, item.prompt_preview[:80])
    console.print(table)


def _resolve_run_or_print_error(workspace: Path, query: str, console: Console) -> str | None:
    run_id = action_ledger_store.resolve_run_id(workspace, query)
    if not run_id:
        console.print(f"[red]No run found for: {query or 'last'}[/red]")
        return None
    return run_id


def _print_run_summary(workspace: Path, run_id: str, console: Console) -> None:
    manifest = action_ledger_store.load_manifest(workspace, run_id) or {}
    summary = action_ledger_store.load_summary(workspace, run_id) or {}
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
    ]
    console.print(Panel("\n".join(lines), title=f"Run {run_id}"))


def _handle_run(
    user_input: str,
    workspace: Path,
    console: Console,
    approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
) -> None:
    _, _, rest = user_input.partition(" ")
    parts = rest.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else "last"
    argument = parts[1].strip() if len(parts) > 1 else ""

    if subcommand == "last":
        run_id = action_ledger_store.resolve_run_id(workspace, "last")
        if not run_id:
            console.print("[dim]No runs recorded yet.[/dim]")
            return
        _print_run_summary(workspace, run_id, console)
        return

    if subcommand == "show":
        run_id = _resolve_run_or_print_error(workspace, argument, console)
        if not run_id:
            return
        _print_run_summary(workspace, run_id, console)
        return

    if subcommand == "timeline":
        run_id = _resolve_run_or_print_error(workspace, argument, console)
        if not run_id:
            return
        events = action_ledger_store.load_events(workspace, run_id)
        if not events:
            console.print("[dim]No events recorded for this run.[/dim]")
            return
        table = Table(title=f"Run Timeline: {run_id}")
        table.add_column("Time")
        table.add_column("Event")
        table.add_column("Detail")
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
        run_id = _resolve_run_or_print_error(workspace, argument, console)
        if not run_id:
            return
        decisions = action_ledger_store.load_decisions(workspace, run_id)
        if not decisions:
            console.print("[dim]No decisions recorded for this run.[/dim]")
            return
        table = Table(title=f"Run Decisions: {run_id}")
        table.add_column("Decision")
        table.add_column("Reason")
        table.add_column("Chosen Action")
        table.add_column("Outcome")
        for decision in decisions:
            table.add_row(
                decision.get("decision", ""),
                decision.get("reason_summary", ""),
                decision.get("chosen_action", ""),
                str(decision.get("outcome", "")),
            )
        console.print(table)
        return

    if subcommand == "tools":
        run_id = _resolve_run_or_print_error(workspace, argument, console)
        if not run_id:
            return
        tool_calls = action_ledger_store.load_tool_calls(workspace, run_id)
        if not tool_calls:
            console.print("[dim]No tool calls recorded for this run.[/dim]")
            return
        table = Table(title=f"Run Tool Calls: {run_id}")
        table.add_column("Tool")
        table.add_column("Phase")
        table.add_column("Outcome")
        for call in tool_calls:
            outcome = (
                "ok" if call.get("ok") else ("failed" if call.get("phase") == "finished" else "")
            )
            table.add_row(call.get("tool", ""), call.get("phase", ""), outcome)
        console.print(table)
        return

    if subcommand == "commands":
        run_id = _resolve_run_or_print_error(workspace, argument, console)
        if not run_id:
            return
        events = action_ledger_store.command_events(workspace, run_id)
        finished = [event for event in events if event.get("type") == "command_finished"]
        if not finished:
            console.print("[dim]No commands recorded for this run.[/dim]")
            return
        table = Table(title=f"Run Commands: {run_id}")
        table.add_column("Command")
        table.add_column("Exit")
        table.add_column("Stdout")
        table.add_column("Stderr")
        for event in finished:
            table.add_row(
                str(event.get("command", ""))[:60],
                str(event.get("exit_code", "")),
                event.get("stdout_path", ""),
                event.get("stderr_path", ""),
            )
        console.print(table)
        return

    if subcommand == "context":
        run_id = _resolve_run_or_print_error(workspace, argument, console)
        if not run_id:
            return
        previews = action_ledger_store.load_context_records(workspace, run_id)
        if not previews:
            console.print("[dim]No context preview recorded for this run.[/dim]")
            return
        console.print(
            Panel(json.dumps(previews, indent=2, default=str), title=f"Contexts: {run_id}")
        )
        return

    if subcommand == "diff":
        run_id = _resolve_run_or_print_error(workspace, argument, console)
        if not run_id:
            return
        mutations = action_ledger_store.load_mutations(workspace, run_id)
        if not mutations:
            console.print("[dim]No patches/mutations recorded for this run.[/dim]")
            return
        table = Table(title=f"Run Mutations: {run_id}")
        table.add_column("Transaction")
        table.add_column("Status")
        table.add_column("Files")
        table.add_column("Rollback")
        for mutation in mutations:
            table.add_row(
                mutation.get("transaction_id", ""),
                mutation.get("status", ""),
                str(len(mutation.get("touched_files", []))),
                str(mutation.get("rollback_available", False)),
            )
        console.print(table)
        return

    if subcommand == "export":
        run_id = _resolve_run_or_print_error(workspace, argument, console)
        if not run_id:
            return
        zip_path = action_ledger_store.export_run(workspace, run_id)
        console.print(f"[green]Exported run to {zip_path}[/green]")
        return

    if subcommand == "validate":
        run_id = _resolve_run_or_print_error(workspace, argument, console)
        if not run_id:
            return
        result = action_ledger_store.validate_run(workspace, run_id)
        color = "green" if result["ok"] else "red"
        lines = [f"Integrity: {'valid' if result['ok'] else 'invalid'}"]
        lines.extend(f"Error: {item}" for item in result["errors"])
        lines.extend(f"Warning: {item}" for item in result["warnings"])
        console.print(
            Panel("\n".join(lines), title=f"Run Validation: {run_id}", border_style=color)
        )
        return

    if subcommand == "clean":
        config = load_action_ledger_config(workspace)
        retention_days = int(config.get("retention_days", 30))
        stale = action_ledger_store.runs_older_than(workspace, retention_days)
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
            target_paths=[f".shamsu/runs/{run_id}" for run_id in stale],
        )
        if not approval_func(request):
            console.print("[yellow]Clean cancelled; no runs were deleted.[/yellow]")
            return
        removed = action_ledger_store.clean_runs(workspace, retention_days)
        console.print(
            f"[green]Removed {len(removed)} run(s) older than {retention_days} day(s).[/green]"
        )
        return

    console.print(
        "[red]Usage: /run last|show|timeline|decisions|tools|commands|context|diff|"
        "validate|export|clean [run-id][/red]"
    )


# Temporary compatibility re-exports while callers migrate from repl.py.
_handle_runs = _modular_handle_runs
_handle_run = _modular_handle_run
_handle_logs = _modular_handle_logs


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
        console.print(
            "[green]Diagnostics setup complete.[/green]"
            if result.get("ok")
            else f"[yellow]Diagnostics setup finished with issues: {result}[/yellow]"
        )
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
            console.print(
                "[yellow]No ErrorPacket recorded yet. Run a command first (e.g. a build/test).[/yellow]"
            )
            return
        if subcommand == "last":
            console.print(f"[bold]{packet.get('summary', '')}[/bold]")
            console.print(f"Command: {packet.get('command', '')} (exit {packet.get('exit_code')})")
            for record in packet.get("root_diagnostics", []):
                location = (
                    f"{record.get('file', '')}:{record.get('line', '')}"
                    if record.get("file")
                    else ""
                )
                console.print(
                    f"- [{record.get('category')}] {record.get('code', '')} {location} {record.get('message', '')}".strip()
                )
            if packet.get("target_files"):
                console.print("Target files: " + ", ".join(packet["target_files"]))
            for snippet in packet.get("recommended_snippets", []):
                console.print(
                    f"Recommended snippet: {snippet['file']} lines {snippet['line_start']}-{snippet['line_end']} ({snippet['reason']})"
                )
            return
        if subcommand == "sources":
            console.print(
                "Parser chain: "
                + (", ".join(packet.get("parser_chain", [])) or "none (no diagnostics extracted)")
            )
            return
        if subcommand == "explain":
            root = packet.get("root_diagnostics", [])
            if not root:
                console.print(
                    "No root diagnostic was selected (command succeeded or nothing was extracted)."
                )
                return
            category = root[0].get("category", "")
            reason = _ROOT_CAUSE_EXPLANATIONS.get(
                category,
                "no higher-priority category matched, so this was the first diagnostic after deduping/grouping.",
            )
            console.print(f"Root cause selection: {reason}")
            console.print(
                f"Diagnostic: [{category}] {root[0].get('code', '')} {root[0].get('message', '')}"
            )
            return
        if subcommand == "parse":
            reparsed = _reparse_last_command(workspace, ws)
            if reparsed is None:
                console.print(
                    "[yellow]No recent command output found in session logs to re-parse.[/yellow]"
                )
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


def _handle_undo(
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    """Roll back the most recent file change (the `/undo` command).

    Sugar over `/patch rollback <id>`: every model-driven write already opened a
    transaction with a backup, but using it meant knowing the command existed
    AND finding the id under `.shamsu/mutations` - which nobody does in the
    moment the agent just mangled their code (gap G2). Each write is its own
    transaction, so this undoes the LAST change, not a whole run; the message
    says so rather than over-promising."""
    latest = latest_undoable_transaction(workspace)
    if latest is None:
        console.print(
            "[yellow]Nothing to undo - no file changes are recorded for this workspace yet.[/yellow]"
        )
        return
    transaction_id, manifest = latest
    touched = sorted((manifest.get("backups") or {}).keys()) or sorted(
        str(op.get("path", "")) for op in (manifest.get("operations") or [])
    )
    listing = "\n".join(f"- {path}" for path in touched[:10] if path)
    console.print(
        Panel(
            f"Most recent change ({transaction_id}):\n{listing or '- (no files recorded)'}",
            title="Undo",
            border_style="yellow",
        )
    )
    engine = PatchEngine(
        workspace,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
    )
    request = ApprovalRequest(
        action_type="file_delete",
        description=f"Undo transaction {transaction_id} (restore {len(touched)} file(s)).",
        risk_level="high",
        working_dir=str(workspace),
        reason="Undo restores backed-up files, overwriting current content.",
        target_paths=touched,
    )
    if not engine.approval_manager.ask(request):
        console.print("[yellow]Undo cancelled - nothing was changed.[/yellow]")
        return
    ok, message = engine.rollback_transaction(transaction_id)
    if ok:
        console.print(f"[green]{message}[/green]")
        console.print(
            "[dim]Undo again to step back further, or `/patch list` to see all changes.[/dim]"
        )
    else:
        console.print(f"[red]{message}[/red]")
    _log_assistant_message(session_logger, message, workflow_id="undo")


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
        action_ledger=get_current_run(),
    )

    if subcommand == "status":
        last = engine.journal.last()
        trash_count = len(engine.trash.list_entries())
        console.print(f"git apply available: {patch_git_apply.available(workspace)}")
        console.print(f"Trashed file(s): {trash_count}")
        if last:
            console.print(
                f"Last transaction: {last['transaction_id']} ({last['status']}) - {last['reason']}"
            )
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
        manifest = engine.transactions.load_manifest(argument) or {}
        request = ApprovalRequest(
            action_type="file_delete",
            description=f"Roll back transaction {argument}.",
            risk_level="high",
            working_dir=str(workspace),
            reason="Rollback restores backed-up files, overwriting current content.",
            target_paths=[str(path) for path in manifest.get("touched_files", [])],
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
            console.print(
                f"Verification: `{verification.get('command')}` exit {verification.get('exit_code')}"
            )
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
            target_paths=[item.relative_path for item in entries],
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
        console.print(
            f"[red]Change request must be JSON matching the change_plan/patch contract: {exc}[/red]"
        )
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
        console.print(
            Panel(
                detail,
                title=f"Patch Rejected ({result.transaction_id or 'no transaction'})",
                border_style="red",
            )
        )


def _handle_milestones(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=2)
    command = parts[1].strip().lower() if len(parts) > 1 else "list"
    if command == "list":
        task_ids = list_task_ids(workspace)
        if not task_ids:
            console.print("[dim]No tracked milestones for this workspace.[/dim]")
            return
        table = Table(title="Milestones")
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
                task.task_id,
                task.phase,
                str(pending),
                str(len(task.blocked_steps)),
                task.next_action or "-",
            )
        console.print(table)
        return
    if command == "show":
        if len(parts) < 3:
            console.print("[red]Usage: milestones show <id>[/red]")
            return
        try:
            task = load_task(workspace, parts[2].strip())
        except (OSError, ValueError) as exc:
            console.print(f"[red]Could not load task {parts[2].strip()}: {exc}[/red]")
            return
        _print_task(task, console)
        return
    console.print("[red]Usage: milestones list|show <id>[/red]")


def _print_tasks_table(service: "TaskmasterService", console: Console) -> None:
    listing = service.list_tasks()
    if not listing.get("ok"):
        console.print(f"[red]{listing.get('error') or 'Could not list Taskmaster tasks.'}[/red]")
        return
    tasks = listing.get("tasks", [])
    if not tasks:
        console.print("[dim]No Taskmaster tasks yet. Run /prd parse <file> first.[/dim]")
        return
    table = Table(title="Taskmaster Tasks")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Priority")
    table.add_column("Dependencies")
    for task in tasks:
        table.add_row(
            task.id,
            task.title,
            task.status,
            task.priority or "-",
            ", ".join(task.dependencies) or "-",
        )
    console.print(table)


def _print_task_detail(task: "TaskmasterTask", console: Console) -> None:
    lines = [
        f"Title: {task.title}",
        f"Status: {task.status}",
        f"Priority: {task.priority or 'n/a'}",
        f"Dependencies: {', '.join(task.dependencies) or 'none'}",
    ]
    if task.description:
        lines.append(f"Description: {task.description}")
    if task.details:
        lines.append(f"Details: {task.details}")
    if task.test_strategy:
        lines.append(f"Test strategy: {task.test_strategy}")
    console.print(Panel("\n".join(lines), title=f"Task {task.id}"))


def _print_prd_task_summary(result: dict, console: Console) -> None:
    if not result.get("ok"):
        console.print(f"[red]PRD parse failed: {result.get('error') or 'unknown error'}[/red]")
        return
    if result.get("reused_cache"):
        console.print(
            "[dim]PRD unchanged - reusing the cached Taskmaster task graph (no reparse).[/dim]"
        )
    _print_tasks_table_from_list(result.get("tasks", []), console)


def _print_tasks_table_from_list(tasks: list, console: Console) -> None:
    if not tasks:
        console.print("[yellow]No tasks were generated.[/yellow]")
        return
    table = Table(title="Taskmaster Tasks")
    table.add_column("ID")
    table.add_column("Title")
    table.add_column("Status")
    table.add_column("Priority")
    table.add_column("Dependencies")
    for task in tasks:
        table.add_row(
            task.id,
            task.title,
            task.status,
            task.priority or "-",
            ", ".join(task.dependencies) or "-",
        )
    console.print(table)


def _handle_taskmaster(user_input: str, workspace: Path, console: Console) -> None:
    _, _, rest = user_input.partition(" ")
    subcommand = rest.strip().split(maxsplit=1)[0].lower() if rest.strip() else "status"
    service = TaskmasterService(workspace)

    if subcommand == "status":
        status = service.status()
        console.print(f"Available: {status.health.available} ({status.health.message})")
        console.print(f"Node: {status.health.node_path or 'not found'}")
        console.print(f"Taskmaster CLI: {status.health.cli_path or 'not installed'}")
        console.print(f"Managed tool path: {service.adapter.tool_dir}")
        console.print(f"Workspace initialized: {status.initialized}")
        if status.initialized:
            console.print(f"Tasks: {status.task_count} {status.status_counts or ''}")
        console.print("Cloud model providers: rejected by default (Ollama-only).")
        return
    if subcommand == "setup":
        result = service.setup()
        if result.get("ok"):
            console.print(
                "[green]Taskmaster setup complete (local Ollama provider configured).[/green]"
            )
        else:
            reason = result.get("error") or result.get("manual_steps") or result
            console.print(f"[red]Taskmaster setup failed: {reason}[/red]")
        return
    if subcommand == "repair":
        result = service.repair()
        if result.get("ok"):
            console.print("[green]Taskmaster repair complete.[/green]")
        else:
            reason = result.get("manual_steps") or result.get("message") or result
            console.print(f"[red]Taskmaster repair failed: {reason}[/red]")
        return
    console.print("[red]Usage: /taskmaster status|setup|repair[/red]")


def _handle_prd_command(user_input: str, workspace: Path, console: Console) -> None:
    _, _, rest = user_input.partition(" ")
    parts = rest.strip().split(maxsplit=1)
    subcommand = parts[0].lower() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""
    service = TaskmasterService(workspace)

    if subcommand != "status":
        ready, reason = service.ensure_ready()
        if not ready:
            console.print(Panel(reason, title="Taskmaster Unavailable", border_style="yellow"))
            return

    if subcommand == "parse":
        if not argument:
            console.print("[red]Usage: /prd parse <file>[/red]")
            return
        try:
            prd_path = _resolve_workspace_file(argument, workspace)
        except SecurityError as exc:
            console.print(f"[red]{exc}[/red]", soft_wrap=True)
            return
        if not prd_path.exists() or not prd_path.is_file():
            console.print(f"[red]File not found: {prd_path}[/red]")
            return
        result = service.parse_prd(prd_path)
        _log_prd_parse_result(prd_path, result)
        _print_prd_task_summary(result, console)
        _log_assistant_message(None, _prd_parse_summary_message(result), workflow_id="prd_parse")
        return
    if subcommand == "reparse":
        cached = service.last_prd_info()
        prd_path_str = argument or cached.get("prd_path", "")
        if not prd_path_str:
            console.print(
                "[red]No previously parsed PRD to reparse. Usage: /prd reparse [file][/red]"
            )
            return
        result = service.parse_prd(Path(prd_path_str), force=True)
        _log_prd_parse_result(Path(prd_path_str), result)
        _print_prd_task_summary(result, console)
        _log_assistant_message(None, _prd_parse_summary_message(result), workflow_id="prd_reparse")
        return
    if subcommand == "status":
        status = service.status()
        cached = service.last_prd_info()
        console.print(f"Taskmaster available: {status.health.available} ({status.health.message})")
        console.print(f"Last parsed PRD: {cached.get('prd_path', 'none')}")
        console.print(f"Tasks: {status.task_count} {status.status_counts or ''}")
        return
    console.print("[red]Usage: /prd parse <file>|status|reparse [file][/red]")


def _prd_parse_summary_message(result: dict) -> str:
    if not result.get("ok"):
        return f"PRD parse failed: {result.get('error') or 'unknown error'}"
    if result.get("reused_cache"):
        return "PRD unchanged; reused the cached Taskmaster task graph."
    return f"Parsed PRD into {len(result.get('tasks', []))} Taskmaster task(s)."


def _log_prd_parse_result(prd_path: Path, result: dict) -> None:
    ledger = get_current_run()
    if not ledger:
        return
    ledger.log_event(
        "prd.parsed",
        prd_path=str(prd_path),
        ok=bool(result.get("ok")),
        reused_cache=bool(result.get("reused_cache")),
    )
    if result.get("ok") and not result.get("reused_cache"):
        ledger.log_event("tasks.created", count=len(result.get("tasks", [])))


def _parse_task_execute_args(rest: str) -> tuple[str, str, bool]:
    """Pull `--verify "<command>"` and `--all` out of the raw argument text,
    leaving whatever's left as the task id (only meaningful for `execute`)."""
    verify_command = ""
    match = re.search(r'--verify[= ]"([^"]*)"', rest)
    if match:
        verify_command = match.group(1)
        rest = rest[: match.start()] + rest[match.end() :]
    batch = "--all" in rest
    rest = rest.replace("--all", "").strip()
    task_id = rest.split()[0] if rest.split() else ""
    return task_id, verify_command, batch


def _print_task_execution_result(result: "TaskExecutionResult", console: Console) -> None:
    border = {
        "done": "green",
        "blocked": "yellow",
        "applied_unverified": "yellow",
        "failed": "red",
        "error": "red",
    }.get(result.status, "white")
    body = result.message or result.error or f"Task {result.task_id}: {result.status}"
    if result.changed_files:
        body += "\n\nChanged files:\n" + "\n".join(f"- {path}" for path in result.changed_files)
    if result.verification is not None and result.verification.ran:
        body += f"\n\nVerification: `{result.verification.command}` exit {result.verification.exit_code}"
    console.print(Panel(body, title=f"Task {result.task_id}: {result.status}", border_style=border))


async def _handle_tasks_execute(
    user_input: str,
    workspace: Path,
    console: Console,
    search: SearchAgent | EmptySearchAgent,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
) -> None:
    parts = user_input.split(maxsplit=2)
    command = parts[1].strip().lower()
    rest = parts[2] if len(parts) > 2 else ""
    task_id_arg, verify_command, batch = _parse_task_execute_args(rest)

    service = TaskmasterService(workspace)
    ready, reason = service.ensure_ready()
    if not ready:
        console.print(Panel(reason, title="Taskmaster Unavailable", border_style="yellow"))
        return

    if command == "execute" and not task_id_arg:
        console.print('[red]Usage: /tasks execute <id> [--verify "<command>"][/red]')
        return
    if batch and not is_long_running_enabled(workspace):
        console.print(
            "[red]Batch execution requires explicit approval. Run /autonomy on first, "
            "or use /tasks continue (without --all) to execute one task at a time.[/red]"
        )
        return

    _warn_if_dirty_before_edit(workspace, console)
    approval_manager = _make_approval_manager(workspace, session_logger, console)
    action_ledger = get_current_run()
    patch_engine = PatchEngine(
        workspace,
        session_logger=session_logger,
        approval_manager=approval_manager,
        action_ledger=action_ledger,
    )
    command_runner = CommandRunner(
        workspace,
        session_logger=session_logger,
        approval_manager=approval_manager,
        action_ledger=action_ledger,
    )
    workflow = TaskExecutionWorkflow(
        workspace,
        search=search,
        service=service,
        memory_service=MemoryService(workspace),
        abstract_service=AbstractService(workspace),
        code_edit_workflow=CodeEditWorkflow(
            workspace, search=search, llm=llm, patch_engine=patch_engine
        ),
        command_runner=command_runner,
    )

    summaries: list[str] = []
    while True:
        if command == "execute":
            current_id = task_id_arg
        else:
            next_result = service.next_task()
            if not next_result.get("ok"):
                console.print(f"[red]{next_result.get('error')}[/red]")
                break
            next_task = next_result.get("task")
            if next_task is None:
                if not summaries:
                    console.print("[dim]No unblocked Taskmaster task available.[/dim]")
                break
            current_id = next_task.id

        console.print(f"[dim]Executing task {current_id}...[/dim]")
        if action_ledger:
            action_ledger.log_event("task.selected", task_id=current_id)
        result = await workflow.run(current_id, verify_command=verify_command)
        if action_ledger:
            action_ledger.log_event(
                "task.execution_finished", task_id=current_id, status=result.status
            )
        _print_task_execution_result(result, console)
        summaries.append(f"Task {current_id}: {result.status}")

        if command == "execute" or not batch:
            break
        if result.status != "done":
            console.print(
                "[yellow]Stopping batch execution: task did not complete successfully.[/yellow]"
            )
            break

    if summaries:
        _log_assistant_message(session_logger, "; ".join(summaries), workflow_id="tasks_execute")


def _handle_tasks(user_input: str, workspace: Path, console: Console) -> None:
    parts = user_input.split(maxsplit=2)
    command = parts[1].strip().lower() if len(parts) > 1 else "list"
    service = TaskmasterService(workspace)
    ready, reason = service.ensure_ready()
    if not ready:
        console.print(Panel(reason, title="Taskmaster Unavailable", border_style="yellow"))
        return

    if command in {"list", ""}:
        _print_tasks_table(service, console)
        return
    if command == "next":
        result = service.next_task()
        if not result.get("ok"):
            console.print(f"[red]{result.get('error')}[/red]")
            return
        task = result.get("task")
        if task is None:
            console.print("[dim]No unblocked task available.[/dim]")
            return
        _print_task_detail(task, console)
        return
    if command == "show":
        if len(parts) < 3:
            console.print("[red]Usage: /tasks show <id>[/red]")
            return
        result = service.show_task(parts[2].strip())
        if not result.get("ok"):
            console.print(f"[red]{result.get('error')}[/red]")
            return
        _print_task_detail(result["task"], console)
        return
    if command == "dependencies":
        if len(parts) < 3:
            console.print("[red]Usage: /tasks dependencies <id>[/red]")
            return
        result = service.dependencies(parts[2].strip())
        if not result.get("ok"):
            console.print(f"[red]{result.get('error')}[/red]")
            return
        deps = result.get("dependencies", [])
        if not deps:
            console.print("[dim]No dependencies.[/dim]")
            return
        for dep in deps:
            console.print(f"- {dep['id']}: {dep['status']}")
        return
    if command == "plan":
        result = service.plan()
        if not result.get("ok"):
            console.print(f"[red]{result.get('error')}[/red]")
            return
        table = Table(title="Execution Plan")
        table.add_column("ID")
        table.add_column("Title")
        table.add_column("Status")
        table.add_column("Blocked By")
        table.add_column("Executable")
        for row in result["tasks"]:
            table.add_row(
                row["id"],
                row["title"],
                row["status"],
                ", ".join(row["blocked_by"]) or "-",
                "yes" if row["executable"] else "no",
            )
        console.print(table)
        return
    if command == "mark-done":
        if len(parts) < 3:
            console.print("[red]Usage: /tasks mark-done <id>[/red]")
            return
        result = service.mark_done(parts[2].strip(), note="Explicitly accepted by user.")
        console.print(
            "[green]Marked done.[/green]"
            if result.get("ok")
            else f"[red]{result.get('error')}[/red]"
        )
        return
    if command == "mark-blocked":
        rest = parts[2] if len(parts) > 2 else ""
        task_id, _, reason_text = rest.partition(" ")
        if not task_id.strip() or not reason_text.strip():
            console.print("[red]Usage: /tasks mark-blocked <id> <reason>[/red]")
            return
        result = service.mark_blocked(task_id.strip(), reason_text.strip())
        console.print(
            "[yellow]Marked blocked.[/yellow]"
            if result.get("ok")
            else f"[red]{result.get('error')}[/red]"
        )
        return
    if command == "mark-failed":
        rest = parts[2] if len(parts) > 2 else ""
        task_id, _, reason_text = rest.partition(" ")
        if not task_id.strip() or not reason_text.strip():
            console.print("[red]Usage: /tasks mark-failed <id> <reason>[/red]")
            return
        result = service.mark_failed(task_id.strip(), reason_text.strip())
        if result.get("ok"):
            console.print(
                f"[yellow]Marked failed (retry {result.get('retry_count')}; now {result.get('next_status')}).[/yellow]"
            )
        else:
            console.print(f"[red]{result.get('error')}[/red]")
        return
    console.print(
        "[red]Usage: /tasks [list|next|show <id>|execute <id>|continue|mark-done <id>|"
        "mark-blocked <id> <reason>|mark-failed <id> <reason>|dependencies <id>|plan][/red]"
    )


def _handle_models(
    user_input: str,
    console: Console,
    workspace: Path | None = None,
) -> None:
    parts = user_input.split(maxsplit=1)
    command_raw = parts[1].strip() if len(parts) > 1 else "status"
    command_lower = command_raw.lower()
    if command_lower == "status":
        _print_runtime_status(console)
        return
    if command_lower == "tier" or command_lower.startswith("tier "):
        tier_arg = command_raw[len("tier") :].strip()
        _handle_models_tier(tier_arg, console, workspace)
        return
    if command_lower == "use" or command_lower.startswith("use "):
        model_arg = command_raw[len("use") :].strip()
        _handle_models_use(model_arg, console, workspace)
        return
    if command_lower == "pull":
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
        results = _pull_models_with_progress(
            Path(status.ollama_path), status.missing_models, console
        )
        _print_model_pull_results(results, console)
        _print_runtime_status(console)
        return
    if command_lower == "repair":
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
                "[cyan]Downloading missing local model(s):[/cyan] "
                + ", ".join(status.missing_models)
            )
            results = _pull_models_with_progress(
                Path(status.ollama_path), status.missing_models, console
            )
            _print_model_pull_results(results, console)
            status = collect_status(Path(status.ollama_path))
        _print_runtime_status(console, status=status)
        return
    console.print("[red]Usage: models status|pull|repair|tier|use[/red]")


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
        requested = ModelTier(tier_arg.strip().lower())
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


def _handle_models_use(model_arg: str, console: Console, workspace: Path | None) -> None:
    if not model_arg:
        current = active_model_override()
        console.print(f"[cyan]Active model override:[/cyan] {current or 'none - using tier'}")
        console.print(f"[cyan]Tier model for qa/coder:[/cyan] {model_for_role('qa')}")
        status = collect_status()
        if status.server_running and status.installed_models:
            console.print("[cyan]Installed models:[/cyan] " + ", ".join(status.installed_models))
        else:
            console.print("[yellow]Ollama is not running; installed model suggestions are unavailable.[/yellow]")
        console.print("Usage: /models use <installed-model>|tier")
        return
    if workspace is None:
        console.print("[red]No workspace available to persist the model choice.[/red]")
        return
    if model_arg.strip().lower() in {"tier", "tiers", "default", "off", "reset", "clear"}:
        clear_model_override(workspace)
        console.print(f"[green]Using {active_tier().value} tier model selection.[/green]")
        _print_runtime_status(console)
        return
    status = collect_status()
    if not status.ollama_found:
        console.print("[red]Ollama was not found. Install or repair Ollama before choosing a model.[/red]")
        return
    if not status.server_running:
        console.print("[red]Ollama is not running. Run `/models repair`, then try `/models use`.[/red]")
        return
    installed = set(status.installed_models)
    if model_arg not in installed:
        console.print(f"[red]Model is not installed: {model_arg}[/red]")
        if status.installed_models:
            console.print("[cyan]Installed models:[/cyan] " + ", ".join(status.installed_models))
        return
    set_model_override(workspace, model_arg)
    console.print(f"[green]Using installed model for all roles:[/green] {model_arg}")
    _print_runtime_status(console)


def _pull_missing_models_for_active_tier(console: Console) -> None:
    """Download whatever the currently active tier is missing, starting the
    local Ollama server first if needed. Shared by `/models tier <name>` and
    the first-run tier prompt - typing/answering either is the consent to
    download directly, no second approval gate."""
    status = collect_status()
    if not status.ollama_found:
        console.print(
            "[yellow]Ollama was not found. Run `models repair` once Ollama is installed.[/yellow]"
        )
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
    service = getattr(web_tool, "service_manager", WebServiceManager(Path.cwd()))
    if command == "setup":
        _print_web_service_status(service.setup(), console)
        return
    if command == "status":
        _print_web_capability_status(web_tool.status(), console)
        return
    if command == "start":
        _print_web_service_status(service.start(), console)
        return
    if command == "stop":
        _print_web_service_status(service.stop(), console)
        return
    if command == "restart":
        _print_web_service_status(service.restart(), console)
        return
    if command == "search":
        if not argument:
            console.print("[red]Usage: web search <query>[/red]")
            return
        result = web_tool.search_and_fetch(
            argument,
            reason="User explicitly requested a sourced web search.",
        )
        _run_request(_print_web_answer(argument, result, result.pages, console, llm))
        return
    if command in {"open", "summarize"}:
        if not argument:
            console.print("[red]Usage: web open <url>[/red]")
            return
        fetch = web_tool.fetch(argument, reason="User explicitly requested a web page fetch.")
        _print_web_fetch(fetch, console)
        return
    console.print(
        "[red]Usage: web setup|status|start|stop|restart|search <query>|open <url>|summarize <url>[/red]"
    )


def _print_web_service_status(status, console: Console) -> None:
    style = "green" if status.ok else "yellow"
    state = getattr(status, "state", "") or (
        "running" if getattr(status, "running", False) else "not_running"
    )
    console.print(
        Panel(f"{status.message}\nStatus: {state}", title="Web Search Service", border_style=style)
    )


def _print_web_capability_status(status, console: Console) -> None:
    table = Table(title="Web Capabilities")
    table.add_column("Capability")
    table.add_column("State")
    table.add_column("Detail")
    table.add_row(
        "Web", "enabled" if status.enabled else "disabled", f"mode={status.provider_mode}"
    )
    table.add_row("SearXNG", status.searxng.state, status.searxng.message)
    table.add_row("Fallback search", status.fallback_state, "DuckDuckGo HTML provider")
    table.add_row(
        "Page fetch", status.fetch_state, "Public HTTP/HTTPS only; local/private targets blocked"
    )
    table.add_row("Cache", status.cache_state, status.cache_path)
    console.print(table)


def _handle_browse(
    user_input: str,
    console: Console,
    browser_tool: BrowserTool,
) -> None:
    parts = user_input.split(maxsplit=3)
    command = parts[1].strip().lower() if len(parts) > 1 else ""
    if command == "status":
        status = browser_tool.status()
        style = "green" if status.available else "yellow"
        detail = status.message
        if status.executable_path:
            detail = f"{detail}\nExecutable: {status.executable_path}"
        console.print(
            Panel(
                f"State: {status.state}\n{detail}", title="Browser Capability", border_style=style
            )
        )
        return
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
    console.print("[red]Usage: browse status|open|read|click|type|screenshot[/red]")


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
        return ModelPullProgress(
            on_start=self.on_start, on_chunk=self.on_chunk, on_finish=self.on_finish
        )


_BUDGET_MANAGER_CACHE: dict[Path, ContextBudgetManager] = {}


def _get_budget_manager(workspace: Path, console: Console) -> ContextBudgetManager:
    resolved = workspace.resolve()
    mgr = _BUDGET_MANAGER_CACHE.get(resolved)
    if mgr is None:
        mgr = ContextBudgetManager(print_fn=console.print, workspace=resolved)
        _BUDGET_MANAGER_CACHE[resolved] = mgr
    else:
        mgr._print_fn = console.print
    return mgr


def _context_bucket_rows() -> list[str]:
    """The per-category split of the last simple-mode prompt, if there was one."""
    from shamsu.agents.simple_chat import LAST_ALLOCATION

    allocation = LAST_ALLOCATION.get("value")
    if allocation is None:
        return ["  (no simple-mode prompt built yet)"]
    total = max(1, allocation.total)
    rows = []
    for name, cost in sorted(allocation.buckets.items(), key=lambda kv: -kv[1]):
        bar = "#" * round(20 * cost / total)
        rows.append(f"  {name:<14} {cost:>7,}  {100 * cost // total:>3}%  {bar}")
    rows.append("  fattest        " + allocation.fattest())
    return rows


def _handle_context(
    normalized_input: str,
    workspace: Path,
    console: Console,
) -> None:
    from shamsu.context.budget import MODEL_CONTEXT_WINDOWS, SAFE_FALLBACK_CTX_WINDOW
    from shamsu.runtime.models import active_tier, tier_model_specs

    parts = normalized_input.split(maxsplit=2)
    sub = parts[1].lower() if len(parts) > 1 else ""
    mgr = _get_budget_manager(workspace, console)

    if sub == "status":
        tier = active_tier()
        rows: list[str] = [f"Active tier: {tier.value}", ""]
        for spec in tier_model_specs(tier):
            ctx = MODEL_CONTEXT_WINDOWS.get(spec.name, SAFE_FALLBACK_CTX_WINDOW)
            rows.append(
                f"  {spec.name:<35}  ctx {ctx // 1024}k  roles: {', '.join(spec.roles[:4])}"
            )
        rows.append("")
        calib = mgr._calibration
        if calib:
            rows.append("Calibration corrections (actual/estimated EMA):")
            for model, factor in calib.items():
                rows.append(f"  {model:<35}  ×{factor:.3f}")
        else:
            rows.append("No calibration data yet (accumulates after first model call).")
        console.print(Panel("\n".join(rows), title="Context Status", border_style="cyan"))

    elif sub == "meter":
        from shamsu.agents.simple_chat import SESSION_COUNTERS as counters

        if not counters.calls:
            console.print("[dim]No model calls yet this session.[/dim]")
            return
        drift = (
            f"{counters.last_prompt_tokens / counters.last_estimate:.2f}x"
            if counters.last_estimate
            else "n/a"
        )
        lines_out = [
            counters.meter(),
            "",
            f"  model calls        : {counters.calls}",
            f"  last prompt (real) : {counters.last_prompt_tokens:,} tokens"
            "   <- Ollama prompt_eval_count",
            f"  our estimate was   : {counters.last_estimate:,} tokens  (off by {drift})",
            "",
            f"  compactions        : {counters.compactions}",
            f"  payload elisions   : {counters.evictions}",
            f"  truncated replies  : {counters.truncations}",
            "",
            f"  total prompt       : {counters.total_prompt:,} tokens "
            f"(avg {counters.average_prompt:,}/call)",
            f"  total completion   : {counters.total_completion:,} tokens",
            f"  efficiency         : {counters.efficiency:.1f}%  "
            "[dim](completion per 100 prompt tokens - higher is better)[/dim]",
            "",
            "[dim]Where the last prompt went (one total tells you the window is",
            "full, never what filled it):[/dim]",
            *_context_bucket_rows(),
            "",
            "[dim]Compactions should be RARE. A count that climbs once per turn is",
            "the same-messages-every-turn bug, not healthy behaviour. Truncated",
            "replies should be zero.[/dim]",
        ]
        console.print(Panel(chr(10).join(lines_out), title="Context Meter", border_style="cyan"))

    elif sub in ("budget", "inspect"):
        result = mgr.last_result
        if result is None:
            console.print("[dim]No model calls made yet this session.[/dim]")
            return
        lines = [
            mgr.format_indicator(result),
            "",
            f"  model        : {result.model_name}",
            f"  specialist   : {result.specialist}",
            f"  estimated    : {result.estimated_tokens:,} tokens",
            f"  context window: {result.context_window:,} tokens",
            f"  usable       : {result.usable_tokens:,} tokens",
            f"  reserve      : {result.reserve_tokens:,} tokens",
            f"  usage        : {result.usage_pct}%",
            f"  compacted    : {result.compacted}",
        ]
        console.print(Panel("\n".join(lines), title="Context Budget", border_style="cyan"))

    elif sub == "compact":
        result = mgr.last_result
        threshold_pct = round(mgr._compact_threshold * 100)
        if result is None:
            console.print(
                f"[dim]Auto-compact threshold: {threshold_pct}%. "
                "No model calls made yet this session.[/dim]"
            )
        else:
            status = "triggered" if result.usage_pct >= threshold_pct else "not triggered"
            console.print(
                f"Auto-compact threshold: {threshold_pct}%  |  "
                f"Last call: {result.usage_pct}% used  |  "
                f"Status: {status}"
            )
            console.print(
                "[dim]Compaction runs automatically before each planner/coder call "
                "when usage exceeds the threshold. "
                "Exact code snippets, file paths, error codes, and imports are always preserved.[/dim]"
            )
    elif sub == "show":
        from shamsu.agents.chat_loop import _CHAT_MAX_CTX, _TOOL_RESULT_MAX_TOKENS
        from shamsu.ui.trace import read_trace_mode

        lines = [
            f"Trace mode         : {read_trace_mode(workspace)}",
            f"Chat context window: {_CHAT_MAX_CTX // 1024}k tokens (SHAMSU_CHAT_MAX_CTX)",
            f"Per-tool-result cap: {_TOOL_RESULT_MAX_TOKENS:,} tokens (SHAMSU_TOOL_RESULT_MAX_TOKENS)",
            "",
            "The working trace now surfaces (at 'normal' verbosity):",
            "  - Search : each search_index query + its top file hits and scores",
            "  - Reasoning : a dim one-line glimpse of the model's reasoning trace",
            "  - Verify : the deterministic verify verdict after writes",
            "",
            "Use `/trace verbose` for full args + reasoning, `/context budget` for the",
            "last model call's token usage, `/context status` for windows/calibration.",
        ]
        # Best-effort bookkeeping that failed silently this session (gap G3).
        # Zero is the healthy state; anything here means a side channel (audit
        # trail, session state, transcript) is broken and worth investigating.
        rows = swallowed.snapshot()
        lines.append("")
        if rows:
            lines.append(f"Swallowed bookkeeping errors this session: {swallowed.total()}")
            for where, count, last_error in rows[:8]:
                lines.append(f"  {where:<32} x{count}  last: {last_error}")
        else:
            lines.append("Swallowed bookkeeping errors this session: 0 (all side channels healthy)")
        console.print(Panel("\n".join(lines), title="Context & Observability", border_style="cyan"))

    else:
        console.print(
            "[red]Usage: /context status|budget|meter|inspect|compact|show[/red]"
        )


def _os_env_flag(name: str) -> bool:
    """True when an environment variable is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _call_accepts_keyword(callable_obj: Any, keyword: str) -> bool:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return True
    return any(
        param.kind is inspect.Parameter.VAR_KEYWORD or name == keyword
        for name, param in signature.parameters.items()
    )


def _make_llm_manager(
    session_logger: SessionLogger | None,
    console: Console,
    workspace: Path | None = None,
    *,
    lightweight: bool = False,
) -> LLMManager:
    """Build the specialist LLM manager. `lightweight=True` is for one-shot small
    talk: it drops the context-budget indicator and the reasoning-trace glimpse,
    so a greeting reply is a clean single line instead of a "ctx qa .../32.8k"
    header followed by a "Reasoning:" dump - nobody wants a chain-of-thought
    for "hi"."""
    lazy_progress = _LazyModelPullProgress(console)
    budget_manager = (
        None if lightweight or workspace is None else _get_budget_manager(workspace, console)
    )
    kwargs: dict[str, Any] = {
        "session_logger": session_logger,
        "model_pull_progress": lazy_progress.as_model_pull_progress(),
        "action_ledger": get_current_run(),
    }
    if not lightweight and _call_accepts_keyword(LLMManager, "on_activity"):
        kwargs["on_activity"] = lambda message: console.print(f"[dim]{message}[/dim]")
    if budget_manager is not None and _call_accepts_keyword(LLMManager, "budget_manager"):
        kwargs["budget_manager"] = budget_manager
    # Surface a reasoning model's chain-of-thought on the specialist path (QA,
    # PRD summary, planner, direct-code). The agent chat loop already shows its
    # own; without this, everything routed OUTSIDE the loop reasoned invisibly.
    if (
        not lightweight
        and workspace is not None
        and _call_accepts_keyword(LLMManager, "on_thinking")
    ):
        kwargs["on_thinking"] = _make_thinking_reporter(console, session_logger, workspace)
    return LLMManager(**kwargs)


def _make_thinking_reporter(
    console: Console, session_logger: SessionLogger | None, workspace: Path
) -> Callable[[str, str], None]:
    def report(model: str, thinking: str) -> None:
        emit_trace(
            console,
            session_logger,
            workspace,
            "assistant.thinking",
            _thinking_preview(thinking),
            {"model": model, "thinking_chars": len(thinking)},
            level="normal",
        )

    return report


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
            progress.update(
                task_id, description=f"{'Installed' if exit_code == 0 else 'Failed'} {model}"
            )
    return results


def _print_model_pull_results(results: dict[str, int], console: Console) -> None:
    for model, exit_code in results.items():
        if exit_code == 0:
            console.print(f"[green]{model}: installed[/green]")
        else:
            console.print(
                f"[red]{model}: failed with exit {exit_code}. Re-run `models pull` to resume.[/red]"
            )


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
    table.add_row("Model override", active_model_override() or "none")
    table.add_row("Active model", model_for_role("qa"))
    table.add_row("Endpoint", status.base_url)
    table.add_row("Ollama", status.ollama_path or "not found")
    table.add_row("Server", "running" if status.server_running else "not running")
    table.add_row("Missing models", ", ".join(status.missing_models) or "none")
    table.add_row("Status", status_text(status))
    console.print(table)


# How stale or how long an interactive session may get before a restart starts a
# fresh one. Deliberately looser than the headless bounds - picking work back up
# after lunch is normal - but not unbounded, which is what left one session per
# workspace running forever and feeding old work into every compiled frame.
_SESSION_MAX_AGE_SECONDS = _env_int_at_least("SHAMSU_SESSION_MAX_AGE_SECONDS", 8 * 3600, 60)
_SESSION_MAX_MESSAGES = _env_int_at_least("SHAMSU_SESSION_MAX_MESSAGES", 200, 10)


def _start_session(args: argparse.Namespace, workspace: Path, console: Console) -> SessionLogger:
    manager = SessionManager(workspace)
    reason = "new session"
    if args.new_session is not None:
        logger = manager.create_session(args.new_session)
    elif args.session:
        logger = manager.resume_session(args.session)
        reason = "resumed"
    else:
        logger, reason = manager.resume_or_start(
            max_age_seconds=_SESSION_MAX_AGE_SECONDS,
            max_messages=_SESSION_MAX_MESSAGES,
        )
    console.print(
        f"[dim]Session: {logger.session_id} ({logger.metadata.title}) - {reason}. "
        "`/sessions list` to see others, `/sessions close` to end this one.[/dim]"
    )
    _announce_other_threads(manager, logger, console)
    _announce_stale_files(logger, console)
    _announce_recovered_transcript(logger, console)
    return logger


def _announce_recovered_transcript(logger: Any, console: Any) -> None:
    """Say so when the transcript had to be rescued.

    A reformatted `messages.jsonl` used to hydrate as ONE message with no
    warning - the agent worked with almost no history and nobody could tell.
    Recovery is good; recovering silently is how the original bug hid.
    """
    try:
        logger.read_messages(1)
        rescued = getattr(logger, "recovered_message_count", 0)
    except Exception:
        return
    if rescued:
        console.print(
            f"[yellow]Note: this session's transcript is no longer line-delimited "
            f"JSON - recovered {rescued} messages from it. Something reformatted "
            f"{logger.messages_path.name}; history is intact but the file should "
            "not be edited by hand.[/yellow]"
        )


def _announce_other_threads(manager: Any, current: Any, console: Any, limit: int = 5) -> None:
    """Name the other threads, so resuming one is discoverable.

    Persistence was never the problem - a conversation survives a reboot and a
    full model eviction. Finding it was: the old sessions sat on disk behind a
    command you had to already know, so starting a new thread read as "yesterday
    is gone". This is the sidebar every chat app has, in one line.
    """
    try:
        others = [
            item for item in manager.list_sessions()
            if item.session_id != current.session_id and item.status == "active"
        ][:limit]
    except Exception:
        return
    if not others:
        return
    console.print("[dim]Other threads you can resume:[/dim]")
    for item in others:
        console.print(
            f"[dim]  /sessions resume {item.session_id}   "
            f"{item.title} ({item.message_count} messages)[/dim]"
        )


def _announce_stale_files(logger: Any, console: Any, limit: int = 8) -> None:
    """Name files that changed while this thread was away.

    The workspace LISTING is rebuilt every round, so layout is never stale. What
    goes stale is file CONTENT quoted in old `read_file` results - the model
    will happily act on a week-old copy. This says which memories to distrust.
    """
    try:
        changed = logger.files_changed_since_last_activity(limit=limit + 1)
    except Exception:
        return
    if not changed:
        return
    shown = ", ".join(changed[:limit])
    more = f" (+{len(changed) - limit} more)" if len(changed) > limit else ""
    console.print(
        f"[dim]Changed since this thread was last active: {shown}{more}. "
        "Re-read those before editing them.[/dim]"
    )


def _handle_sessions(
    user_input: str,
    manager: SessionManager,
    current: SessionLogger,
    console: Console,
) -> SessionLogger:
    parts = user_input.split(maxsplit=3)
    command = parts[1].lower() if len(parts) > 1 else "list"
    # The rest after the subcommand, kept intact for multi-word queries.
    _, _, after_command = user_input.partition(" ")
    argument = after_command.partition(" ")[2].strip() if " " in after_command.strip() else ""
    try:
        if command == "list":
            table = Table(title="Sessions")
            table.add_column("ID")
            table.add_column("Title")
            table.add_column("Status")
            table.add_column("Updated")
            table.add_column("Events")
            for item in manager.list_sessions():
                table.add_row(
                    item.session_id, item.title, item.status, item.updated_at, str(item.event_count)
                )
            console.print(table)
            return current
        if command == "current":
            _print_session(current.metadata, console)
            return current
        if command == "fork":
            # A fresh window that still remembers where it came from. The
            # parent keeps every byte and the link is recorded, so `history`
            # still reaches across it - forking costs nothing in recall.
            forked = manager.fork(current.session_id, argument or None)
            console.print(
                f"[green]Forked[/green] {current.metadata.title} -> "
                f"{forked.metadata.title} ({forked.session_id})"
            )
            console.print(
                "[dim]The earlier conversation is kept in full and stays "
                "searchable with `/sessions history <query>`.[/dim]"
            )
            return forked
        if command == "history":
            if not argument:
                console.print("[yellow]Say what to look for: /sessions history <query>[/yellow]")
                return current
            from shamsu.session.history import render_hits, search_history

            hits = search_history(manager, current.session_id, argument)
            console.print(render_hits(hits, argument))
            return current
        if command == "tree":
            chain = manager.ancestry(current.session_id)
            if len(chain) == 1:
                console.print("[dim]This conversation has not been forked.[/dim]")
                return current
            for depth, item in enumerate(reversed(chain)):
                marker = "  " * depth + ("* " if item.session_id == current.session_id else "- ")
                console.print(f"{marker}{item.title}  [dim]{item.session_id}[/dim]")
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
            console.print(
                f'[green]Renamed session {renamed.session_id} to "{renamed.title}"[/green]'
            )
            if renamed.session_id == current.session_id:
                return SessionLogger(manager, renamed)
            return current
        if command == "new":
            # Start a fresh thread WITHOUT ending the current one. `close` was
            # the only way to get a new session from inside the REPL, which
            # forced you to end work you wanted to keep just to start something
            # else in the same workspace. The old session stays active and
            # resumable by id.
            previous = current.session_id
            title = argument.strip() or None
            logger = manager.create_session(title)
            console.print(
                f"[green]Started session {logger.session_id} ({logger.metadata.title}).[/green]\n"
                f"[dim]{previous} is still open - `/sessions resume {previous}` to go back.[/dim]"
            )
            return logger
        if command == "close":
            target = parts[2] if len(parts) >= 3 else current.session_id
            closed = manager.close_session(target)
            console.print(f"[yellow]Closed session {closed.session_id}[/yellow]")
            if closed.session_id == current.session_id:
                return manager.create_session()
            return current
        if command == "export" and len(parts) >= 3:
            path = manager.export_session(parts[2])
            console.print(f"[green]Exported session bundle: {path}[/green]")
            return current
        if command == "trace":
            logger = manager.logger_for(parts[2]) if len(parts) >= 3 else current
            _print_session_trace(logger, console)
            return current
        if command == "summary":
            logger = manager.logger_for(parts[2]) if len(parts) >= 3 else current
            _print_session_summary(logger, console)
            return current
        if command == "memory":
            logger = manager.logger_for(parts[2]) if len(parts) >= 3 else current
            _print_session_memory(logger, console)
            return current
        if command == "search":
            if not argument:
                console.print("[red]Usage: /sessions search <query>[/red]")
                return current
            _print_session_search(manager, argument, console)
            return current
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        return current
    console.print(
        "[red]Usage: sessions list|current|show|resume|rename|close|export|trace|summary|memory|search[/red]"
    )
    return current


# Structured action events safe to surface in `/sessions trace`. Deliberately
# excludes raw model turns and planner output (chat.message, planner.plan,
# llm.request/response, user.prompt) so no hidden chain-of-thought leaks.
_TRACE_ALLOW_PREFIXES = (
    "router.",
    "route.",
    "workflow.",
    "agent.tool",
    "agent.run",
    "agent.stuck",
    "command.",
    "patch.",
    "tool.",
    "web.",
    "browser.",
    "approval.",
    "context.pack",
    "memory.write",
    "memory.long_term",
    "memory.local",
    "memory.retrieved",
    "session.route",
    "session.pending_action",
    "session.started",
    "session.resumed",
    "session.closed",
    "session.auto_titled",
    "assistant.message",
)


def _is_trace_event(event_type: str) -> bool:
    return any(event_type.startswith(prefix) for prefix in _TRACE_ALLOW_PREFIXES)


def _trace_line(event: dict[str, Any]) -> str:
    event_type = str(event.get("event_type", ""))
    payload = event.get("payload", {}) or {}
    summary = str(event.get("summary", "")).strip()
    if event_type == "router.decision":
        return f"Route: {payload.get('intent') or payload.get('route') or summary}"
    if event_type == "session.route.updated":
        return f"Route: {payload.get('route') or '-'}"
    if event_type.startswith("workflow."):
        label = event_type.split(".", 1)[1]
        return f"Workflow {label}: {summary or payload.get('intent', '')}".strip()
    if event_type == "agent.tool_call":
        return f"Tool call: {payload.get('tool_name', '?')}"
    if event_type == "agent.tool_result":
        ok = "ok" if payload.get("ok") else "failed"
        return f"Tool: {payload.get('tool_name', '?')} {ok}"
    if event_type.startswith("command."):
        return f"Command {event_type.split('.', 1)[1]}: {summary}"
    if event_type.startswith("patch."):
        return f"Patch {event_type.split('.', 1)[1]}: {summary}"
    if event_type.startswith("browser."):
        return f"Browser {event_type.split('.', 1)[1]}: {summary}"
    if event_type.startswith("web."):
        return f"Web {event_type.split('.', 1)[1]}: {summary}"
    if event_type.startswith("approval."):
        return f"Approval {event_type.split('.', 1)[1]}: {summary}"
    if event_type.startswith("memory."):
        return f"Memory: {summary or event_type}"
    if event_type == "assistant.message":
        return f"Final: {summary or _clip_text(str(payload.get('message', '')), 100)}"
    return f"{event_type}: {summary}"


def _print_session_trace(logger: SessionLogger, console: Console) -> None:
    events = [
        event for event in logger.tail(400) if _is_trace_event(str(event.get("event_type", "")))
    ]
    if not events:
        console.print("[dim]No structured trace events for this session yet.[/dim]")
        return
    table = Table(title=f"Trace — {logger.metadata.title}")
    table.add_column("Time")
    table.add_column("Action")
    for event in events[-60:]:
        table.add_row(str(event.get("timestamp", ""))[11:19], _trace_line(event))
    console.print(table)


def _print_session_summary(logger: SessionLogger, console: Console) -> None:
    summary = logger.read_summary()
    if not summary.strip():
        # No summary persisted yet: generate a deterministic one on demand.
        summary = logger.update_summary_from_events()
    console.print(
        Panel(
            summary.strip() or "No summary available.", title=f"Summary — {logger.metadata.title}"
        )
    )


def _print_session_memory(logger: SessionLogger, console: Console) -> None:
    records = logger.read_local_memory()
    if not records:
        console.print("[dim]No local session memory recorded yet.[/dim]")
        return
    table = Table(title=f"Session Memory — {logger.metadata.title}")
    table.add_column("Kind")
    table.add_column("Memory")
    table.add_column("When")
    for record in records[-40:]:
        table.add_row(
            str(record.get("kind", "")),
            _clip_text(str(record.get("text", "")), 200),
            str(record.get("timestamp", ""))[:19],
        )
    console.print(table)


def _print_session_search(manager: SessionManager, query: str, console: Console) -> None:
    matches = manager.search_sessions(query)
    if not matches:
        console.print(f"[dim]No sessions matched: {query}[/dim]")
        return
    table = Table(title=f"Search — {query}")
    table.add_column("Session")
    table.add_column("Title")
    table.add_column("Source")
    table.add_column("Snippet")
    for match in matches:
        table.add_row(
            str(match.get("session_id", ""))[:19],
            _clip_text(str(match.get("title", "")), 24),
            f"{match.get('source', '')}/{match.get('role', '')}".rstrip("/-"),
            _clip_text(str(match.get("snippet", "")), 70),
        )
    console.print(table)


def _clip_text(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


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


def _finalize_session_work(
    session_logger: SessionLogger | None,
    workflow: str,
    request_text: str,
) -> None:
    """Refresh local resume state and enqueue any evidence-backed memory."""
    if not session_logger:
        return
    try:
        session_logger.update_summary_from_events()
    except Exception:
        pass
    try:
        _record_task_memory(
            session_logger.manager.workspace,
            f"{workflow} request: {request_text.strip()[:500]}",
            "task_summary",
            session_logger,
            {"workflow": workflow, "intent": workflow},
        )
    except Exception:
        pass


def _append_agent_context(user_input: str, agent_context: str) -> str:
    if not agent_context:
        return user_input
    return f"{user_input}\n\nAdditional SHAMSU context:\n{agent_context}"


def _extract_requested_file_path(user_input: str) -> str | None:
    for token in re.split(r"\s+", user_input):
        cleaned = token.strip(" \t\r\n'\"`@,.;:")
        if re.fullmatch(
            r"(?:[A-Za-z]:)?[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_. -]+)*\.[A-Za-z0-9]{1,12}",
            cleaned,
        ):
            return cleaned
    match = _FILELIKE_RE.search(user_input)
    if match:
        return match.group(0).strip(" \t\r\n'\"`@,.;:")
    return None


_BACKTICK_SPAN_RE = re.compile(r"`([^`\n]+)`")
_QUOTED_COMMAND_SPAN_RE = re.compile(r"(?P<quote>['\"])(?P<content>[^'\"\n]+)(?P=quote)")
_KNOWN_RUNNER_RE = re.compile(
    r"^(?:python3?|py|pytest|npm|npx|node|pnpm|yarn|cargo|go|dotnet)\b", re.IGNORECASE
)


def _explicit_command_in_prompt(user_input: str) -> str:
    """Return a backtick-quoted command the user spelled out, if there is one.

    A single backticked token is a filename ("run `app.py`"), not a command, so
    only a multi-token span counts. Used to stand down from command inference.
    """
    for match in _BACKTICK_SPAN_RE.finditer(str(user_input or "")):
        candidate = match.group(1).strip()
        if candidate and len(candidate.split()) > 1:
            return candidate
    for match in _QUOTED_COMMAND_SPAN_RE.finditer(str(user_input or "")):
        candidate = match.group("content").strip()
        if len(candidate.split()) > 1 and _KNOWN_RUNNER_RE.match(candidate):
            return candidate
    return ""


def _command_for_existing_script_request(user_input: str, workspace: Path) -> str:
    text = str(user_input or "")
    lowered = text.lower()
    if not re.search(r"\b(?:run|execute|launch)\b", lowered):
        return ""
    # This path exists to infer an unstated command from a filename. When the
    # user spelled the command out, inferring one from a filename inside it runs
    # something they did not ask for - "run `python -m py_compile ok.py`" became
    # `python ok.py`, which executes the script instead of compile-checking it.
    # Honor the explicit command when it names a known runner (same allowlist as
    # _composite_verification_command); otherwise stand down rather than guess.
    explicit = _explicit_command_in_prompt(text)
    if explicit:
        return explicit if _KNOWN_RUNNER_RE.match(explicit) else ""
    requested = _extract_requested_file_path(text)
    if not requested:
        return ""
    suffix = Path(requested).suffix.lower()
    if suffix not in {".py", ".js", ".mjs", ".cjs"}:
        return ""
    try:
        candidate = (Path(workspace).resolve() / requested).resolve()
        candidate.relative_to(Path(workspace).resolve())
    except (OSError, ValueError):
        return ""
    if not candidate.is_file():
        return ""
    rel = candidate.relative_to(Path(workspace).resolve()).as_posix()
    quoted = subprocess.list2cmdline([rel]) if os.name == "nt" else shlex.quote(rel)
    if suffix == ".py":
        return f"python {quoted}"
    return f"node {quoted}"


def _handle_run_existing_script_request(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> str:
    command = _command_for_existing_script_request(user_input, workspace)
    if not command:
        message = "I could not find an existing workspace script to run."
        console.print(Panel(message, title="Run Command", border_style="yellow"))
        _log_assistant_message(session_logger, message, workflow_id="command.run")
        return message
    ledger = get_current_run()
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
        action_ledger=ledger,
    )
    call_id = ledger.log_tool_call("run_command", {"command": command}) if ledger else ""
    if ledger:
        verifier_id = ledger.verifier_id_for(command, "direct_script_run")
        ledger.log_verification_started(
            command,
            verifier_id=verifier_id,
            source="direct_script_run",
            required=True,
        )
    else:
        verifier_id = ""
    result = registry.execute("run_command", {"command": command})
    if ledger:
        ledger.log_tool_result(call_id, "run_command", result.ok, result.message, result.data)
        ledger.log_verification_result(
            bool(result.ok),
            result.message,
            command=command,
            verifier_id=verifier_id,
            source="direct_script_run",
            required=True,
            exit_code=result.data.get("exit_code"),
        )
    stdout = str(result.data.get("stdout") or "").strip()
    stderr = str(result.data.get("stderr") or "").strip()
    output = stdout or stderr or result.message
    # State the outcome, not just the bytes. "Run X and tell me whether it
    # succeeded" was answered with a bare output fence - and a command like
    # py_compile prints nothing on success, so that fence was empty and the
    # question went unanswered. The exit code is known here; say it.
    exit_code = result.data.get("exit_code")
    verdict = "succeeded" if result.ok else "failed"
    headline = f"`{command}` {verdict}"
    if exit_code is not None:
        headline += f" (exit {exit_code})"
    label = "Command output" if result.ok else "Command failed"
    body = f"{headline}\n\n{label}:\n```\n{output}\n```" if output else headline
    console.print(Panel(body, title=f"$ {command}", border_style="green" if result.ok else "red"))
    _log_assistant_message(session_logger, body, workflow_id="command.run")
    return body


# The routing chain, as data (gap B2). This is the ONE ordered list of
# (label, detector) rules; `_classify_route_label` walks it and `_handle_request`
# dispatches on the label it returns, so there is exactly one place where "which
# route is this?" is decided.
#
# It used to be answered twice: `_handle_request` had the real if/elif chain and
# `_classify_route_label` re-implemented it BY HAND for the session trace. They
# had already drifted - the mirror carried 11 of the 20 rules, in a different
# order, so "run the game" dispatched to run_game while the trace recorded "qa".
# Debugging a misroute from a trail that lies is worse than having no trail.
#
# ORDER IS THE LOGIC: these are evaluated top-down, first match wins. Moving a
# rule changes routing. `tests/test_routing_matrix.py` pins the behavior.
# Every detector is normalized to (text, workspace) so the table stays uniform.
_ROUTE_RULES: tuple[tuple[str, Callable[[str, Path], bool]], ...] = (
    # A "read/summarize the PRD" question is answered from the PRD text before
    # anything else - otherwise "checkout the prd" trips git and "what is it
    # about" falls into the tool loop and stalls.
    ("prd_summary", lambda text, ws: _looks_like_prd_summary_request(text, ws)),
    # Git/repo requests are classified before web-search, QA and code-edit (see
    # is_git_request): otherwise "commit the current changes" trips the web
    # keyword, "stage the files" falls into weak QA, and "what are the unstaged
    # changes" trips the code-edit heuristic.
    ("git", lambda text, ws: is_git_request(text)),
    # A PLAN request is classified before build/write/workspace rules. "Make a
    # step by step plan for PRD.md" otherwise matched a build ("implement"), a
    # file write ("make ... PRD.md"), or the ReAct regex ("make <anything>.md")
    # long before the plan route at the tail ever ran. Planning is one decision,
    # made in one place, so it stops being whack-a-mole across every detector.
    ("plan_prd", lambda text, ws: _looks_like_prd_plan_request(text)),
    ("mcp", lambda text, ws: _looks_like_mcp_request(text)),
    ("workspace.location", lambda text, ws: _looks_like_workspace_location_prompt(text)),
    ("workspace.files", lambda text, ws: _looks_like_workspace_files_prompt(text)),
    ("prd.build", lambda text, ws: _looks_like_prd_build_request(text, ws)),
    ("docs.ingest", lambda text, ws: _looks_like_docs_ingest_request(text)),
    ("docs.query", lambda text, ws: _looks_like_docs_query_request(text)),
    ("file.read", lambda text, ws: _looks_like_file_read_request(text)),
    ("file.write", lambda text, ws: _looks_like_file_write_request(text)),
    ("package.install", lambda text, ws: _looks_like_package_install_request(text)),
    ("command.run", lambda text, ws: _command_for_existing_script_request(text, ws) != ""),
    # A self-contained coding question ("write python for the first 100 primes")
    # is answered directly by the model - no planner, no tool loop, no timeout.
    ("direct_code", lambda text, ws: _looks_like_direct_code_request(text, ws)),
    ("workspace.prds", lambda text, ws: _looks_like_workspace_prd_request(text)),
    (
        "continue_game",
        lambda text, ws: (
            _looks_like_affirmative_continue(text) and _multiplayer_template_present(ws)
        ),
    ),
    ("run_game", lambda text, ws: _looks_like_run_game_request(text)),
    ("dev_server.recovery", lambda text, ws: _looks_like_dev_server_failure(text)),
    ("dev_server", lambda text, ws: _looks_like_dev_server_prompt(text)),
    ("prd.context_question", lambda text, ws: _looks_like_prd_context_question(text, ws)),
    ("browser", lambda text, ws: _looks_like_browser_needed_prompt(text)),
    ("web", lambda text, ws: _looks_like_web_needed_prompt(text)),
    ("agent-chat", lambda text, ws: _looks_like_react_prompt(text)),
    ("django", lambda text, ws: _looks_like_django_generation_request(text)),
    # plan_prd is evaluated EARLY (above), not here - a plan request must beat
    # the build/write/react detectors that would otherwise swallow it.
)

# No rule matched. NOT a route in its own right so much as the tail: the
# search/LLM-router path, which ends in the tool-less QA brain.
ROUTE_FALLTHROUGH = "qa"


def _matching_route_labels(effective_input: str, workspace: Path) -> list[str]:
    # Route detectors are keyword/verb scanners: they read "modify" and "files"
    # as intent to write, with no idea a "do not" sits in front of them. So a
    # read-only clause is masked out BEFORE detection - measured live, the same
    # web-search prompt matched no route without the clause and `file.write`
    # with it, which then made a correct answer report itself as a failure.
    # Only the detectors see the masked text; handlers and the model still get
    # the user's full instruction, and the tool layer enforces it.
    detector_input = read_only.strip(effective_input)
    labels: list[str] = []
    for label, matches in _ROUTE_RULES:
        try:
            if matches(detector_input, workspace):
                labels.append(label)
        except Exception as exc:
            swallowed.record(f"route.detector.{label}", exc)
    return labels


def _classify_single_route_label(effective_input: str, workspace: Path) -> str:
    labels = _matching_route_labels(effective_input, workspace)
    return labels[0] if labels else ROUTE_FALLTHROUGH


def _operation_plan(effective_input: str, workspace: Path) -> OperationPlan:
    # A build-from-PRD request is already a complete workflow: it reads the
    # contract, writes the product, and verifies acceptance. Splitting its
    # sentences into generic read/mutate/run steps loses the PRD contract and
    # lets unrelated route keywords hijack individual clauses.
    if _looks_like_prd_build_request(effective_input, workspace):
        return OperationPlan(
            prompt=effective_input.strip(),
            candidates=("prd.build",),
            clauses=(effective_input.strip(),),
            steps=(
                OperationStep(
                    id=1,
                    kind="mutation",
                    route="prd.build",
                    instruction=effective_input.strip(),
                ),
            ),
        )
    if re.search(
        r"\bthrough\s+(?:the\s+)?react\s+tool\s+loop\b", effective_input, re.I
    ) and _looks_like_file_write_request(read_only.strip(effective_input)):
        return OperationPlan(
            prompt=effective_input.strip(),
            candidates=("file.write",),
            clauses=(effective_input.strip(),),
            steps=(
                OperationStep(
                    id=1,
                    kind="mutation",
                    route="file.write",
                    instruction=effective_input.strip(),
                ),
            ),
        )
    if _command_for_existing_script_request(
        effective_input, workspace
    ) and not _looks_like_file_write_request(read_only.strip(effective_input)):
        return OperationPlan(
            prompt=effective_input.strip(),
            candidates=("command.run",),
            clauses=(effective_input.strip(),),
            steps=(
                OperationStep(
                    id=1,
                    kind="verify",
                    route="command.run",
                    instruction=effective_input.strip(),
                ),
            ),
        )
    return parse_operation_plan(
        effective_input,
        workspace,
        _classify_single_route_label,
        _matching_route_labels,
    )


def _classify_route_label(effective_input: str, workspace: Path) -> str:
    """Resolve which route handles this prompt: the first matching rule's label,
    or `qa` when nothing matches.

    This is the authority, not a description of one - `_handle_request`
    dispatches on what this returns. A detector that raises is treated as
    "no match" rather than taking the whole REPL down over a routing guess.
    """
    plan = _operation_plan(effective_input, workspace)
    return "composite" if plan.is_composite else plan.primary_route


def _simple_build_seed(user_input: str, workspace: Path) -> str:
    """Turn `/build <prd>` into the one instruction that starts a build.

    The slash is already stripped by the command router, so "build the login
    page" and "/build SPEC.md" arrive identically. Only treat it as the command
    when the argument names a file that exists - otherwise ordinary prose
    beginning with "build" would be hijacked into a PRD run.
    """
    from shamsu.agents.simple_prompt import build_instruction

    text = (user_input or "").strip()
    if not text.lower().startswith("build "):
        return ""
    argument = text[len("build "):].strip().strip('"').strip("'")
    if not argument:
        return ""
    try:
        candidate = (workspace / argument).resolve()
        if not candidate.is_file():
            return ""
        candidate.relative_to(Path(workspace).resolve())
    except (OSError, ValueError):
        return ""
    return build_instruction(argument)


class _LiveFeedbackReader:
    """Collect what the user types while a turn is running.

    smallcode gets this for free: its TUI is a raw-stdin event loop, so
    keystrokes are handled whether or not the agent is mid-turn. SHAMSU blocks
    on the model instead, so the keyboard has to be watched deliberately.

    Two things it must not do, both learned here the hard way:

    * never read while an approval prompt is waiting. On Windows the whole
      input stack is main-thread-owned, and a second reader competing for
      stdin is the run_in_executor+stdin trap that made turns hang.
    * never raise. A keyboard that cannot be polled - a pipe, a non-tty, a CI
      runner - must cost nothing at all; the turn simply proceeds as before.
    """

    POLL_SECONDS = 0.15

    def __init__(self, queue: Any, console: Console) -> None:
        self._queue = queue
        self._console = console
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "_LiveFeedbackReader":
        if self._usable():
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _usable(self) -> bool:
        if os.environ.get("SHAMSU_LIVE_FEEDBACK", "").strip() == "0":
            return False
        try:
            return sys.stdin is not None and sys.stdin.isatty()
        except Exception:  # noqa: BLE001
            return False

    def _run(self) -> None:
        buffer: list[str] = []
        try:
            import msvcrt
        except ImportError:
            msvcrt = None  # type: ignore[assignment]
        while not self._stop.is_set():
            try:
                if prompt_is_active():
                    # The approval prompt owns the keyboard. Stay out of it.
                    self._stop.wait(self.POLL_SECONDS)
                    continue
                if msvcrt is not None:
                    if not msvcrt.kbhit():
                        self._stop.wait(self.POLL_SECONDS)
                        continue
                    char = msvcrt.getwch()
                    if char in (chr(13), chr(10)):
                        self._submit("".join(buffer))
                        buffer.clear()
                    elif char == chr(8):
                        if buffer:
                            buffer.pop()
                    elif char.isprintable():
                        buffer.append(char)
                else:
                    import select

                    ready, _w, _e = select.select([sys.stdin], [], [], self.POLL_SECONDS)
                    if ready:
                        self._submit(sys.stdin.readline())
            except Exception:  # noqa: BLE001 - a broken keyboard costs nothing
                return

    def _submit(self, text: str) -> None:
        if self._queue.push(text):
            self._console.print(
                "[dim]noted - passing that to the agent at the next step[/dim]"
            )


async def _run_simple_chat(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    thinking_status: Any = None,
) -> None:
    """The default path: one conversation, seven tools, no routing.

    Everything the legacy path does before the model sees a word - orchestrator,
    PRD-plan detection, the 27-branch router, the planner, phases, task objects,
    the synthetic state frame - is skipped. What remains is what the user
    actually wants: talk to the model, and let it reach for a tool when it needs
    one.
    """
    from rich.markdown import Markdown

    from shamsu.agents.chat_loop import _default_ollama_client
    from shamsu.agents.simple_chat import SimpleChatLoop, build_simple_tools
    from shamsu.llm.manager import OLLAMA_BASE_URL
    from shamsu.runtime.timeouts import TimeoutConfig

    user_input = _simple_build_seed(user_input, workspace) or user_input
    action_ledger = get_current_run()
    timeouts = TimeoutConfig()
    tools = build_simple_tools(
        workspace,
        # Tools run on a worker thread; the approval prompt must not. See
        # `make_approval_func`.
        main_loop=asyncio.get_running_loop(),
        # Bind THIS console. Called bare, `ask_approval` builds a fresh
        # Console(), which knows nothing about the live spinner - so
        # `_pause_console_live` had nothing to stop and the status kept
        # repainting over the prompt ("Working> y"), and over the answer.
        console_approval=lambda request: ask_approval(request, console=console),
        session_logger=session_logger,
        action_ledger=action_ledger,
    )
    from shamsu.agents.simple_feedback import FeedbackQueue

    feedback = FeedbackQueue()
    loop = SimpleChatLoop(
        workspace,
        client=_default_ollama_client(OLLAMA_BASE_URL, timeouts),
        # Anything typed while the turn runs reaches the model at the next
        # round. A 24-round turn was previously watch-only.
        feedback=feedback,
        tools=tools,
        session_logger=session_logger,
        action_ledger=action_ledger,
        on_activity=lambda message: console.print(f"[dim]{message}[/dim]"),
        # A model call is silent for as long as it runs, and at the 600s
        # timeout that is ten minutes that look exactly like a hang. Ticking
        # the spinner's own text says "alive, still thinking, N seconds" and
        # costs a line nobody has to scroll past.
        on_status=_status_updater(thinking_status),
    )
    with _LiveFeedbackReader(feedback, console):
        result = await loop.run(user_input)
    body = result.final.strip() or "No response returned."
    console.print(Markdown(body))
    _log_assistant_message(session_logger, body, workflow_id="simple-chat")


async def _simple_pending_run(
    instruction: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
) -> None:
    """Run a stored pending action through SIMPLE mode.

    Slash commands and pending-action replies are dispatched inside `main()`
    BEFORE `_handle_request`, so the simple-mode guard there never sees them.
    That left four live routes into the legacy orchestrator - `proceed`, a bare
    "yes", a resumed plan, and a pending PRD plan - and a one-word reply was
    enough to take them. Live 2026-08-18 that is how `project.inspect`,
    `file.read`, `code.search` and a `test.run` denied by an AUTHOR phase
    contract ended up in a simple-mode transcript, where the model then
    imitated them.
    """
    text = (instruction or "").strip()
    if not text:
        console.print("[yellow]There is nothing pending to continue.[/yellow]")
        return
    await _run_simple_chat(text, workspace, console, session_logger)


def _pending_plan_instruction(pending_action: dict[str, Any], *, skip: int = 0) -> str:
    """The stored plan, as the one instruction that restarts it."""
    task = str(
        pending_action.get("created_from_prompt") or pending_action.get("task") or ""
    ).strip()
    steps = [str(step).strip() for step in (pending_action.get("steps") or []) if str(step).strip()]
    steps = steps[skip:]
    if not steps:
        return task
    listed = chr(10).join(f"{index}. {step}" for index, step in enumerate(steps, 1))
    lead = task or "Continue the plan we agreed."
    return (
        f"{lead}\n\nWork through these steps in order, one at a time. After each "
        f"one, check it works and say what you finished:\n{listed}"
    )


def _status_updater(thinking_status: Any) -> Any:
    """A callback that REPLACES the live status line, or None if there is none."""
    if thinking_status is None or not hasattr(thinking_status, "update"):
        return None

    def update(message: str) -> None:
        # Never paint while a prompt is waiting on the user. Rich renders a
        # status update even when the Live is stopped, so a heartbeat tick
        # lands straight on top of the approval question and whatever has been
        # typed so far.
        if prompt_is_active():
            return
        with contextlib.suppress(Exception):
            thinking_status.update(f"[dim]{message}[/dim]")

    return update


def _legacy_routing_enabled() -> bool:
    """Whether to use the pre-simple-mode router. Opt-in via SHAMSU_LEGACY_ROUTING."""
    from shamsu.agents.simple_chat import simple_mode_enabled

    return not simple_mode_enabled()


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
    if not _legacy_routing_enabled():
        await _run_simple_chat(
            user_input, workspace, console, session_logger, thinking_status=thinking_status
        )
        return
    agent_result = AgentOrchestrator(workspace, session_logger=session_logger).run(user_input)
    effective_input = agent_result.effective_input or user_input
    if session_logger is None:
        effective_input = _expand_followup_prompt(effective_input, previous_user_prompt)
    effective_input = recover_original_prompt(effective_input)
    agent_context = agent_result.context
    if not agent_result.handled and _looks_like_prd_plan_request(effective_input):
        await _handle_prd_development_plan_request(
            effective_input,
            workspace,
            console,
            session_logger=session_logger,
        )
        return
    operation_plan = _operation_plan(effective_input, workspace)
    # Record the routing decision in session state so `/sessions trace` and a
    # resumed session can see how the last prompt was dispatched.
    if agent_result.handled:
        route_label = agent_result.action
    elif operation_plan.is_composite:
        route_label = "composite"
    else:
        route_label = operation_plan.primary_route
    route_label = route_label or "agent-chat"
    # Pure small talk ("hey how are you", "thanks") that matched no specific
    # route must NOT reach the task router - there it becomes a "QA task" with a
    # fabricated plan and a "proceed?" prompt. Only override the fallthrough
    # catch-all, so a real question that fell through to qa is untouched.
    if (
        not agent_result.handled
        and route_label == ROUTE_FALLTHROUGH
        and _is_conversational_prompt(effective_input)
    ):
        route_label = "general_chat"
    ledger = get_current_run()
    if hasattr(web_tool, "action_ledger"):
        web_tool.action_ledger = ledger
    if hasattr(browser_tool, "action_ledger"):
        browser_tool.action_ledger = ledger
    if ledger is not None:
        ledger.log_event("operation_plan_created", **operation_plan.to_dict())
        ledger.log_decision(
            "dispatch_request",
            goal=effective_input[:500],
            observation=(
                "AgentOrchestrator handled the request directly."
                if agent_result.handled
                else "The ordered route table classified the request."
            ),
            evidence=[
                f"route:{route_label}",
                f"orchestrator_handled:{agent_result.handled}",
                f"route_candidates:{','.join(operation_plan.candidates) or 'none'}",
                "operation_sequence:"
                + ",".join(f"{step.id}:{step.kind}" for step in operation_plan.steps),
            ],
            chosen_action=route_label,
            reason_summary=f"Dispatching this prompt through the {route_label} route.",
            expected_postcondition="The selected workflow returns a user-visible result or a truthful terminal outcome.",
            outcome="selected",
        )
    if session_logger is not None:
        try:
            session_logger.set_last_route(
                {
                    "route": route_label,
                    "handled": agent_result.handled,
                    "operation_plan": operation_plan.to_dict(),
                }
            )
        except Exception as exc:
            swallowed.record("repl.set_last_route", exc)
    # Audit EVERY prompt (not just tool-loop runs): one prompt+route entry per
    # request under .shamsu/audit, so the trail covers PRD summaries, git, QA and
    # direct-code answers too. Downstream paths add their own step-level detail.
    try:
        _audit = SessionAuditLog(
            workspace, session_logger.session_id if session_logger is not None else None
        )
        _audit.log_prompt(effective_input)
        _audit.log_route(route_label, workflow=route_label)
    except Exception as exc:
        swallowed.record("repl.audit_prompt_route", exc)
    if agent_result.handled:
        console.print(Panel(agent_result.message, title=agent_result.title or "SHAMSU"))
        _log_assistant_message(
            session_logger, agent_result.message, workflow_id=agent_result.action or "agent"
        )
        return
    # Dispatch on the route already resolved above by `_classify_route_label`.
    # The detectors themselves live in `_ROUTE_RULES` - this chain used to run a
    # second, hand-maintained copy of them, which is how the trace ended up
    # reporting a route that never ran (gap B2). Now the label IS the decision,
    # so `last_route` and the audit trail cannot disagree with reality.
    if route_label == "general_chat":
        # A lightweight, single-shot conversational reply: no workspace scan, no
        # planner, no task handoff, no tools, and crucially NO injected workspace
        # context. Passing agent_context (workspace root, file listing, recent
        # turns) made the model narrate *about the context* - "the assistant has
        # no specific action to take based on the user's message" - instead of
        # just saying hi. The lightweight manager also drops the ctx indicator
        # and the reasoning-trace glimpse, so a greeting is one clean line.
        await _run_general_chat(
            effective_input,
            console,
            _make_llm_manager(session_logger, console, workspace, lightweight=True),
            session_logger=session_logger,
            thinking_status=thinking_status,
        )
        return
    if route_label == "composite":
        await _run_composite_request(
            operation_plan,
            workspace,
            console,
            session_logger=session_logger,
            agent_context=agent_context,
        )
        return
    if route_label == "prd_summary":
        await _handle_prd_summary_request(
            effective_input,
            workspace,
            console,
            _make_llm_manager(session_logger, console, workspace),
            session_logger=session_logger,
            thinking_status=thinking_status,
        )
        return
    if route_label == "git":
        await _handle_git_request(
            effective_input,
            workspace,
            console,
            session_logger=session_logger,
            agent_context=agent_context,
        )
        return
    if route_label == "workspace.location":
        message = _print_workspace_location(workspace, console)
        _log_assistant_message(session_logger, message, workflow_id="workspace.location")
        return
    if route_label == "workspace.files":
        message = _print_workspace_files(workspace, console)
        _log_assistant_message(session_logger, message, workflow_id="workspace.files")
        return
    if route_label == "prd.build":
        await _handle_prd_build_request(
            effective_input, workspace, console, session_logger=session_logger
        )
        return
    if route_label == "docs.ingest":
        await _run_agent_chat(
            effective_input,
            workspace,
            console,
            session_logger=session_logger,
            use_long_term_memory=False,
            use_planner=False,
        )
        return
    if route_label == "docs.query":
        await _run_agent_chat(
            effective_input,
            workspace,
            console,
            session_logger=session_logger,
            use_long_term_memory=False,
            use_planner=False,
        )
        return
    if route_label == "file.read":
        await _handle_file_read_request(
            effective_input,
            workspace,
            console,
            _make_llm_manager(session_logger, console, workspace),
            session_logger=session_logger,
            thinking_status=thinking_status,
        )
        return
    if route_label == "file.write":
        upfront = deterministic_user_decision(effective_input)
        if upfront is not None:
            _, question, options = upfront
            pending = {
                "question": question,
                "options": options,
                "allow_free_text": True,
                "source": "direct_file_upfront",
                "created_from_prompt": effective_input,
            }
            body = format_question(pending)
            console.print(Panel(body, title="Need Input", border_style="cyan"))
            if session_logger is not None:
                try:
                    session_logger.set_pending_question(pending)
                except Exception as exc:
                    swallowed.record("repl.direct_file_pending_question", exc)
            if ledger is not None:
                ledger.log_event(
                    "run_needs_input",
                    question=question,
                    option_count=len(options),
                    route="file.write",
                )
            emit_trace(
                console,
                session_logger,
                workspace,
                "clarification.needed",
                question,
                {"options": [option["label"] for option in options]},
                level="normal",
            )
            _log_assistant_message(session_logger, body, workflow_id="clarification")
            return
        harness_input, direct_plan = _direct_file_write_handoff(
            effective_input,
            workspace,
            "",
        )
        _log_event(
            session_logger,
            "workflow.plan",
            plan_log_payload(direct_plan),
            f"Direct file route selected {direct_plan.mode} mode",
            workflow_id=direct_plan.mode,
        )
        required_tool_prefix = ""
        if (direct_plan.mode == "test_generation" or direct_plan.document_context) and len(
            direct_plan.target_files
        ) == 1:
            try:
                target_path = _resolve_workspace_file(direct_plan.target_files[0], workspace)
            except SecurityError:
                target_path = None
            if target_path is not None and not target_path.exists():
                required_tool_prefix = "write_file"
        result = await _run_agent_chat(
            harness_input,
            workspace,
            console,
            session_logger=session_logger,
            auto_approve=is_long_running_enabled(workspace),
            user_request=effective_input,
            use_long_term_memory=False,
            use_planner=False,
            hydrate_history=False,
            required_tool_prefix=required_tool_prefix,
            allowed_write_paths=tuple(direct_plan.target_files) or None,
            force_long_running=bool(
                re.search(
                    r"\bthrough\s+(?:the\s+)?react\s+tool\s+loop\b",
                    effective_input,
                    re.IGNORECASE,
                )
            ),
        )
        # This route is dispatched as a mutation request. A model can answer
        # with a chat-shaped code fence instead of calling a file tool - no
        # tool call, no approval, no file touched - and without this check the
        # run had no failure evidence at all, so it fell through to the
        # default "success" outcome despite doing nothing.
        changed_files = list(getattr(result, "changed_files", ()) or ())
        if not changed_files:
            ledger = get_current_run()
            if ledger:
                ledger.log_event("mutation_required_but_missing", route="file.write")
        return
    if route_label == "command.run":
        _handle_run_existing_script_request(
            effective_input,
            workspace,
            console,
            session_logger=session_logger,
        )
        return
    if route_label == "package.install":
        _handle_package_install_request(
            effective_input,
            workspace,
            console,
            session_logger=session_logger,
        )
        return
    if route_label == "direct_code":
        emit_trace(
            console,
            session_logger,
            workspace,
            "route.detected",
            "direct_code",
            {"reason": "self_contained_coding_question"},
            level="normal",
        )
        answer = await _run_direct_code_answer(
            effective_input,
            console,
            _make_llm_manager(session_logger, console, workspace),
            session_logger=session_logger,
            thinking_status=thinking_status,
        )
        _audit_simple_turn(workspace, session_logger, "direct_code", effective_input, answer)
        return
    if route_label == "workspace.prds":
        message = _handle_workspace_prd_request(workspace, console)
        _log_assistant_message(session_logger, message, workflow_id="workspace.prds")
        return
    if route_label == "continue_game":
        await _run_agent_chat(
            _build_continue_game_request(),
            workspace,
            console,
            session_logger=session_logger,
            force_long_running=True,
            auto_approve=True,
        )
        return
    if route_label == "run_game":
        await _handle_run_game(workspace, console, session_logger=session_logger)
        return
    if route_label == "dev_server.recovery":
        _handle_dev_server_recovery(workspace, console, session_logger=session_logger)
        return
    if route_label == "dev_server":
        _handle_dev_server(effective_input, workspace, console, session_logger=session_logger)
        return
    if route_label == "prd.context_question":
        message = _handle_workspace_prd_request(workspace, console)
        _log_assistant_message(session_logger, message, workflow_id="prd.context_question")
        return
    if route_label == "browser":
        await _run_browser_assist(
            effective_input,
            console,
            llm=_make_llm_manager(session_logger, console, workspace),
            browser_tool=browser_tool,
        )
        return
    if route_label == "web":
        await _run_web_assist(
            effective_input,
            console,
            llm=_make_llm_manager(session_logger, console, workspace),
            web_tool=web_tool,
            session_logger=session_logger,
        )
        return
    if route_label == "mcp":
        await _run_agent_chat(effective_input, workspace, console, session_logger=session_logger)
        return
    if route_label == "agent-chat":
        await _run_agent_chat(effective_input, workspace, console, session_logger=session_logger)
        return
    if route_label == "django":
        generate_command = f"generate-django {_extract_prd_path_from_prompt(effective_input)}"
        _handle_generate_django(generate_command, workspace, console, session_logger=session_logger)
        return
    if route_label == "plan_prd":
        if _looks_like_plan_intent(effective_input):
            await _handle_prd_development_plan_request(
                effective_input,
                workspace,
                console,
                session_logger=session_logger,
            )
            return
        plan_command = f"plan-prd {_resolved_prd_reference(effective_input, workspace)}"
        _handle_plan_prd(plan_command, workspace, console, session_logger=session_logger)
        return
    # ROUTE_FALLTHROUGH: no rule matched - the search/LLM-router tail.
    search, uses_real_index = _build_search_agent(workspace, session_logger)
    llm = _make_llm_manager(session_logger, console, workspace)
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
                    user_request=effective_input,
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
    original_intent = decision.intent
    decision = _enforce_read_only_decision(effective_input, decision)
    if decision.intent != original_intent:
        _log_event(
            session_logger,
            "routing.read_only_override",
            {"original_intent": original_intent, "selected_intent": decision.intent},
            "Blocked a mutating workflow for an explicitly read-only request",
            workflow_id="qa",
        )
        if ledger is not None:
            ledger.log_event(
                "routing_read_only_override",
                original_intent=original_intent,
                selected_intent=decision.intent,
            )
    intent_before_investigative = decision.intent
    decision = _enforce_investigative_question_decision(effective_input, decision)
    if decision.intent != intent_before_investigative:
        _log_event(
            session_logger,
            "routing.investigative_question_override",
            {"original_intent": intent_before_investigative, "selected_intent": decision.intent},
            "Answered an investigative question instead of proposing an unrequested change",
            workflow_id="qa",
        )
        if ledger is not None:
            ledger.log_event(
                "routing_investigative_question_override",
                original_intent=intent_before_investigative,
                selected_intent=decision.intent,
            )
    _print_decision(decision, console, verbose=_trace_mode(workspace) == "verbose")
    emit_trace(
        console,
        session_logger,
        workspace,
        "route.detected",
        decision.intent,
        {"confidence": f"{decision.confidence:.2f}"},
        level="normal",
    )
    task_plan = build_task_plan(decision, effective_input, workspace=workspace)
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
            # QA must be EARNED by question shape (gap B1). Anything that is
            # work - an imperative, a trouble report, or a statement of what
            # should change ("the login page needs a dark mode") - goes to the
            # agent loop, which has tools and can ask upfront (J6). The
            # tool-less QA brain would only describe the change, confidently.
            if _qa_branch_routes_to_agent(effective_input, uses_real_index):
                await _run_agent_chat(
                    harness_input,
                    workspace,
                    console,
                    session_logger=session_logger,
                    auto_approve=is_long_running_enabled(workspace),
                    user_request=effective_input,
                )
            else:
                await _run_qa(
                    effective_input,
                    workspace,
                    console,
                    llm,
                    extra_context=agent_context,
                    session_logger=session_logger,
                    thinking_status=thinking_status,
                )
        elif decision.intent == "code_edit":
            # Hard safety guard: a read-only Git question ("what are the
            # unstaged changes?") must never enter the patch/coder workflow,
            # even if the classifier mislabeled it as code_edit. Answer it as a
            # read-only Git request instead.
            if _is_read_only_git_question(effective_input):
                _log_event(
                    session_logger,
                    "routing.git_override",
                    {
                        "route": "git_read",
                        "reason": "code_edit_blocked",
                        "read_only": True,
                        "mutation": False,
                    },
                    "Blocked code-edit for read-only Git question",
                    workflow_id="git",
                )
                if _trace_mode(workspace) != "quiet":
                    console.print("[dim]patch_workflow=blocked reason=read_only_git_question[/dim]")
                    console.print("[dim]route=git_read selected_workflow=git_read_only[/dim]")
                _run_git_read_only(
                    effective_input,
                    workspace,
                    console,
                    session_logger,
                    lambda line: (
                        console.print(f"[dim]{line}[/dim]")
                        if _trace_mode(workspace) != "quiet"
                        else None
                    ),
                )
            else:
                await _run_code_edit(harness_input, workspace, search, console, llm, session_logger)
        elif decision.intent == "bug_fix":
            bugfix_input = harness_input
            if not _bugfix_request_has_actionable_target(effective_input):
                # No explicit target - reuse the most recent failing command +
                # errors from session memory (e.g. after `/autonomy on` a build
                # failed and the user just says "fix it") instead of re-asking.
                reused = _bugfix_report_from_last_failure(effective_input, session_logger)
                if reused is None:
                    message = (
                        "Tell me what to fix first: include a file path, traceback, failing command, "
                        "or the exact error message. Example: /fix tests/test_app.py fails with AssertionError ..."
                    )
                    console.print(
                        Panel(message, title="Bug Fix Needs Target", border_style="yellow")
                    )
                    _log_assistant_message(session_logger, message, workflow_id="bug_fix")
                    return
                bugfix_input, reused_command = reused
                console.print(
                    f"[dim]Reusing the last failing command from this session: {reused_command}[/dim]"
                )
            await _run_bug_fix(bugfix_input, workspace, search, console, llm, session_logger)
        elif decision.intent == "audit":
            await _run_audit(harness_input, search, console, llm)
        elif decision.intent == "test_gen":
            await _run_test_generation(
                harness_input, workspace, search, console, llm, session_logger
            )
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
        # Read-only Q&A produces no durable lesson worth persisting, so skip the
        # long-term memory write for plain questions - it is pure post-answer
        # overhead there. Workflows that actually change the project (edits, bug
        # fixes, tests, docs) still record a summary, now off the hot path.
        if decision.intent in {"code_edit", "bug_fix", "test_gen", "doc_gen", "project_gen"}:
            memory_kind = "bug_lesson" if decision.intent == "bug_fix" else "task_summary"
            _record_task_memory(
                workspace,
                f"{decision.intent} request: {effective_input[:700]}",
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
        if (
            decision.intent in {"qa", "explain"}
            and decision.confidence < 0.6
            and (
                _looks_like_command_like_prompt(user_input)
                or _looks_like_trouble_report(user_input)
            )
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
                steps=[{"id": 1, "specialist": intent, "task": user_input[len(prefix) :]}],
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
    elif _looks_like_trouble_report(user_input) or any(
        word in text for word in ("traceback", "exception", "error:", "failing", "fix ", "repair")
    ):
        intent = "bug_fix"
    elif any(
        word in text
        for word in ("write tests", "generate tests", "test for", "pytest", "run tests", "test ")
    ):
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


def _direct_file_write_handoff(
    user_input: str,
    workspace: Path,
    agent_context: str = "",
) -> tuple[str, TaskPlan]:
    classified = _keyword_decision(user_input)
    deletion_requested = bool(re.search(r"\b(?:delete|remove)\b", user_input, re.IGNORECASE))
    intent = (
        classified.intent
        if classified.intent in {"code_edit", "test_gen", "doc_gen"}
        else "code_edit"
    )
    requested_target = _explicit_canonical_edit_target(user_input) or _extract_requested_file_path(
        user_input
    )
    if deletion_requested:
        intent = "code_edit"
        requested_target = _project_root_qualified_target(
            user_input, requested_target, workspace
        )
    target = requested_target
    source_under_test = ""
    if requested_target:
        target_name = Path(requested_target).name.casefold()
        target_suffix = Path(requested_target).suffix.casefold()
        if not deletion_requested and target_suffix in {".md", ".rst", ".txt"}:
            intent = "doc_gen"
        elif (
            not deletion_requested
            and not (workspace / requested_target).is_file()
            and (target_name.startswith("test_") or ".test." in target_name or ".spec." in target_name)
        ):
            intent = "test_gen"
    if intent == "test_gen" and requested_target:
        inferred_test_target = _test_output_path(requested_target)
        if inferred_test_target != requested_target:
            source_under_test = requested_target
            target = inferred_test_target
    decision = RoutingDecision(
        intent=intent,
        complexity="single",
        steps=[
            {
                "id": 1,
                "specialist": "coder",
                "task": "Inspect the target, apply the requested mutation, and verify it.",
            }
        ],
        needs_tools=[
            "read_file",
            "edit_file",
            "append_file",
            "write_file",
            "delete_file",
            "file_info",
            "run_command",
        ],
        target_files=[target] if target else [],
        confidence=1.0,
    )
    plan = build_task_plan(decision, user_input, workspace=workspace)
    handoff = append_task_handoff(user_input, plan, agent_context)
    if deletion_requested and target:
        handoff += (
            "\n\n## File Deletion Contract\n"
            f"Deletion target: `{target}`\n"
            "- Read or inspect the exact target first.\n"
            "- Call `delete_file` on that exact target; do not represent deletion as an empty "
            "write and do not rewrite a similarly named file.\n"
            "- Confirm the target is absent with `file_info` before finishing.\n"
        )
    semantic_contract = contract.derive(user_input, workspace=workspace)
    if semantic_contract.required_python_symbols:
        requirements = "\n".join(
            f"- `{symbol}` must exist as a new function in `{path}`."
            for path, symbol in semantic_contract.required_python_symbols
        )
        handoff += (
            "\n\n## Explicit Edit Contract\n"
            f"{requirements}\n"
            "- Preserve existing functions and their behavior unless the user explicitly "
            "asked to change them.\n"
            "- Adding a function means inserting a new definition, not changing an existing "
            "function's return expression.\n"
            "- Re-read the edited file and confirm every named function exists before finishing."
        )
    if source_under_test:
        source_path = workspace / source_under_test
        try:
            source_text = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            source_text = ""
        source_excerpt = source_text[:12_000]
        handoff += (
            "\n\n## Test Generation Contract\n"
            f"Source under test: {source_under_test}\n"
            f"Required test output: {target}\n"
            f"Create `{target}` with focused executable tests. Do not overwrite "
            f"`{source_under_test}`. The harness will run the tests after the write."
        )
        if source_excerpt:
            handoff += (
                "\n\nSource snapshot:\n```text\n"
                + source_excerpt
                + ("\n[truncated]" if len(source_text) > len(source_excerpt) else "")
                + "\n```"
            )
    return handoff, plan


def _project_root_qualified_target(
    user_input: str,
    target: str,
    workspace: Path,
) -> str:
    """Qualify a basename described as being at an explicitly named project root."""
    normalized = str(target or "").replace("\\", "/").strip("/ ")
    if not normalized or "/" in normalized or "project root" not in user_input.lower():
        return normalized
    quoted = re.findall(r"[`\"']([^`\"']+)[`\"']", user_input)
    project_dirs: list[str] = []
    for item in quoted:
        candidate = item.replace("\\", "/").strip("/ ")
        if not candidate or Path(candidate).suffix:
            continue
        try:
            resolved = _resolve_workspace_file(candidate, workspace)
        except SecurityError:
            continue
        if resolved.is_dir():
            project_dirs.append(candidate)
    if len(project_dirs) != 1:
        return normalized
    return f"{project_dirs[0]}/{normalized}"


def _explicit_canonical_edit_target(user_input: str) -> str:
    """Resolve a path declared canonical when the user says to edit only it."""
    if not re.search(
        r"\b(?:update|edit|modify|change|repair|fix)\s+only\s+(?:the\s+)?canonical\b",
        user_input,
        re.IGNORECASE,
    ):
        return ""
    match = re.search(
        r"\bcanonical\b[^.!?\n]{0,80}['\"`](?P<path>[^'\"`\n]+\.[A-Za-z0-9]{1,12})['\"`]",
        user_input,
        re.IGNORECASE,
    )
    return match.group("path").replace("\\", "/") if match else ""


def _test_output_path(source: str) -> str:
    """Derive a deterministic test target when the prompt names source code."""
    normalized = source.replace("\\", "/").lstrip("./")
    path = Path(normalized)
    name = path.name.casefold()
    if name.startswith("test_") or ".test." in name or ".spec." in name:
        return normalized
    suffix = path.suffix.casefold()
    stem = path.stem
    if suffix == ".py":
        return f"tests/test_{stem}.py"
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return f"tests/{stem}.test{path.suffix}"
    return normalized


_MUTATING_INTENTS = frozenset({"bug_fix", "code_edit", "doc_gen", "generate", "test_gen"})


def _explicitly_read_only(user_input: str) -> bool:
    # Canonical definition lives in shamsu/safety/read_only.py. The local copy
    # this replaced missed "do not modify any OTHER files" (it required a
    # literal "any files"), so run 3 of the dogfood never registered a
    # constraint at all.
    return read_only.applies(user_input)


def _enforce_read_only_decision(
    user_input: str,
    decision: RoutingDecision,
) -> RoutingDecision:
    if decision.intent not in _MUTATING_INTENTS or not _explicitly_read_only(user_input):
        return decision
    return RoutingDecision(
        intent="qa",
        complexity="single",
        steps=[{"id": 1, "specialist": "qa", "task": user_input}],
        needs_tools=["search"],
        confidence=1.0,
    )


# Change verbs that unambiguously mean "modify the code". Their presence vetoes
# the investigative-question downgrade: "fix the bug in X" is work even though
# a small-model router and a question detector could both trip over it.
_MUTATION_VERB_RE = re.compile(
    r"\b(fix|repair|resolve|correct|patch|refactor|rewrite|implement|add|remove|"
    r"delete|rename|update|modify|change|replace|create|make|build|generate|"
    r"write|edit|install|configure)\b"
)


def _looks_like_investigative_question(user_input: str) -> bool:
    """A question ABOUT code - "is there a bug in divide?", "does this handle
    zero?" - that a small-model router misclassifies as bug_fix/code_edit and
    then "answers" by proposing an unrequested patch (live repro 2026-07-23).
    The user asked a question, not for a change. Conservative: any explicit
    change verb anywhere means it is real work and this returns False, so only
    pure questions with zero mutation intent are downgraded."""
    raw = user_input.strip().lower()
    if not raw:
        return False
    if _MUTATION_VERB_RE.search(raw):
        return False
    return _prefers_qa_answer(user_input)


def _enforce_investigative_question_decision(
    user_input: str,
    decision: RoutingDecision,
) -> RoutingDecision:
    # Only mutating code intents; generate/doc_gen/test_gen have their own
    # strong signals and are left alone.
    if decision.intent not in {"bug_fix", "code_edit"}:
        return decision
    if not _looks_like_investigative_question(user_input):
        return decision
    return RoutingDecision(
        intent="qa",
        complexity="single",
        steps=[{"id": 1, "specialist": "qa", "task": user_input}],
        needs_tools=["search"],
        confidence=1.0,
    )


# A request to PLAN, not to build. "plan the implementation", "make a step by
# step plan", "outline the approach" - the user wants the plan first, and must
# not be dropped into a full build because "implementation" happens to contain
# "implement". Distinct from `proceed`/`run the plan`, which execute one.
_PLAN_INTENT_RE = re.compile(
    r"\b(?:make|write|draft|create|give\s+me|outline|sketch|propose)\s+"
    r"(?:a\s+|an\s+|the\s+)?(?:step[\s-]?by[\s-]?step\s+)?"
    r"(?:(?:implementation|development|devolopment)\s+)?plan\b"
    r"|\bplan\s+(?:out\s+)?(?:the\s+|a\s+|how\s+)"
    r"|\boutline\s+(?:the\s+)?(?:steps|approach|plan)\b"
    r"|^\s*plan\b(?!\s*(?:mode|is|was))",
    re.IGNORECASE,
)


def _looks_like_plan_intent(user_input: str) -> bool:
    """True when the user is asking for a PLAN, not asking to build now."""
    return bool(_PLAN_INTENT_RE.search(user_input or ""))


def _looks_like_prd_plan_request(user_input: str) -> bool:
    text = user_input.lower()
    has_prd = bool(_extract_prd_path_from_prompt(user_input)) or "prd" in text
    if not has_prd:
        return False
    return _looks_like_plan_intent(user_input) or any(
        phrase in text for phrase in ("plan project", "project plan", "plan-prd")
    )


_PRD_SCHEMA_BUILD_RE = re.compile(
    r"\b(?:build|implement|create|write|generate|configure|setup|set\s+up|make)\b",
    re.IGNORECASE,
)
_PRD_SCHEMA_TARGET_RE = re.compile(
    r"\b(?:schema|ddl|database|db|postgres|postgresql|model|models|migration|migrations)\b",
    re.IGNORECASE,
)


def _looks_like_prd_schema_build_request(user_input: str) -> bool:
    """Use the PRD as source material to implement database/schema files."""
    text = user_input.lower()
    has_prd = bool(_extract_prd_path_from_prompt(user_input)) or "prd" in text
    if not has_prd or _looks_like_plan_intent(user_input):
        return False
    return bool(_PRD_SCHEMA_BUILD_RE.search(user_input) and _PRD_SCHEMA_TARGET_RE.search(text))


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


# Questions about SHAMSU's own tools/capabilities. These must be answered
# deterministically from the real tool registry - never routed to the tool-less
# QA brain, which would guess from stale session context (old file names, prior
# commits) and list tools that do not exist.
_CAPABILITY_QUESTION_PHRASES = (
    "what tools",
    "which tools",
    "what tool can you",
    "tools can you use",
    "tools do you use",
    "tools do you have",
    "tools are available",
    "tools you have",
    "your tools",
    "list your tools",
    "list the tools",
    "available tools",
    "what can you do",
    "what are you able to do",
    "what are you capable of",
    "your capabilities",
    "what are your capabilities",
    "what commands can you",
    "your abilities",
    "what abilities",
    "how can you help",
)


def _looks_like_capabilities_question(user_input: str) -> bool:
    """Only used for the 'Checking which tools I actually have...' status line;
    the real answer is produced deterministically by AgentOrchestrator (see
    `_asks_capabilities` there), before the memory gate and model routing."""
    text = user_input.lower()
    return any(phrase in text for phrase in _CAPABILITY_QUESTION_PHRASES)


def _looks_like_mcp_request(user_input: str) -> bool:
    """Explicit MCP instructions must reach the tool-calling loop."""
    text = user_input.lower()
    return bool(
        re.search(r"\bmcp\b|\bmcp__[a-z0-9_-]+__[a-z0-9_-]+\b", text)
        and re.search(r"\b(use|call|ask|query|list|read|write|create|search|fetch|test)\b", text)
    )


def _looks_like_django_generation_request(user_input: str) -> bool:
    text = user_input.lower()
    return any(
        phrase in text for phrase in ("generate django", "generate project", "build django")
    ) and bool(_extract_prd_path_from_prompt(user_input))


def _looks_like_web_needed_prompt(user_input: str) -> bool:
    text = user_input.lower()
    if _looks_like_browser_needed_prompt(user_input):
        return False
    extracted_url = _extract_url_from_prompt(user_input)
    if extracted_url and not _is_local_url(extracted_url):
        return True
    # An explicit instruction to use the web is not a hint to be weighed - it is
    # the user telling us which tool to use. "Use web search to find X" matched
    # NONE of these until 2026-07-20 ("search the web" is not "web search"), so
    # it fell through to the tool-less QA brain, which answered a "what is the
    # release date" question from stale model memory and got the year wrong.
    if any(
        phrase in text
        for phrase in (
            "web search",
            "search the web",
            "search online",
            "online search",
            "search the internet",
            "internet search",
            "on the web",
            "from the web",
            "check on the web",
            "google it",
            "google for",
        )
    ):
        return True
    if any(
        phrase in text
        for phrase in (
            "look up",
            "find docs",
            "documentation for",
            "official docs",
            "latest ",
            "current ",
        )
    ):
        return True
    if any(
        word in text
        for word in (
            "weather",
            "forecast",
            "temperature",
            "rain today",
            "news today",
            "stock price",
            "exchange rate",
        )
    ):
        return True
    if _has_fuzzy_web_keyword(text):
        return True
    if any(
        word in text
        for word in ("package", "api docs", "release notes", "version", "breaking change")
    ) and not _is_project_local_prompt(text):
        return True
    return False


def _looks_like_package_install_request(user_input: str) -> bool:
    """Route explicit project dependency installs into the tool-calling loop."""
    text = " ".join(user_input.strip().lower().split())
    if not text:
        return False
    if re.search(
        r"\b(?:pip3?|python3?\s+-m\s+pip|uv\s+pip|npm|pnpm|yarn)\s+install\b",
        text,
    ):
        return True
    if re.search(r"\b(?:install|add)\s+(?:a\s+|the\s+)?(?:package|dependency|library)\b", text):
        return True
    return bool(
        re.match(r"^install\s+[a-z0-9_.-]+(?:\[[a-z0-9_,.-]+\])?(?:[<>=!~]=?[^\s,;]+)?\b", text)
        and re.search(r"\b(?:python|project|workspace|dependency|package|library)\b", text)
    )


def _python_package_spec(user_input: str) -> str:
    match = re.search(
        r"\binstall\s+(?P<spec>[A-Za-z0-9_.-]+"
        r"(?:\[[A-Za-z0-9_,.-]+\])?(?:[<>=!~]=?[A-Za-z0-9_.+!-]+)?)",
        user_input,
        re.IGNORECASE,
    )
    return match.group("spec") if match else ""


def _package_install_command(user_input: str) -> str:
    spec = _python_package_spec(user_input)
    return f"python -m pip install {spec}" if spec else ""


def _execute_package_install(
    command: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    ledger: ActionLedger | None,
) -> tuple[Any, str]:
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
        action_ledger=ledger,
    )
    call_id = ledger.log_tool_call("run_command", {"command": command}) if ledger else ""
    result = registry.execute("run_command", {"command": command})
    if ledger:
        ledger.log_tool_result(call_id, "run_command", result.ok, result.message, result.data)
    resolved = str(result.data.get("resolved_command") or command)
    environment = result.data.get("project_environment")
    kind = str(environment.get("kind") or "") if isinstance(environment, dict) else ""
    output = str(result.data.get("stdout") or result.data.get("stderr") or result.message).strip()
    verdict = "succeeded" if result.ok else "failed"
    headline = f"`{command}` {verdict}"
    if kind:
        headline += f" using `{kind}`"
    body = f"{headline}\n\nResolved command:\n`{resolved}`"
    if output:
        body += f"\n\nOutput:\n```\n{output}\n```"
    console.print(
        Panel(
            body,
            title="Package Install",
            border_style="green" if result.ok else "red",
        )
    )
    return result, body


def _handle_package_install_request(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> str:
    command = _package_install_command(user_input)
    if not command:
        message = "I could not determine which Python package to install."
        console.print(Panel(message, title="Package Install", border_style="yellow"))
        _log_assistant_message(session_logger, message, workflow_id="package.install")
        return message
    result, body = _execute_package_install(
        command,
        workspace,
        console,
        session_logger,
        get_current_run(),
    )
    _log_assistant_message(session_logger, body, workflow_id="package.install")
    return body


# -- Git request routing override -------------------------------------------
# Git/repo requests must be classified BEFORE web-search, QA, and code-edit
# routing. Otherwise phrasing like "commit the current changes" trips the
# web-search keyword ("current "), "can you stage the files?" falls into
# low-confidence QA, and "what are the unstaged changes" trips the code-edit
# heuristic ("change"). These deterministic helpers give a Git prompt a hard
# override so it always reaches the Git tools instead.

_GIT_READ_ONLY_PHRASES = (
    "git status",
    "git diff",
    "git branch",
    "git branches",
    "git remote",
    "git log",
    "git show",
    "unstaged change",
    "staged change",
    "uncommitted change",
    "unpushed commit",
    "current branch",
    "what changed",
    "what has changed",
    "what are the changes",
    "what are the change",
    "show changes",
    "show me the changes",
    "repo status",
    "repository status",
    "status of the repo",
    "status of this repo",
    "what's changed",
    "whats changed",
    "diff of the repo",
    "working tree",
    # Untracked files are a pure git/status concept: any mention must reach
    # git_status, never find_file (which used to search for a file literally
    # named "untracked").
    "untracked",
    "untracked file",
    "untracked files",
    "untracked change",
)

_GIT_MUTATION_PHRASES = (
    "stage the file",
    "stage files",
    "stage the change",
    "stage changes",
    "stage all",
    "stage everything",
    "git add",
    "commit",
    "git push",
    "push this",
    "push to",
    "push it",
    "push the",
    "git pull",
    "pull from",
    "pull the latest",
    "git fetch",
    "fetch from",
    "create branch",
    "create a branch",
    "new branch",
    "switch branch",
    "stash",
    "git restore",
    "restore the file",
    "amend",
    # "checkout" only counts when it clearly means the git verb. Bare "checkout"
    # is colloquial ("checkout the prd" = "look at the prd") and must NOT be
    # treated as a git request.
    "git checkout",
    "checkout branch",
    "checkout the branch",
    "check out branch",
    "checkout main",
    "checkout master",
    "switch to branch",
)

# A generic Git anchor: a prompt that clearly talks about git/repo work is a
# Git request even without one of the phrases above.
_GIT_ANCHOR_PHRASES = (
    "git ",
    "the repo",
    "this repo",
    "the repository",
    "in git",
    "to github",
    "on github",
)


def is_read_only_git_request(text: str) -> bool:
    """True for Git prompts that only inspect the repo (status/diff/log/...)."""
    low = text.lower()
    return any(phrase in low for phrase in _GIT_READ_ONLY_PHRASES)


def is_git_mutation_request(text: str) -> bool:
    """True for Git prompts that change repo state (stage/commit/push/...)."""
    low = text.lower()
    if any(phrase in low for phrase in _GIT_MUTATION_PHRASES):
        return True
    # "checkout ... branch" is a git branch switch; bare "checkout the prd" is not.
    return "checkout" in low and "branch" in low


def is_git_request(text: str) -> bool:
    """True for any Git/repo request (read-only or mutation)."""
    if is_read_only_git_request(text) or is_git_mutation_request(text):
        return True
    if _looks_like_git_init_request(text):
        return True
    low = text.lower()
    if not any(anchor in low for anchor in _GIT_ANCHOR_PHRASES):
        return False
    # A bare git/repo mention still needs a git-shaped verb/noun to qualify, so
    # unrelated sentences that merely contain "the repo" don't get hijacked.
    return any(
        word in low
        for word in (
            "status",
            "diff",
            "commit",
            "branch",
            "stage",
            "staged",
            "unstaged",
            "untracked",
            "push",
            "pull",
            "fetch",
            "checkout",
            "stash",
            "remote",
            "log",
            "changes",
        )
    )


def _is_read_only_git_question(text: str) -> bool:
    """Hard guard for the code-edit path: a read-only Git *question* (starts
    with what/show/list/... and asks about repo status/diff/changes) must never
    enter the patch/coder workflow."""
    low = text.strip().lower()
    starts_read_only = any(
        low.startswith(prefix)
        for prefix in (
            "what",
            "show",
            "list",
            "describe",
            "tell me",
            "explain",
            "which",
            "where",
            "why",
            "how",
        )
    )
    return starts_read_only and is_read_only_git_request(text) and not is_git_mutation_request(text)


def _git_inspection_guidance(user_input: str) -> str:
    """Tell the tool agent to inspect the repo before mutating it.

    The agent still chooses and runs the typed Git tools itself (and every
    mutation still passes through the existing command-safety/approval system);
    this only nudges the read-before-write ordering the task requires and bans
    destructive shortcuts."""
    low = user_input.lower()
    lines = [
        "This is a Git/repo request. Use the typed git_* tools only; do not run",
        "raw shell git or invent commands. Inspect before you mutate:",
    ]
    if any(p in low for p in ("commit",)):
        lines.append(
            "- Before committing: call git_status, git_diff, then git_add_all "
            "(or git_add), then git_diff_staged, then git_commit."
        )
    elif is_git_mutation_request(user_input) and any(p in low for p in ("stage", "add")):
        lines.append(
            "- Before staging: call git_status (and git_diff), then git_add_all or git_add, then git_status again."
        )
    if any(p in low for p in ("push",)):
        lines.append(
            "- Before pushing: call git_branch, git_remote, and "
            "git_unpushed_commits, then git_push only after that. Never "
            "force-push and never reset."
        )
    lines.append(
        "- Read-only inspection tools are safe to run first without asking. "
        "Do not use reset --hard, force-push, or any destructive operation."
    )
    return "\n".join(lines)


def _format_git_read_result(tool_name: str, result: "ToolResult") -> str:
    label = {"git_status": "git status", "git_diff": "git diff"}.get(tool_name, tool_name)
    if not result.ok:
        detail = result.data.get("stderr") or result.data.get("error") or result.message
        return f"$ {label}\n{detail}".strip()
    data = result.data
    if tool_name == "git_status":
        if not data.get("is_git_repo", True):
            return "$ git status\nThis workspace is not a git repository."
        raw = (data.get("raw_output") or "").strip()
        if not data.get("is_dirty"):
            return "$ git status\nWorking tree clean (no changes)."
        changed = data.get("changed_files") or []
        body = raw or "\n".join(changed)
        return f"$ git status\n{body}".strip()
    # git_diff and other GitCommandResult-backed tools
    output = (data.get("stdout") or "").strip()
    if not output:
        return f"$ {label}\n(no output)"
    if len(output) > 4000:
        output = output[:4000] + "\n... [diff truncated]"
    return f"$ {label}\n{output}"


def _run_git_read_only(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    trace: Callable[[str], None],
) -> None:
    """Answer a read-only Git request deterministically (no LLM): run the read
    tools through the same registry/command-safety stack and summarize."""
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
        action_ledger=get_current_run(),
    )
    sections: list[str] = []
    for tool_name in ("git_status", "git_diff"):
        trace(f"tool={tool_name} args={{}}")
        result = registry.execute(tool_name, {})
        _log_event(
            session_logger,
            "tool.git_read",
            {"tool": tool_name, "ok": result.ok},
            f"Git read tool {tool_name}",
            workflow_id="git",
        )
        sections.append(_format_git_read_result(tool_name, result))
    body = "\n\n".join(section for section in sections if section).strip() or "No git output."
    console.print(Panel(Text(body), title="Git"))
    _log_assistant_message(session_logger, body, workflow_id="git-read")


_GIT_INIT_PHRASES = (
    "git init",
    "initialize git",
    "initialise git",
    "init the repo",
    "init a repo",
    "initialize the repo",
    "initialise the repo",
    "initialize a git",
    "initialise a git",
    "initialize this repo",
    "create a git repo",
    "create a repo",
    "make this a git repo",
    "make it a git repo",
    "make this a repo",
    "set up git",
    "setup git",
    "start a git repo",
    "turn this into a git repo",
)


def _looks_like_git_init_request(text: str) -> bool:
    low = text.lower()
    return any(phrase in low for phrase in _GIT_INIT_PHRASES)


def _ensure_shamsu_gitignore(workspace: Path) -> None:
    """Make sure `.shamsu/` (audit logs, sessions, internal state) is git-ignored
    so a commit never sweeps SHAMSU's own working files into the user's repo."""
    gitignore = workspace / ".gitignore"
    try:
        existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
        if any(line.strip().rstrip("/") == ".shamsu" for line in existing.splitlines()):
            return
        prefix = "" if (not existing or existing.endswith("\n")) else "\n"
        gitignore.write_text(existing + prefix + ".shamsu/\n", encoding="utf-8")
    except OSError:
        pass


def _run_git_init(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    trace: Callable[[str], None],
) -> None:
    """Deterministically initialize a git repository (git init + git_status)."""
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
        action_ledger=get_current_run(),
    )
    status = registry.execute("git_status", {})
    if status.ok and isinstance(status.data, dict) and status.data.get("is_git_repo"):
        console.print(Panel("This workspace is already a git repository.", title="Git"))
        _log_assistant_message(session_logger, "Already a git repository.", workflow_id="git-init")
        return
    trace("tool=git_init args={}")
    result = registry.execute("git_init", {})
    if not result.ok:
        msg = f"git init failed: {result.message}"
        console.print(Panel(msg, title="Git", border_style="red"))
        _log_assistant_message(session_logger, msg, workflow_id="git-init")
        return
    _ensure_shamsu_gitignore(workspace)
    after = registry.execute("git_status", {})
    body = "Initialized an empty git repository."
    if after.ok and isinstance(after.data, dict) and after.data.get("changed_files"):
        files = after.data["changed_files"]
        body += f'\n{len(files)} untracked file(s). Say "add and commit" to make the first commit.'
    console.print(Panel(body, title="Git Init"))
    _log_assistant_message(session_logger, body, workflow_id="git-init")


def _looks_like_git_add_commit_request(text: str) -> bool:
    """A common, deterministic mutation: "add [all] files and commit".

    Detected so it runs through a fixed git_status -> git_add_all -> git_commit
    sequence instead of the LLM tool loop, which used to wander off (e.g. search
    for a file literally named "untracked" before committing)."""
    low = text.lower()
    if "commit" not in low:
        return False
    words = set(re.sub(r"[^\w\s]", " ", low).split())
    return bool({"add", "stage"} & words) or "stage all" in low or "add all" in low


def _extract_commit_message(user_input: str) -> str:
    """Pull an explicit commit message out of the prompt, if the user gave one."""
    quoted = re.search(r"""['"]([^'"]{3,200})['"]""", user_input)
    if quoted:
        return quoted.group(1).strip()
    labelled = re.search(
        r"(?:message|msg|-m|commit message)\s*[:=]?\s*(.+)$",
        user_input,
        re.IGNORECASE,
    )
    if labelled:
        return labelled.group(1).strip().strip("\"'")
    return ""


def _run_git_add_commit(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    trace: Callable[[str], None],
) -> None:
    """Deterministic "stage everything and commit" flow.

    Runs git_status -> git_add_all -> git_commit through the typed git tools,
    gated behind a single approval. Never searches the filesystem (the old bug
    reached for find_file query="untracked")."""
    approval_manager = _make_approval_manager(workspace, session_logger, console)
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=approval_manager,
        action_ledger=get_current_run(),
    )

    trace("tool=git_status args={}")
    status = registry.execute("git_status", {})
    if not status.ok or (
        isinstance(status.data, dict) and not status.data.get("is_git_repo", True)
    ):
        # Not a repo yet - initialize it so "add and commit" actually works
        # instead of dead-ending. git init is safe and local.
        trace("tool=git_init args={}")
        init_result = registry.execute("git_init", {})
        if not init_result.ok:
            msg = f"This workspace is not a git repository and `git init` failed: {init_result.message}"
            console.print(Panel(msg, title="Git"))
            _log_assistant_message(session_logger, msg, workflow_id="git-commit")
            return
        _ensure_shamsu_gitignore(workspace)
        console.print("[dim]Initialized a new git repository.[/dim]")
        status = registry.execute("git_status", {})
    if isinstance(status.data, dict) and not status.data.get("is_dirty", True):
        console.print(Panel("Nothing to commit - the working tree is clean.", title="Git"))
        _log_assistant_message(
            session_logger,
            "Nothing to commit - the working tree is clean.",
            workflow_id="git-commit",
        )
        return

    message = _extract_commit_message(user_input)
    if not message:
        message = "Update files"

    changed = status.data.get("changed_files", []) if isinstance(status.data, dict) else []
    preview = "\n".join(str(item) for item in changed[:20]) or "(all changes)"
    approved = True
    if approval_manager is not None:
        approved = approval_manager.ask(
            ApprovalRequest(
                action_type="run_command",
                description=f'Stage all changes and commit with message: "{message}"',
                risk_level="medium",
                preview=preview,
                working_dir=str(workspace),
                reason="Deterministic git add + commit requested by the user.",
            )
        )
    if not approved:
        console.print("[yellow]Commit cancelled - nothing was staged or committed.[/yellow]")
        _log_assistant_message(
            session_logger, "Commit cancelled by user.", workflow_id="git-commit"
        )
        return

    trace("tool=git_add_all args={}")
    add_result = registry.execute("git_add_all", {})
    trace(f'tool=git_commit args={{"message": "{message}"}}')
    commit_result = registry.execute("git_commit", {"message": message})

    lines = [_format_git_read_result("git_status", status)]
    if not add_result.ok:
        lines.append(f"git add failed: {add_result.message}")
    if commit_result.ok:
        lines.append(f"Committed with message: {message}")
        commit_out = (
            commit_result.data.get("stdout", "") if isinstance(commit_result.data, dict) else ""
        )
        if commit_out:
            lines.append(commit_out.strip())
    else:
        lines.append(f"git commit failed: {commit_result.message}")
    body = "\n\n".join(section for section in lines if section).strip()
    console.print(Panel(body, title="Git Commit"))
    _log_assistant_message(session_logger, body, workflow_id="git-commit")


async def _handle_git_request(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    agent_context: str = "",
) -> None:
    """Route a Git/repo request to the Git tools.

    Classified before web-search / QA / code-edit routing. Read-only requests
    are answered deterministically; mutation (or mixed) requests go to the tool
    agent, which is told to inspect the repo first and still runs every write
    through the existing command-safety/approval system."""
    init_request = _looks_like_git_init_request(user_input)
    mutation = is_git_mutation_request(user_input) or init_request
    read_only = is_read_only_git_request(user_input)
    route = "git_write" if mutation else "git_read"
    trace_mode = _trace_mode(workspace)

    def trace(line: str) -> None:
        if trace_mode != "quiet":
            console.print(f"[dim]{line}[/dim]")

    # A pure "initialize git" request (no commit) just inits the repo.
    if init_request and not _looks_like_git_add_commit_request(user_input):
        trace("route=git_write selected_workflow=git_init")
        _run_git_init(user_input, workspace, console, session_logger, trace)
        return

    trace(f"route={route} confidence=0.95 reason=git_override")
    _log_event(
        session_logger,
        "routing.git_override",
        {"route": route, "reason": "git_override", "read_only": read_only, "mutation": mutation},
        f"Git override routed to {route}",
        workflow_id="git",
    )

    if not mutation and read_only:
        trace("selected_workflow=git_read_only")
        _run_git_read_only(user_input, workspace, console, session_logger, trace)
        return

    # A plain "add and commit" is deterministic: never hand it to the LLM loop.
    if mutation and _looks_like_git_add_commit_request(user_input):
        trace("selected_workflow=git_add_commit")
        _run_git_add_commit(user_input, workspace, console, session_logger, trace)
        return

    trace("selected_workflow=agent_tools")
    guidance = _git_inspection_guidance(user_input)
    harness_input = _append_agent_context(user_input, guidance)
    if agent_context:
        harness_input = _append_agent_context(harness_input, agent_context)
    await _run_agent_chat(
        harness_input,
        workspace,
        console,
        session_logger=session_logger,
        user_request=user_input,
    )


_PRD_CONTEXT_QUESTION_PHRASES = (
    "what is this game about",
    "what's this game about",
    "what is the game about",
    "what is this app about",
    "what's this app about",
    "what is the app about",
    "what is this project about",
    "what's this project about",
    "what is the project about",
    "what is this product about",
    "what's this product about",
    "tell me about the game",
    "describe the game",
    "describe the project",
    "describe the product",
    "what does this game do",
    "what does the game do",
    "summarize the prd",
    "what does the prd say",
    "what's in the prd",
    "what is in the prd",
)


# Read/summarize-the-PRD requests: "what is the project about", "checkout the
# prd and tell me what it is", "summarize the prd". These must read the actual
# PRD text and summarize it - never route to git (checkout) or the tool loop.
_PRD_SUMMARY_TRIGGERS = (
    "what is the prd about",
    "what's the prd about",
    "whats the prd about",
    "what is this prd about",
    "what is the project about",
    "what's the project about",
    "whats the project about",
    "what is this project about",
    "what is this app about",
    "what is this product about",
    "what is this about",
    "what's this about",
    "tell me what is the project",
    "tell me what the project",
    "tell me about the prd",
    "tell me about the project",
    "tell me about this project",
    "summarize the prd",
    "summarise the prd",
    "what does the prd say",
    "what's in the prd",
    "what is in the prd",
    "read the prd",
    "check the prd",
    "checkout the prd",
    "check out the prd",
    "look at the prd",
    "review the prd",
    "explain the prd",
    "describe the project",
    "describe the prd",
    "what is the app about",
    "from the prd",
    "in the prd",
    "inside the prd",
    "according to the prd",
    "according to prd",
    "use the prd",
    "based on the prd",
    "look in the prd",
)


def _looks_like_prd_summary_request(user_input: str, workspace: Path) -> bool:
    text = user_input.lower()
    if _looks_like_prd_plan_request(user_input):
        return False
    if _looks_like_prd_schema_build_request(user_input):
        return False
    explicit_prd = _extract_prd_path_from_prompt(user_input)
    explicit_summary = (
        bool(explicit_prd)
        and is_prd_filename(Path(explicit_prd.lstrip("@")).name)
        and any(
            verb in text
            for verb in ("summarize", "summarise", "explain", "describe", "review", "read")
        )
    )
    prd_reference_question = (
        "prd" in text
        and any(
            phrase in text
            for phrase in (
                "find",
                "look",
                "tell me",
                "what",
                "which",
                "where",
                "who",
                "how",
                "requirements",
                "features",
                "roles",
                "tech stack",
                "constraints",
            )
        )
    )
    if (
        not explicit_summary
        and not prd_reference_question
        and not any(trigger in text for trigger in _PRD_SUMMARY_TRIGGERS)
    ):
        return False
    # A build verb ("build/implement the prd") is a build request, not a read.
    if any(verb in text for verb in ("build ", "implement ", "generate ", "scaffold ")):
        return False
    return bool(_find_workspace_prd_files(workspace))


async def _handle_prd_summary_request(
    user_input: str,
    workspace: Path,
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
    thinking_status: Any = None,
) -> None:
    """Read the workspace PRD (incl. PDF text) and summarize what the project is
    about in one model call - no tools, no git, no stall."""
    prd_path = _resolve_build_prd(user_input, workspace)
    if prd_path is None:
        candidates = _find_workspace_prd_files(workspace)
        if len(candidates) > 1:
            console.print("[yellow]I found multiple PRD files - which one?[/yellow]")
            for path in candidates[:10]:
                console.print(f"- {path.as_posix()}")
            return
        if not candidates:
            console.print("[yellow]I couldn't find a PRD file in this workspace.[/yellow]")
            return
        prd_path = workspace / candidates[0]

    try:
        parsed = parse_prd_file(prd_path)
    except PRDParseError as exc:
        # Extraction failed (image-only/encrypted PDF, etc). Be honest instead of
        # guessing - do not hallucinate a summary.
        console.print(
            Panel(
                f"Low-confidence PRD extraction: {exc}\n\n"
                "I could not reliably read the PRD text, so I won't guess what it's about. "
                "Try a text/Markdown PRD, or an unencrypted, text-based PDF.",
                title="PRD",
                border_style="yellow",
            )
        )
        return

    try:
        relative_path = prd_path.relative_to(workspace).as_posix()
    except ValueError:
        relative_path = prd_path.name
    prd_text = (parsed.raw_text or _render_sections(parsed)).strip()
    if not prd_text:
        console.print("[yellow]The PRD appears to be empty.[/yellow]")
        return

    _log_event(
        session_logger,
        "prd.summary.requested",
        {"path": str(prd_path), "title": parsed.title},
        f"Summarizing PRD {prd_path.name}",
        workflow_id="prd-summary",
    )
    pack = ContextPack(
        task_id="prd-summary",
        step_id=1,
        specialist="qa",
        user_request=(
            f"Answer this request using only the PRD text. File: {relative_path}.\n"
            f"User request: {user_input}\n\n"
            "If the request asks for specific information, answer that directly. "
            "If it asks generally, give: (1) one-line purpose, (2) main features/requirements, "
            "(3) stated tech stack or constraints. Be concise and do not invent beyond the PRD."
        ),
        prd_context=f"PRD content ({relative_path}):\n\n{prd_text[:12000]}",
    )
    if hasattr(llm, "run_specialist_stream"):
        try:
            streamed, _text = await _stream_answer(
                console, llm, pack, "PRD Summary", session_logger, "prd-summary", thinking_status
            )
        except Exception:
            streamed = False
        if streamed:
            _audit_simple_turn(workspace, session_logger, "prd_summary", user_input, _text)
            return
    response = await llm.run_specialist("qa", pack)
    body = response.raw.strip() or "No summary produced."
    console.print(Panel(body, title=f"PRD Summary - {parsed.title}"))
    _log_assistant_message(session_logger, body, workflow_id="prd-summary")
    _audit_simple_turn(workspace, session_logger, "prd_summary", user_input, body)


def _looks_like_prd_context_question(user_input: str, workspace: Path) -> bool:
    """Detect questions about the game/project that should be answered from the PRD.

    Only triggers when a PRD actually exists in the workspace - otherwise
    there is nothing to read and the question should go through normal routing.
    """
    text = user_input.lower()
    if not any(phrase in text for phrase in _PRD_CONTEXT_QUESTION_PHRASES):
        return False
    return bool(_find_workspace_prd_files(workspace))


_WEB_FUZZY_KEYWORDS = ("weather", "forecast", "temperature")


def _has_fuzzy_web_keyword(text: str) -> bool:
    """Catches common typos (e.g. "weither" for "weather") that exact
    substring matching above would otherwise silently miss, sending the
    prompt into a tool-less chat path that hallucinates a fake search."""
    words = re.findall(r"[a-z]+", text)
    return any(
        difflib.get_close_matches(word, _WEB_FUZZY_KEYWORDS, n=1, cutoff=0.8) for word in words
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
    return extract_document_reference(user_input)


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


# What SHAMSU says it is about to do, keyed by routed intent. Stating the plan
# in plain language reads far more naturally than the old `intent=qa
# confidence=0.35` dump, which leaked internal routing jargon at the user.
_INTENT_ACTIVITY = {
    "qa": "Looking through the workspace to answer that.",
    "explain": "Reading the relevant code to explain that.",
    "code_edit": "Planning the change - I'll show a diff before applying anything.",
    "bug_fix": "Tracing the error to find and fix the root cause.",
    "test_gen": "Working out what to cover and writing the tests.",
    "audit": "Auditing the project for issues.",
    "doc_gen": "Updating the documentation.",
    "generate": "Planning the project from the PRD before generating files.",
}


def _print_decision(decision: RoutingDecision, console: Console, verbose: bool = False) -> None:
    activity = _INTENT_ACTIVITY.get(
        decision.intent, f"Working on this as a {decision.intent} task."
    )
    console.print(f"[cyan]{activity}[/cyan]")
    # Keep the raw routing signal available for debugging, but only when the
    # user has explicitly opted into verbose trace output.
    if verbose:
        console.print(f"[dim]intent={decision.intent} confidence={decision.confidence:.2f}[/dim]")


def _print_workspace_location(workspace: Path, console: Console) -> str:
    message = f"I am working in:\n{workspace}"
    console.print(Panel(message, title="Current Workspace"))
    return message


def _print_workspace_files(workspace: Path, console: Console, limit: int = 20) -> str:
    entries = sorted(
        [path for path in workspace.iterdir() if path.name != ".shamsu"],
        key=lambda path: (not path.is_dir(), path.name.lower()),
    )
    if not entries:
        message = f"{workspace}\n\nThis workspace is empty."
        console.print(Panel(message, title="Workspace Files"))
        return message
    shown = entries[:limit]
    body = "\n".join(
        f"[dir]  {item.name}" if item.is_dir() else f"[file] {item.name}" for item in shown
    )
    if len(entries) > limit:
        body = f"{body}\n... {len(entries) - limit} more"
    message = f"Workspace: {workspace}\n\n{body}"
    console.print(Panel(message, title="Workspace Files"))
    return message


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
    for path in walk_workspace_files(workspace):
        if not (is_prd_filename(path.name) or path.name.lower() == "requirements.md"):
            continue
        try:
            candidates.append(path.relative_to(workspace))
        except ValueError:
            continue
    return sorted(candidates)


def _handle_workspace_prd_request(workspace: Path, console: Console) -> str:
    candidates = _find_workspace_prd_files(workspace)
    if not candidates:
        message = (
            "I couldn't find a PRD file in this workspace yet. "
            "Add a `.md`, `.txt`, `.pdf`, or `.docx` PRD (e.g. named `*prd*` or `Product Requirements*`), "
            "then ask again or run `/parse-prd <file>`."
        )
        console.print(f"[yellow]{message}[/yellow]")
        return message
    if len(candidates) > 1:
        listing = "\n".join(f"- {path}" for path in candidates[:10])
        message = (
            f"I found multiple PRD files in this workspace:\n{listing}\n\n"
            "Tell me which one to open, or run `/parse-prd <file>` or `/plan-prd <file>`."
        )
        console.print("[yellow]I found multiple PRD files in this workspace:[/yellow]")
        for path in candidates[:10]:
            console.print(f"- {path}")
        console.print(
            "Tell me which one to open, or run `/parse-prd <file>` or `/plan-prd <file>`."
        )
        return message
    relative_path = candidates[0]
    absolute_path = workspace / relative_path
    try:
        parsed = parse_prd_file(absolute_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        return str(exc)
    section_names = ", ".join(parsed.sections.keys()) or "none"
    message = (
        f"File: {relative_path}\n"
        f"Title: {parsed.title}\n"
        f"Sections: {section_names}\n\n"
        f'Use `/plan-prd "{relative_path}"` if you want me to turn it into a project plan.'
    )
    console.print(Panel(message, title="PRD Found"))
    return message


_PRD_BUILD_VERBS = ("build", "finish", "implement", "generate", "make", "create", "develop")
_PRD_BUILD_NOUNS = (
    "product",
    "app",
    "application",
    "game",
    "project",
    "website",
    "site",
    "it",
    "this",
    "prd",
)
# Matched on WORD boundaries, not as substrings. The plain `noun in text` this
# replaced meant the "it" entry matched inside with/quit/write/site/edit - so
# the noun half of the build test was satisfied by almost any English sentence,
# leaving `_resolve_build_prd` as the only thing standing between an ordinary
# prompt and a full product build.
_PRD_BUILD_NOUN_RE = re.compile(r"\b(?:" + "|".join(_PRD_BUILD_NOUNS) + r")s?\b", re.IGNORECASE)

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
    "fix",
    "build",
    "make",
    "create",
    "add",
    "implement",
    "update",
    "change",
    "edit",
    "modify",
    "write",
    "refactor",
    "rewrite",
    "improve",
    "apply",
    "generate",
    "install",
    "setup",
    "scaffold",
    "bootstrap",
    "initiate",
    "initialize",
    "initialise",
    "configure",
    "continue",
    "finish",
    "complete",
    "proceed",
    "resume",
    "redo",
    "regenerate",
    "review",
    "debug",
    "resolve",
    "correct",
    "patch",
    "run",
    "start",
    "stop",
    "restart",
    "execute",
    "launch",
    "open",
    "deploy",
    "compile",
    "rebuild",
}

# Leading phrasing that marks a genuine question/explanation: keep it on QA.
_QUESTION_PREFIXES = (
    "what",
    "why",
    "how",
    "when",
    "where",
    "who",
    "which",
    "whose",
    "explain",
    "tell me",
    "show me",
    "describe",
    "list ",
    "summarize",
    "is ",
    "are ",
    "was ",
    "were ",
    "does ",
    "do you",
    "did you",
    "should i",
    "can you explain",
    "could you explain",
    "whats",
    "hows",
)


# Polite-request openers: "can you get rid of the sidebar" is work, not a
# question, even though it is question-shaped. The explain-style forms stay
# questions via _QUESTION_PREFIXES ("can you explain" is listed there and is
# checked FIRST in _prefers_qa_answer).
_POLITE_REQUEST_PREFIXES = ("can you", "could you", "would you", "will you", "please ")

# Additional interrogative openers beyond _QUESTION_PREFIXES.
_QUESTION_OPENERS = ("do ", "did ", "has ", "have ", "am i", "will ", "would ", "could ", "should ")


def _prefers_qa_answer(user_input: str) -> bool:
    """True when the tool-less QA specialist is the RIGHT destination: a
    genuine question (or casual chat) - not work phrased without an action verb.

    QA used to be the catch-all for everything the action-verb list missed
    (gap B1): "the login page needs a dark mode" or "hook the form up to the
    api" got a *description* from the tool-less brain instead of the change.
    Adding verbs to the list is whack-a-mole - the architecture guarantees the
    next miss. So the default flips: QA must be EARNED by question shape, and
    everything else goes to the agent loop, which has tools and can ask (J6).
    Misrouting a question to the loop costs latency; misrouting work to QA
    produces a confidently useless answer. The loop is the safe side.
    """
    raw = user_input.strip().lower()
    if not raw:
        return True
    if _is_casual_prompt(raw):
        return True
    # Question-style openers win first so "can you explain X" stays QA...
    if raw.startswith(_QUESTION_PREFIXES):
        return True
    # ...and remaining polite forms ("can you get rid of...") are requests.
    if raw.startswith(_POLITE_REQUEST_PREFIXES):
        return False
    if raw.endswith("?"):
        return True
    if raw.startswith(_QUESTION_OPENERS):
        return True
    # A short verb-less fragment ("charge card", "auth flow") is a lookup -
    # the user pointing at something they want explained - not work. Anything
    # with an action verb was already caught by _looks_like_action_request.
    words = raw.split()
    return len(words) <= 3 and not (_ACTION_VERBS & set(words))


def _qa_branch_routes_to_agent(effective_input: str, uses_real_index: bool) -> bool:
    """The qa/explain tail's actual decision, extracted so it is testable:
    True -> the tool-having agent loop, False -> the tool-less QA specialist."""
    if _explicitly_read_only(effective_input):
        return False
    return (
        _looks_like_action_request(effective_input)
        or _looks_like_trouble_report(effective_input)
        or (_is_general_chat_prompt(effective_input) and not uses_real_index)
        or not _prefers_qa_answer(effective_input)
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
    if _explicitly_read_only(user_input):
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
    if "do" in words and words & {
        "thing",
        "things",
        "work",
        "stuff",
        "rest",
        "task",
        "tasks",
        "job",
    }:
        return True
    return False


# "It's broken" reports and pasted error/stack-trace logs. These are implicit
# fix requests: the user is showing a problem, not asking a question, so they
# must reach the tool-having agent loop, which can read the files and repair,
# not the tool-less QA brain that returns a troubleshooting checklist.
_TROUBLE_SIGNALS = (
    "not working",
    "cant see",
    "can't see",
    "cannot see",
    "doesnt work",
    "doesn't work",
    "does not work",
    "not showing",
    "nothing happens",
    "nothing shows",
    "still broken",
    "still not",
    "blank page",
    "blank screen",
    "white screen",
    "wont run",
    "won't run",
    "crashes",
    "not rendering",
    "isnt working",
    "isn't working",
    "no game",
    "page is blank",
)
_ERROR_LOG_SIGNALS = (
    "error:",
    "cannot find",
    "is not exported",
    "has no exported member",
    "failed to compile",
    "uncaught",
    "syntaxerror",
    "referenceerror",
    "typeerror",
    "module not found",
    "cannot find module",
    "unexpected token",
    "traceback (most recent",
    "does not exist on type",
    "does not provide an export named",
    "no exported member",
    "stack trace",
    " at ",
    "ts2305",
    "ts2724",
    "ts2339",
    "ts2307",
    "ts(",
    "vite:",
    "[plugin:",
)


def _looks_like_trouble_report(user_input: str) -> bool:
    low = user_input.lower()
    return any(s in low for s in _TROUBLE_SIGNALS) or any(s in low for s in _ERROR_LOG_SIGNALS)


def _bugfix_request_has_actionable_target(user_input: str) -> bool:
    """Bug-fix workflows need a concrete file, traceback, command, or error.

    Vague prompts like "fix a code for me" make small local models guess and
    often end in an empty workflow error. Ask for the missing target instead.
    """
    normalized = _strip_forced_prefix(user_input, "fix")
    text = normalized.lower()
    if _FILELIKE_RE.search(normalized):
        return True
    if _looks_like_trouble_report(normalized):
        return True
    return any(
        signal in text
        for signal in (
            "failing command",
            "failing test",
            "test failed",
            "tests failed",
            "exit code",
            "assertionerror",
            "exception",
        )
    )


def _bugfix_report_from_last_failure(
    user_input: str, session_logger: SessionLogger | None
) -> tuple[str, str] | None:
    """Build a bug-fix report from the last failing command + errors stored in
    session memory, or None if there is nothing to reuse. Returns (report,
    command) so the caller can tell the user which command it reused."""
    if session_logger is None:
        return None
    try:
        failure = session_logger.get_last_failure()
    except Exception:
        return None
    if failure.get("actionable") is False:
        return None
    if str(failure.get("classification", "command_failure")) != "command_failure":
        return None
    if str(failure.get("source", "user_command")) != "user_command":
        return None
    command = str(failure.get("command", "")).strip()
    errors = str(failure.get("errors", "")).strip()
    if not errors and not command:
        return None
    exit_code = failure.get("exit_code", "")
    intent = (
        _strip_forced_prefix(user_input, "fix").strip() or "Repair the reported build/test failure."
    )
    report = (
        f"{intent}\n\n"
        f"Last failing command: {command or '(unknown)'}\n"
        f"Exit code: {exit_code}\n\n"
        f"Errors / output from that command:\n{errors or '(no captured output)'}"
    )
    return report, command or "(unknown)"


_FILE_WRITE_VERBS = {
    "create",
    "write",
    "save",
    "generate",
    "make",
    "add",
    "edit",
    "update",
    "modify",
    "overwrite",
    "fix",
    "repair",
    "change",
}

_FILE_HINT_WORDS = {
    "file",
    "files",
    "script",
    "component",
    "module",
    "readme",
    "gitignore",
    "dockerfile",
    "makefile",
    "procfile",
    "env",
    "npmrc",
    "config",
    "page",
    "class",
    "test",
    "tests",
}

_FILELIKE_RE = re.compile(
    r"(?:^|\s|['\"`@])(?:[A-Za-z0-9_. -]+[/\\])*[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12}(?:\s|$|['\"`,.;:])"
)
_SPECIAL_FILELIKE_RE = re.compile(
    r"(?:^|\s|['\"`@])(?:[A-Za-z0-9_. -]+[/\\])*(?:"
    r"Dockerfile(?:\.[A-Za-z0-9_.-]+)?|"
    r"Makefile(?:\.[A-Za-z0-9_.-]+)?|"
    r"Procfile|"
    r"\.env(?:\.[A-Za-z0-9_.-]+)?|"
    r"\.dockerignore|\.gitignore|\.gitattributes|\.npmrc|\.nvmrc|"
    r"\.prettierrc|\.eslintrc|\.babelrc|\.editorconfig"
    r")(?:\s|$|['\"`,.;:])",
    re.IGNORECASE,
)


def _has_filelike_token(user_input: str) -> bool:
    return bool(_FILELIKE_RE.search(user_input) or _SPECIAL_FILELIKE_RE.search(user_input))


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
    # "Make a step by step plan for PRD.md" is a plan request, not a file write -
    # but "make" is a write verb and "PRD.md" is file-like, so it matched here
    # and skipped planning entirely. A plan intent is never a raw file write.
    if _looks_like_plan_intent(user_input):
        return False
    words = set(re.sub(r"[^\w\s]", " ", raw).split())
    if not (_FILE_WRITE_VERBS & words):
        return False
    if _has_filelike_token(user_input):
        return True
    return bool(words & _FILE_HINT_WORDS)


def _looks_like_file_read_request(user_input: str) -> bool:
    """Route explicit file inspection prompts to a deterministic read first.

    Small local models often answer "inspect/read file.py" from QA memory unless
    the program reads the file before the model gets the turn.
    """
    raw = user_input.strip().lower()
    if not raw or not _has_filelike_token(user_input):
        return False
    words = set(re.sub(r"[^\w\s]", " ", raw).split())
    read_verbs = {
        "read",
        "open",
        "inspect",
        "explain",
        "summarize",
        "summarise",
        "parse",
        "review",
        "analyze",
        "analyse",
    }
    return "read_file" in raw or bool(words & read_verbs)


def _looks_like_docs_ingest_request(user_input: str) -> bool:
    """Recognize requests to retain provided docs for future coding tasks."""
    text = user_input.strip().lower()
    if not text:
        return False
    ingest_intent = any(
        phrase in text
        for phrase in (
            "ingest ",
            "import documentation",
            "import docs",
            "add as a reference",
            "add this reference",
            "register documentation",
            "register docs",
            "save as a reference",
            "remember this documentation",
            "remember these docs",
        )
    )
    if not ingest_intent:
        return False
    source_signal = bool(_has_filelike_token(user_input)) or "http://" in text or "https://" in text
    doc_signal = any(
        word in text for word in ("doc", "documentation", "reference", "manual", "guide", "library")
    )
    return source_signal and doc_signal


def _looks_like_docs_query_request(user_input: str) -> bool:
    """Recognize explicit questions/searches over previously registered docs."""
    text = user_input.strip().lower()
    if not text or _looks_like_docs_ingest_request(user_input):
        return False
    doc_signal = bool(
        re.search(
            r"\b(registered (?:doc|docs|document|documents)|documentation|docs|manual|"
            r"library reference)\b",
            text,
        )
    )
    query_signal = any(
        phrase in text
        for phrase in (
            "ask the ",
            "according to ",
            "look up in ",
            "search docs",
            "search the docs",
            "search documentation",
            "summarize the manual",
            "summarise the manual",
            "summarize the registered",
            "summarise the registered",
            "what do the docs",
            "what does the manual",
        )
    )
    return doc_signal and query_signal


def _required_docs_tool(user_input: str) -> str:
    text = user_input.lower()
    if re.search(r"\b(summarize|summarise|summary)\b", text):
        return "summarize_docs"
    if re.search(r"\b(search|find|look up)\b", text):
        return "search_docs"
    return "ask_docs"


# Self-contained "write me some code" asks that need no workspace context.
# These must answer directly from the model (fast) instead of entering the
# planner + tool loop, which used to only produce a plan and then time out on a
# trivial "print the first 100 primes" request.
_DIRECT_CODE_NOUNS = (
    "code",
    "function",
    "snippet",
    "script",
    "program",
    "programme",
    "regex",
    "one-liner",
    "oneliner",
    "algorithm",
    "class",
    "method",
    "loop",
)
_DIRECT_CODE_PRODUCE_VERBS = (
    "write",
    "give me",
    "show me",
    "generate",
    "create",
    "print",
    "implement",
    "make",
    "provide",
    "produce",
    "code for",
    "how do i write",
    "how to write",
)
# Signals the ask is actually about the workspace/files, so it must NOT be a
# direct answer (it needs the tool loop instead).
_DIRECT_CODE_WORKSPACE_SIGNALS = (
    "save",
    "to a file",
    "into a file",
    "in a file",
    "create a file",
    "write a file",
    "in the workspace",
    "in my workspace",
    "in this project",
    "in the project",
    "to the repo",
    "add to",
    "run it",
    "run this",
    "run the",
    "execute",
    "test it",
    "edit ",
    " open ",
    "commit",
    "existing",
    "this file",
    "that file",
)


# Capitalized words that are languages, frameworks or tools rather than the
# user's own domain types, so "write a Python function" stays a self-contained
# question while "implement Spaceship and Bullet classes" does not.
_DIRECT_CODE_TECH_NOUNS = {
    "python", "javascript", "typescript", "java", "kotlin", "swift", "go",
    "golang", "rust", "ruby", "php", "perl", "scala", "haskell", "elixir",
    "sql", "html", "css", "bash", "powershell", "react", "vue", "angular",
    "svelte", "django", "flask", "fastapi", "express", "node", "nodejs",
    "pygame", "numpy", "pandas", "pytorch", "tensorflow", "docker", "git",
    "linux", "windows", "macos", "postgres", "postgresql", "mysql", "sqlite",
    "redis", "mongodb", "api", "json", "yaml", "csv", "http", "https", "rest",
    "i", "a", "the", "write", "implement", "create", "add", "build", "make",
}
_DOMAIN_TYPE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\b")


def _names_workspace_domain_types(user_input: str, workspace: Path) -> bool:
    """True when the request names capitalized domain types in a live project.

    "Implement Spaceship and Bullet classes" is a workspace edit, not a coding
    quiz - but with no filename in it, it looked identical to "write python for
    the first 100 primes" and was answered in chat while the project sat
    untouched. Proper-noun types plus an existing codebase is the distinction.
    """
    candidates = [
        word
        for word in _DOMAIN_TYPE_RE.findall(user_input)
        if word.lower() not in _DIRECT_CODE_TECH_NOUNS
    ]
    if not candidates:
        return False
    try:
        return bool(_workspace_file_inventory_for_preflight(workspace, limit=5))
    except Exception:
        return False


def _looks_like_direct_code_request(user_input: str, workspace: Path | None = None) -> bool:
    """A self-contained coding question ("write python to print the first 100
    primes") that should be answered directly by the model, without the planner
    or the file/tool loop. File-writing and workspace requests are excluded."""
    text = user_input.strip().lower()
    if not text:
        return False
    # Explicit file writes / workspace ops are handled by the tool loop.
    if _looks_like_file_write_request(user_input):
        return False
    if _FILELIKE_RE.search(user_input):
        return False
    if any(signal in text for signal in _DIRECT_CODE_WORKSPACE_SIGNALS):
        return False
    if workspace is not None and _names_workspace_domain_types(user_input, workspace):
        return False
    produce = any(verb in text for verb in _DIRECT_CODE_PRODUCE_VERBS)
    code_noun = any(noun in text for noun in _DIRECT_CODE_NOUNS)
    return produce and code_noun


async def _run_direct_code_answer(
    user_input: str,
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
    thinking_status: Any = None,
) -> str:
    """Answer a self-contained coding question in one model call - no planner,
    no tool loop. Returns the answer so the caller can record the real turn
    (it used to audit an empty string, losing the answer from the trail and
    the transcript). Streams when the manager supports it so the code appears
    immediately instead of after a full agent run."""
    pack = ContextPack(
        task_id="direct-code",
        step_id=1,
        specialist="qa",
        user_request=user_input,
        prd_context=(
            "The user asked a self-contained coding question that needs no access to their "
            "workspace or files. Answer directly: provide a complete, correct, runnable code "
            "solution in a single fenced code block, with a one or two sentence explanation. "
            "Do not claim to have created, saved, or run any files - just return the code. "
            + NO_LIVE_TOOLS_NOTICE
        ),
    )
    if hasattr(llm, "run_specialist_stream"):
        try:
            streamed, _text = await _stream_answer(
                console, llm, pack, "Code", session_logger, "direct-code", thinking_status
            )
        except Exception:
            streamed = False
        if streamed:
            return _text
    response = await llm.run_specialist("qa", pack)
    body = response.raw.strip() or "No response returned."
    title = f"Code ({response.model_used})" if response.model_used else "Code"
    console.print(Panel(body, title=title))
    _log_assistant_message(session_logger, body, workflow_id="direct-code")
    return body


async def _handle_file_read_request(
    user_input: str,
    workspace: Path,
    console: Console,
    llm: LLMManager,
    session_logger: SessionLogger | None = None,
    thinking_status: Any = None,
) -> None:
    filepath = _extract_requested_file_path(user_input)
    if not filepath:
        message = "No file path was found in the request."
        console.print(Panel(message, title="File Read", border_style="red"))
        _log_assistant_message(session_logger, message, workflow_id="file.read")
        return

    tools = AgentToolRegistry(workspace, session_logger=session_logger)
    result = tools.read_file(filepath)
    if not result.ok:
        candidates = result.data.get("candidates") if isinstance(result.data, dict) else None
        detail = f"read_file {filepath} failed: {result.message}"
        if candidates:
            detail += "\n\nCandidates:\n" + "\n".join(
                f"- {candidate}" for candidate in candidates[:10]
            )
        console.print(Panel(detail, title="File Read Failed", border_style="red"))
        _log_assistant_message(session_logger, detail, workflow_id="file.read")
        return

    content = str(result.data.get("content", "")) if isinstance(result.data, dict) else ""
    if not content.strip():
        message = f"read_file {filepath} returned no readable content."
        console.print(Panel(message, title="File Read", border_style="yellow"))
        _log_assistant_message(session_logger, message, workflow_id="file.read")
        return

    grounded_request = (
        "The file has already been read successfully with read_file. "
        "Answer using only the file content below. Do not answer from memory, "
        "do not say the file is unavailable, and keep any requested length limit.\n\n"
        f"User request:\n{user_input}\n\n"
        f"File path: {filepath}\n"
        f"read_file result: {result.message}\n\n"
        "File content:\n"
        f"{content}"
    )
    pack = ContextPack(
        task_id="file-read",
        step_id=1,
        specialist="qa",
        user_request=grounded_request,
        prd_context=grounded_request,
    )
    if hasattr(llm, "run_specialist_stream"):
        try:
            streamed, _text = await _stream_answer(
                console, llm, pack, "File Answer", session_logger, "file.read", thinking_status
            )
        except Exception:
            streamed = False
        if streamed:
            return
    response = await llm.run_specialist("qa", pack)
    body = response.raw.strip() or "No response returned."
    title = f"File Answer ({response.model_used})" if response.model_used else "File Answer"
    console.print(Panel(body, title=title))
    _log_assistant_message(session_logger, body, workflow_id="file.read")


def _audit_simple_turn(
    workspace: Path,
    session_logger: SessionLogger | None,
    route: str,
    prompt: str,
    final: str,
) -> None:
    """Record a non-tool-loop turn (direct code, QA, PRD summary, git read) in
    the detailed audit trail AND in the session transcript.

    The transcript half matters for continuity: `ChatState` hydrates the agent
    loop from `messages.jsonl`, but only the loop itself ever wrote there
    (`chat_state._append`). So anything answered WITHOUT the loop - a QA answer,
    a PRD summary, a direct-code reply - was invisible to the next agent run:
    ask "what does game.js do?" then "add a pause button", and the agent had no
    idea what was just discussed. Writing both sides here closes that hole
    without double-appending, since the loop persists its own turns and never
    calls this. Best-effort: never let bookkeeping break a response."""
    try:
        session_id = session_logger.session_id if session_logger is not None else None
        audit = SessionAuditLog(workspace, session_id)
        audit.log_prompt(prompt)
        audit.log_route(route, workflow=route)
        audit.log_final(final)
    except Exception as exc:
        swallowed.record("repl.audit_simple_turn", exc)
    if session_logger is None:
        return
    try:
        if prompt.strip():
            session_logger.append_message("user", prompt)
        if final.strip():
            session_logger.append_message("assistant", final)
    except Exception as exc:
        swallowed.record("repl.transcript_simple_turn", exc)


def _looks_like_run_game_request(user_input: str) -> bool:
    text = user_input.lower()
    has_run = any(word in text for word in ("run", "start", "launch", "serve", "open"))
    has_game = any(word in text for word in ("game", "app", "site", "preview", "link", "access"))
    return has_run and has_game


_DEV_SERVER_FAIL_PHRASES = (
    "didnt run",
    "didn't run",
    "did not run",
    "didnt start",
    "didn't start",
    "did not start",
    "failed to start",
    "wont start",
    "won't start",
    "not starting",
    "not running yet",
    "dev server failed",
    "server failed",
)


def _looks_like_dev_server_failure(user_input: str) -> bool:
    """Detect follow-up messages indicating the last dev-server launch failed.

    Examples:
      "it didnt run btw"  -> True
      "it didn't start"   -> True
      "the dev server failed to start" -> True
    """
    text = user_input.lower()
    if not any(phrase in text for phrase in _DEV_SERVER_FAIL_PHRASES):
        return False
    # Short follow-up OR explicitly mentions a dev/run concept.
    dev_words = ("run", "server", "dev", "launch", "start", "npm", "vite", "node")
    return len(user_input.split()) <= 12 or any(word in text for word in dev_words)


def _handle_dev_server_recovery(
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    """Re-attempt the last/inferred dev-server command after a reported failure."""
    manager = DevServerManager(
        workspace,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
        session_logger=session_logger,
    )
    active = manager.status()
    if active:
        for server in active:
            console.print(f"[dim]Previously launched: {server.command} (PID {server.pid})[/dim]")
    command = infer_dev_command(workspace)
    console.print(f"[dim]Attempting to (re)launch dev server: {command}[/dim]")
    result = manager.start(command)
    if result.launched:
        console.print(
            Panel(
                f"Command: {result.command}\nURL: {result.url}",
                title="Dev Server Recovery - Started",
                border_style="green",
            )
        )
    elif result.duplicate:
        console.print(
            Panel(
                f"The dev server appears to already be running.\n"
                f"Command: {result.command}\nURL: {result.url}",
                title="Dev Server - Already Running",
                border_style="yellow",
            )
        )
    else:
        console.print(
            Panel(
                f"Command attempted: {command}\nReason: {result.message}\n\n"
                "Check that no process is holding open the port and that dependencies are installed.",
                title="Dev Server Recovery - Failed",
                border_style="red",
            )
        )


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
    """Extract the actual shell command from user input.

    Handles natural-language sentences like
    "can you run npm run dev in a new terminal window" -> "npm run dev".
    Falls back to inferring from workspace package.json when no command found.
    """
    normalized = _normalize_command_input(user_input).strip()
    # Try to extract a command embedded in a natural-language sentence first.
    extracted = extract_dev_command_from_sentence(normalized)
    if extracted:
        return extracted
    # If the normalized input IS a bare command (no extra words), use it directly.
    if is_dev_server_command(normalized):
        return normalized
    return infer_dev_command(workspace)


def _looks_like_affirmative_continue(user_input: str) -> bool:
    text = strip_filler_prefix(re.sub(r"[^\w\s]", " ", user_input.lower()).strip())
    if not text:
        return False
    return text in {
        "yes",
        "yes please",
        "yeah",
        "yep",
        "ok",
        "okay",
        "sure",
        "continue",
        "go ahead",
        "do it",
        "proceed",
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
        dev = _start_background_command(
            "npm run dev", workspace, log_dir / "dev.log", visible_console=True
        )
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
            session_logger,
            "project.preview.started",
            {"url": url, "dev_pid": dev.pid, "logs": str(log_dir)},
            f"Started game preview at {url}",
            workflow_id="game-preview",
        )
    else:
        relay = _start_background_command(
            "npm run dev:relay", workspace, log_dir / "relay.log", visible_console=True
        )
        vite = _start_background_command(
            "npm run dev", workspace, log_dir / "vite.log", visible_console=True
        )
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
            session_logger,
            "project.preview.started",
            {"url": url, "vite_pid": vite.pid, "relay_pid": relay.pid, "logs": str(log_dir)},
            f"Started game preview at {url}",
            workflow_id="game-preview",
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
    "failed to resolve import",
    "internal server error",
    "pre-transform error",
    "could not resolve",
    "transform failed",
    "[plugin:",
    "has no exported member",
    "is not exported",
    "cannot find module",
    "cannot find name",
    "unexpected token",
    "syntaxerror",
    "referenceerror",
    "error ts",
    "tsc: error",
    "npm err!",
    "module not found",
    "failed to compile",
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
    signal (the word prd/product requirements, or a doc named in the prompt).
    This keeps narrow prompts like "build the navbar" or "fix the build" from
    triggering a full autonomous product build.
    A terse "do the task"/"continue" also counts when exactly one PRD is present
    - in a PRD workspace that almost always means "build that PRD", and the
    build is approval-gated anyway so it is safe to route here.

    Detection is INTENT-only: it deliberately does not require the PRD file to
    resolve. Resolving is `_handle_prd_build_request`'s job, and it reports
    honestly (which of several PRDs? none found?). Gating detection on
    resolution used to drop an unambiguous "build the app from the PRD" through
    ~15 further rules into the tool-less QA brain - which then just *talks*
    about building instead of building. A PRD that exists but isn't *named*
    "prd" (e.g. `spec.md`) hit this on every prompt.
    """
    # "Plan the implementation from PRD.md" asks for a PLAN. It used to reach
    # here and match a build because "implementation" contains "implement" - so
    # a plan request kicked off a full autonomous build (and, pre-fix, wrote a
    # .gitignore). A plan intent is never a build.
    if _looks_like_plan_intent(user_input):
        return False
    if _looks_like_prd_schema_build_request(user_input):
        return True
    if _looks_like_prd_milestone_execution_request(user_input):
        return True
    if (
        _looks_like_vague_action_request(user_input)
        and _resolve_build_prd(user_input, workspace) is not None
    ):
        return True
    explicit_path = _extract_prd_path_from_prompt(user_input)
    if (
        explicit_path
        and _looks_like_file_write_request(user_input)
        and _resolve_build_prd(user_input, workspace) is None
    ):
        # "Create NOTES.md from the PRD" names an output file, not the PRD to
        # build. The explicit "prd" word used to bypass _resolve_build_prd's
        # named-output protection and launch the full autonomous builder.
        return False
    text = user_input.lower()
    words = re.sub(r"[^\w\s]", " ", text).split()
    has_build_verb = any(verb in text for verb in _PRD_BUILD_VERBS) or _has_fuzzy_word(
        words, _PRD_BUILD_VERBS
    )
    has_product_noun = bool(_PRD_BUILD_NOUN_RE.search(text))
    if not (has_build_verb and has_product_noun):
        return False
    if "prd" in text or "product requirements" in text or "requirements document" in text:
        return True
    # No PRD wording, but the prompt names a doc directly ("build the app from
    # spec.md"): still a build-from-spec request, even though the filename would
    # never pass `is_prd_filename`. Requires it to actually resolve, since
    # there is no explicit "prd" in the prompt to vouch for the intent.
    if _extract_prd_path_from_prompt(user_input):
        return _resolve_build_prd(user_input, workspace) is not None
    return False


def _has_fuzzy_word(words: list[str], targets: tuple[str, ...], cutoff: float = 0.78) -> bool:
    for word in words:
        if difflib.get_close_matches(word, targets, n=1, cutoff=cutoff):
            return True
    return False


def _resolved_prd_reference(user_input: str, workspace: Path) -> str:
    """A workspace-relative PRD path to name in a plan prompt.

    The bare-document pattern truncates an unquoted mention whose filename
    contains spaces (`@canvas lite.pdf` -> `lite.pdf`), so the plan route told
    the agent to read a file that does not exist and it stopped to ask what
    that document was (observed live 2026-08-02). The mention-aware resolver
    reassembles the real name; fall back to the raw token only when nothing
    resolves.
    """
    resolved = _resolve_build_prd(user_input, workspace)
    if resolved is not None:
        try:
            return resolved.resolve().relative_to(Path(workspace).resolve()).as_posix()
        except (OSError, ValueError):
            return resolved.as_posix()
    return _extract_prd_path_from_prompt(user_input) or "the PRD"


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
        # The prompt named a document that does NOT exist - almost always the
        # file the user is asking us to CREATE ("create shamsu_smoke_note.md").
        # Falling through to "the single workspace PRD" below made every such
        # prompt resolve to an unrelated PRD, so a one-line file request routed
        # to prd.build and built somebody else's product instead. If the user
        # named a doc, that doc is the subject; not finding it is an answer.
        #
        # Exception: an unquoted @-mention whose filename contains spaces is
        # truncated by the bare-document pattern (`@canvas lite.pdf` yields
        # `lite.pdf`, which of course does not exist). An `@` means the user is
        # pointing AT an existing workspace file, never asking to create one,
        # so let the mention resolver - which reassembles the spaced name - have
        # its turn instead of failing on the truncated token.
        if "@" not in user_input:
            return None

    for mention in MentionResolver(workspace).resolve_all(user_input):
        # Identification, not readability: a mention that pinned down a real
        # file still names the PRD even when reading its CONTENT failed (an
        # unreadable/corrupt PDF must route here and report the parse error,
        # not fall back to generic file.write on a truncated name).
        if mention.path is None or not (workspace / mention.path).is_file():
            continue
        if (
            is_prd_filename(mention.path.name)
            # A document the user explicitly @-mentions in a build request IS
            # the requirements document, whatever it is named - PDFs and Word
            # files are inputs SHAMSU reads, never code it writes. The name
            # heuristic alone rejected `canvas lite.pdf` and derailed the
            # 2026-08-01 dogfood into generic file.write.
            or mention.path.suffix.lower() in DOCUMENT_EXTENSIONS
        ):
            return workspace / mention.path

    if explicit:
        # A named-but-unresolvable document stays the subject of the request:
        # never silently substitute the workspace's single PRD for it.
        return None

    candidates = _find_workspace_prd_files(workspace)
    if len(candidates) == 1:
        return workspace / candidates[0]
    return None


def _extract_prd_milestones(parsed) -> list[str]:
    lines = parsed.raw_text.splitlines() if parsed.raw_text else []
    milestones = [
        line.strip() for line in lines if re.match(r"^\s*(milestone|phase|step)\s*\d", line, re.I)
    ]
    return milestones


def _milestone_executor_enabled() -> bool:
    raw = os.environ.get("SHAMSU_MILESTONE_EXECUTOR", "").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _compiled_prd_milestones(parsed, *, require_complex: bool = False) -> list[str]:
    try:
        contract_obj = extract_contract(parsed)
        ledger = compile_requirement_ledger(contract_obj)
    except Exception:
        return []
    if require_complex and not is_complex_prd_contract(contract_obj, ledger):
        return []
    milestones: list[str] = []
    for milestone in ledger.milestones:
        requirement_preview = ", ".join(milestone.requirement_ids[:8])
        if len(milestone.requirement_ids) > 8:
            requirement_preview += f", +{len(milestone.requirement_ids) - 8} more"
        suffix = f" [{requirement_preview}]" if requirement_preview else ""
        milestones.append(f"{milestone.id}: {milestone.title}{suffix}")
    return milestones


def _prd_milestones_for_execution(parsed) -> tuple[list[str], str]:
    explicit = _extract_prd_milestones(parsed)
    if explicit:
        return explicit, "explicit_prd"
    if not _milestone_executor_enabled():
        return [], "disabled"
    configured = os.environ.get("SHAMSU_MILESTONE_EXECUTOR", "").strip()
    compiled = _compiled_prd_milestones(parsed, require_complex=not configured)
    return compiled, "compiled_requirement_ledger" if compiled else "simple_project"


PRD_DEVELOPMENT_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "plan_summary": {"type": "string"},
        "stack": {"type": "array", "items": {"type": "string"}},
        "milestones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": {"type": "string"},
                    "goal": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "verification": {"type": "string"},
                },
                "required": ["id", "title", "goal"],
            },
        },
        "risks": {"type": "array", "items": {"type": "string"}},
        "first_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["plan_summary", "milestones"],
}

PRD_DEVELOPMENT_PLAN_SYSTEM = """You are SHAMSU's PRD development planner.
Use reasoning to turn the PRD contract into a practical build plan.
Return ONLY JSON matching the schema.
Respect the compiled requirement IDs and milestone boundaries.
Do not claim files already exist unless the prompt says they do.
Keep the plan concrete, ordered, and buildable by a local coding agent."""


_REPLAN_RE = re.compile(
    r"\b(?:re-?plan|replan|new plan|fresh plan|plan (?:it )?again|start (?:the )?plan over)\b",
    re.IGNORECASE,
)


def _development_plan_cache_path(
    workspace: Path,
    project: Any,
    project_root: str,
) -> Path | None:
    """Where this PRD's approved development plan lives, keyed by contract hash.

    Beside the milestone ledger rather than in the pending action: the plan is
    project state, not conversation state. Saying "okay lets start" used to
    re-run the planner from scratch because only the plan's SOURCE string was
    ever stored.
    """
    contract_obj = getattr(project, "prd_contract", None)
    if contract_obj is None:
        return None
    try:
        ledger = compile_requirement_ledger(contract_obj)
        root = prd_execution_root(workspace, ledger.contract_hash, execution_key=project_root)
    except Exception:
        return None
    return root / "development_plan.json"


def _load_cached_development_plan(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not data.get("milestones"):
        return None
    if data.get("source") != "model":
        return None
    return data


def _save_development_plan(path: Path | None, plan: dict[str, Any]) -> None:
    if path is None or plan.get("source") != "model":
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan, indent=2, ensure_ascii=True), encoding="utf-8")
    except OSError as exc:
        swallowed.record("repl.development_plan_save", exc)


def _prd_plan_timeout_seconds(planner_thinks: bool) -> float:
    """Wall clock for one planning call.

    The old 30s default predates reasoning planners and was survivable only
    because a timeout silently fell back to compiled milestones. Now that the
    fallback is (correctly) gone, that same 30s turns a slow-but-working planner
    into a hard failure: a reasoning 9B can spend 15-20s reaching first token
    and then needs to think before emitting any JSON at all.
    """
    raw = os.environ.get("SHAMSU_PRD_PLAN_TIMEOUT_SECONDS", "").strip()
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            pass
    return 420.0 if planner_thinks else 180.0


def _prd_plan_num_predict(planner_thinks: bool) -> int:
    """Output budget for one planning call.

    Thinking tokens are generated tokens: a reasoning model spends its budget
    on the chain of thought first, so a cap sized for the JSON alone leaves
    nothing to emit the JSON with.
    """
    return _env_int_at_least("SHAMSU_PRD_PLAN_NUM_PREDICT", 3072 if planner_thinks else 1400, 512)


async def _prepare_prd_development_plan(
    parsed,
    relative_path: Path,
    project: Any,
    milestones: list[str],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    *,
    cache_path: Path | None = None,
    force_replan: bool = False,
) -> dict[str, Any]:
    if not force_replan:
        cached = _load_cached_development_plan(cache_path)
        if cached is not None:
            console.print(
                "[dim]Reusing the saved development plan for this PRD. "
                "Say `replan` to build a new one.[/dim]"
            )
            return cached
    contract = getattr(project, "prd_contract", None)
    contract_brief = contract.render_brief() if contract is not None else ""
    payload = {
        "user_request": str(getattr(project, "user_request", "") or ""),
        "prd_file": relative_path.as_posix(),
        "title": parsed.title,
        "sections": list(parsed.sections.keys()),
        "contract": contract_brief,
        "compiled_milestones": milestones,
        "workspace_files": _workspace_file_inventory_for_preflight(workspace, limit=40),
        "rules": [
            "Use the compiled milestones as the executable backbone.",
            "Add practical implementation detail, file ownership, and verification per milestone.",
            "Prefer Docker Compose, backend, frontend, and database wiring when the contract requires them.",
            "Return plan text only as JSON fields; do not write files.",
        ],
    }
    ledger = get_current_run()
    if ledger:
        ledger.log_event("prd_development_plan_started", path=relative_path.as_posix())
    plan: dict[str, Any] | None = None
    last_error = ""
    for attempt in range(2):
        planner_payload = dict(payload)
        if attempt:
            planner_payload["previous_validation_error"] = last_error
            planner_payload["retry_rules"] = [
                "Return a non-empty milestones array.",
                "Each milestone must include id, title, and goal.",
                "Do not switch the tech stack or product type.",
            ]
        planner_model = model_for_role("planner")
        planner_thinks = role_should_think("planner", planner_model)
        try:
            raw = await asyncio.wait_for(
                _make_llm_manager(session_logger, console, workspace).generate_structured(
                    "planner",
                    PRD_DEVELOPMENT_PLAN_SYSTEM,
                    json.dumps(planner_payload, indent=2, ensure_ascii=True),
                    PRD_DEVELOPMENT_PLAN_SCHEMA,
                    temperature=0.0,
                    num_predict=_prd_plan_num_predict(planner_thinks),
                ),
                timeout=_prd_plan_timeout_seconds(planner_thinks),
            )
            candidate = _loads_freeform_json(raw or "")
            plan = _validate_prd_development_plan(candidate, payload.get("user_request") or "")
            break
        except (TimeoutError, asyncio.TimeoutError):
            # asyncio.TimeoutError stringifies to "", so the panel used to read
            # "Last error: TimeoutError:" - which names no cause and no remedy.
            budget = _prd_plan_timeout_seconds(planner_thinks)
            last_error = (
                f"the planner model {planner_model} ran out of time after {budget:.0f}s"
                + (" (it is a reasoning model, so it thinks before answering)" if planner_thinks else "")
                + ". Raise SHAMSU_PRD_PLAN_TIMEOUT_SECONDS, or use a non-reasoning planner model."
            )
            if ledger:
                ledger.log_event(
                    "prd_development_plan_attempt_failed",
                    attempt=attempt + 1,
                    error=last_error,
                    timeout_seconds=budget,
                    planner_model=planner_model,
                    planner_thinks=planner_thinks,
                )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if ledger:
                ledger.log_event(
                    "prd_development_plan_attempt_failed",
                    attempt=attempt + 1,
                    error=last_error,
                )
    if plan is None:
        plan = {
            "source": "planner_failed",
            "error": last_error,
            "plan_summary": "Planner output could not be validated. No fallback plan was executed.",
            "stack": [],
            "milestones": [],
            "risks": [],
            "first_actions": [],
        }
    if ledger:
        ledger.log_event(
            "prd_development_plan_finished",
            source=plan.get("source"),
            milestones=len(plan.get("milestones") or []),
            error=plan.get("error", ""),
        )
    _save_development_plan(cache_path, plan)
    if plan.get("source") == "model":
        console.print("[dim]LLM development plan accepted from planner role.[/dim]")
    elif plan.get("source") == "planner_failed":
        console.print(
            "[yellow]LLM development plan failed validation; no fallback architecture will be used.[/yellow]"
        )
    else:
        console.print("[dim]LLM development plan unavailable; using compiled fallback.[/dim]")
    return plan


# File extensions that betray a stack the request never asked for. Keyed by the
# stack the plan would be drifting INTO, so the rejection can name it.
_STACK_SIGNATURE_FILES = {
    "django/web backend": ("settings.py", "urls.py", "wsgi.py", "asgi.py", "forms.py", "admin.py"),
    "node/web frontend": ("package.json", "vite.config.js", "vite.config.ts", "tsconfig.json"),
}
# Words in the request that legitimately license each of those stacks.
_STACK_LICENCE_WORDS = {
    "django/web backend": (
        "django", "web app", "webapp", "website", "backend", "api", "rest",
        "server", "dashboard", "admin", "crud", "http", "browser",
    ),
    "node/web frontend": (
        "react", "vue", "svelte", "vite", "node", "npm", "typescript",
        "javascript", "frontend", "web app", "webapp", "website", "browser",
    ),
}


def _architecture_conformance_errors(plan: dict[str, Any], request: str) -> list[str]:
    """Reject a plan whose architecture does not match what was asked for.

    A pygame request came back as a plan targeting `backend/core/forms.py` and
    HTML templates, and nothing checked: the planner was validated for SHAPE
    (ids, titles, goals present) and never for whether it was building the thing
    the user described. Structure was well-formed; the product was wrong.
    """
    text = request.lower()
    proposed = " ".join(
        [
            *(str(item) for item in plan.get("stack") or []),
            *(
                str(path)
                for milestone in plan.get("milestones") or []
                if isinstance(milestone, dict)
                for path in milestone.get("files") or []
            ),
        ]
    ).lower()
    if not proposed:
        return []
    errors: list[str] = []
    for stack, signatures in _STACK_SIGNATURE_FILES.items():
        hits = sorted({name for name in signatures if name in proposed})
        if not hits:
            continue
        if any(word in text for word in _STACK_LICENCE_WORDS[stack]):
            continue
        errors.append(
            f"plan proposes {stack} files ({', '.join(hits)}) but the request "
            f"never asks for that: {request.strip()[:120]}"
        )
    return errors


def _validate_prd_development_plan(candidate: Any, request: str = "") -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ValueError("planner did not return a JSON object")
    raw_milestones = candidate.get("milestones")
    if not isinstance(raw_milestones, list) or not raw_milestones:
        raise ValueError("planner returned no milestones")
    milestones: list[dict[str, Any]] = []
    for index, item in enumerate(raw_milestones[:12], start=1):
        if not isinstance(item, dict):
            continue
        title = _safe_plan_text(item.get("title"), 140)
        goal = _safe_plan_text(item.get("goal"), 260)
        if not title or not goal:
            continue
        milestones.append(
            {
                "id": _safe_plan_text(item.get("id"), 40) or f"M-{index:03d}",
                "title": title,
                "goal": goal,
                "files": _safe_plan_list(item.get("files"), 6, 120),
                "verification": _safe_plan_text(item.get("verification"), 180),
            }
        )
    if not milestones:
        raise ValueError("planner milestones were incomplete")
    plan = {
        "source": "model",
        "plan_summary": _safe_plan_text(candidate.get("plan_summary"), 500),
        "stack": _safe_plan_list(candidate.get("stack"), 10, 80),
        "milestones": milestones,
        "risks": _safe_plan_list(candidate.get("risks"), 8, 160),
        "first_actions": _safe_plan_list(candidate.get("first_actions"), 8, 160),
    }
    drift = _architecture_conformance_errors(plan, request)
    if drift:
        # Raised, not warned: this joins the same retry-then-stop path a
        # malformed plan takes, so a drifting architecture is never silently
        # accepted as the thing to build.
        raise ValueError("; ".join(drift))
    return plan


def _safe_plan_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _safe_plan_list(value: Any, limit: int, max_chars: int) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = [_safe_plan_text(item, max_chars) for item in value[:limit] if item]
    return [item for item in dict.fromkeys(cleaned) if item]


def _compiled_milestones_as_plan_items(milestones: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for index, milestone in enumerate(milestones[:12], start=1):
        milestone_id = _milestone_id_from_line(milestone)
        title = milestone.split(":", 1)[1].strip() if ":" in milestone else milestone
        result.append(
            {
                "id": milestone_id,
                "title": title,
                "goal": "Implement and verify this compiled PRD milestone.",
            }
        )
    return result


def _prd_development_plan_failed(development_plan: dict[str, Any]) -> bool:
    return development_plan.get("source") == "planner_failed"


def _print_prd_development_plan_failure(
    development_plan: dict[str, Any],
    console: Console,
) -> None:
    error = str(development_plan.get("error") or "unknown planner validation error")
    message = (
        "The planner model did not return a valid executable milestone plan after a retry.\n\n"
        "I stopped instead of using a compiled fallback, because that fallback can drift away "
        "from the PRD's product type or tech stack.\n\n"
        f"Last error: {error}"
    )
    console.print(Panel(message, title="PRD Planning Failed", border_style="red"))


def _print_prd_development_plan(
    parsed,
    relative_path: Path,
    milestones: list[str],
    development_plan: dict[str, Any],
    console: Console,
    *,
    build_after: bool,
    execution_scope: str = "all",
    skill_names: list[str] | None = None,
) -> None:
    section_names = list(parsed.sections.keys())
    lines = [
        f"File: {relative_path.as_posix()}",
        f"Title: {parsed.title}",
        f"Plan source: {development_plan.get('source') or 'deterministic'}",
        "",
        "Sections: " + (", ".join(section_names) if section_names else "none"),
    ]
    summary = str(development_plan.get("plan_summary") or "").strip()
    if summary:
        lines.extend(["", "Planner summary:", summary])
    stack = list(development_plan.get("stack") or [])
    if stack:
        lines.extend(["", "Planned stack: " + ", ".join(stack[:10])])
    if skill_names:
        lines.extend(
            [
                "",
                "Suggested SHAMSU skills: " + ", ".join(skill_names[:10]),
                "Use `/skills show <name>` to inspect one, or `/skills explain <prompt>` to preview selection.",
            ]
        )
    plan_items = list(development_plan.get("milestones") or [])
    if plan_items:
        lines.append("")
        lines.append("LLM development milestones:")
        for item in plan_items[:12]:
            files = list(item.get("files") or []) if isinstance(item, dict) else []
            verification = str(item.get("verification") or "") if isinstance(item, dict) else ""
            lines.append(
                f"  - {item.get('id', '')}: {item.get('title', '')} - {item.get('goal', '')}"
            )
            if files:
                lines.append(f"    files: {', '.join(files[:6])}")
            if verification:
                lines.append(f"    verify: {verification}")
    if milestones:
        lines.append("")
        lines.append("Compiled execution ledger:")
        lines.extend(f"  - {item}" for item in milestones[:12])
        if len(milestones) > 12:
            lines.append(f"  ... {len(milestones) - 12} more")
    risks = list(development_plan.get("risks") or [])
    if risks:
        lines.extend(["", "Risks:"])
        lines.extend(f"  - {item}" for item in risks[:6])
    lines.append("")
    if build_after:
        if execution_scope == "slice":
            lines.append("I'll execute one PRD slice now, writing only the files needed for")
            lines.append("that milestone. Reply `continue` when you want the next slice.")
        else:
            lines.append("I'll build this now, autonomously (long-running mode), writing files in")
            lines.append("your workspace until it's implemented. Type `exit` to stop.")
    else:
        next_id = _milestone_id_from_line(milestones[0]) if milestones else "the first slice"
        lines.append("Plan only. No project files were created or modified.")
        lines.append(f"Reply `start {next_id}` or `continue` to approve and execute one slice.")
        lines.append("Reply `build all autonomously` only when you want the full plan to run.")
    console.print(Panel("\n".join(lines), title="PRD Development Plan"))


def _print_prd_build_plan(
    parsed,
    relative_path: Path,
    console: Console,
    development_plan: dict[str, Any] | None = None,
    *,
    execution_scope: str = "all",
    skill_names: list[str] | None = None,
) -> None:
    if development_plan is not None:
        milestones, _ = _prd_milestones_for_execution(parsed)
        _print_prd_development_plan(
            parsed,
            relative_path,
            milestones,
            development_plan,
            console,
            build_after=True,
            execution_scope=execution_scope,
            skill_names=skill_names,
        )
        return
    section_names = list(parsed.sections.keys())
    milestones, milestone_source = _prd_milestones_for_execution(parsed)
    lines = [
        f"File: {relative_path.as_posix()}",
        f"Title: {parsed.title}",
        "",
        "Sections: " + (", ".join(section_names) if section_names else "none"),
    ]
    if milestones:
        lines.append("")
        lines.append(
            "Milestones detected:"
            if milestone_source == "explicit_prd"
            else "Compiled requirement milestones:"
        )
        lines.extend(f"  - {item}" for item in milestones[:12])
        if len(milestones) > 12:
            lines.append(f"  ... {len(milestones) - 12} more")
    lines.append("")
    lines.append("I'll build this now, autonomously (long-running mode), writing files in")
    lines.append("your workspace until it's implemented. Type `exit` to stop.")
    console.print(Panel("\n".join(lines), title="PRD Build Plan"))


_PRD_AUTONOMOUS_PHRASES = (
    "build all",
    "build everything",
    "execute all",
    "run all",
    "implement all",
    "full autonomous",
    "autonomous build",
    "autonomously",
    "without asking",
)

_PRD_SLICE_COMMAND_RE = re.compile(
    r"\b(?:start|begin|execute|run|implement(?:ed|ing)?|proceed|porceed|continue|do|"
    r"ensure|configure|complete|finish|scaffold|bootstrap|initiate|initiali[sz]e)\b",
    re.IGNORECASE,
)

_PRD_SLICE_SCAFFOLD_HINTS = (
    "boilerplate",
    "boilerplates",
    "scaffold",
    "folder structure",
    "project structure",
    "backend",
    "frontend",
    "postgres",
    "docker",
)


def _prd_autonomous_execution_requested(user_input: str) -> bool:
    lowered = user_input.lower().strip()
    if _explicitly_read_only(user_input):
        return False
    return any(phrase in lowered for phrase in _PRD_AUTONOMOUS_PHRASES)


def _looks_like_prd_slice_execution_reply(user_input: str) -> bool:
    text = user_input.lower().strip()
    if not text:
        return False
    if _prd_autonomous_execution_requested(text):
        return True
    if _looks_like_affirmative_continue(text):
        return True
    if re.fullmatch(r"yes\s+(?:please\s+)?porceed", text):
        return True
    if is_affirmative(text):
        return True
    if text in {
        "continue",
        "proceed",
        "start",
        "start it",
        "do it",
        "run it",
        "execute it",
        "implement it",
    }:
        return True
    if not _PRD_SLICE_COMMAND_RE.search(text):
        return False
    if any(hint in text for hint in _PRD_SLICE_SCAFFOLD_HINTS):
        return True
    return any(
        token in text
        for token in (
            "plan",
            "milestone",
            "slice",
            "phase",
            "step",
            "first",
            "next",
            "m-",
            "m ",
        )
    )


_PRD_MILESTONE_EXECUTION_RE = re.compile(
    r"\b(?:m-\d{3}|milestone\s+\d{1,3})\b",
    re.IGNORECASE,
)
_PRD_MILESTONE_EXECUTION_ACTION_RE = re.compile(
    r"\b(?:start|begin|execute|run|implement(?:ed|ing)?|make\s+sure|ensure|"
    r"configure|complete|finish|build|do|proceed)\b",
    re.IGNORECASE,
)


def _looks_like_prd_milestone_execution_request(user_input: str) -> bool:
    """A request to implement a named PRD milestone, not to make another plan."""
    text = user_input.lower()
    if _looks_like_plan_intent(user_input):
        return False
    if "prd" not in text and "plan" not in text:
        return False
    return bool(
        _PRD_MILESTONE_EXECUTION_RE.search(user_input)
        and _PRD_MILESTONE_EXECUTION_ACTION_RE.search(user_input)
    )


def _store_pending_prd_plan_execution(
    session_logger: SessionLogger | None,
    *,
    user_input: str,
    relative_path: Path,
    project_root: str,
    milestones: list[str],
    development_plan: dict[str, Any],
) -> None:
    if session_logger is None:
        return
    next_id = _milestone_id_from_line(milestones[0]) if milestones else ""
    try:
        session_logger.set_pending_action(
            {
                "type": "prd_plan",
                "awaiting": "prd_plan_selection",
                "prd_path": relative_path.as_posix(),
                "project_root": project_root,
                "next_milestone_id": next_id,
                "milestone_count": len(milestones),
                "plan_source": str(development_plan.get("source") or ""),
                "created_from_prompt": user_input,
            }
        )
    except Exception as exc:
        swallowed.record("repl.prd_pending_plan_execution", exc)


def _prd_skill_names_for_project(project: Any) -> list[str]:
    contract_obj = getattr(project, "prd_contract", None)
    if contract_obj is None:
        return []
    try:
        ledger = compile_requirement_ledger(contract_obj)
    except Exception:
        return []
    names = [
        str(skill)
        for milestone in ledger.milestones
        for skill in milestone.active_skills
        if str(skill)
    ]
    return list(dict.fromkeys(names))


def _prd_milestone_contracts(
    preflight: dict[str, Any],
    milestone: str,
    milestone_id: str,
    project_root: str,
    workspace: Path,
) -> list[TaskContract]:
    """Turn one milestone into the atomic tasks that will actually be executed.

    The decomposition already existed implicitly - expected-file passes and
    behavioural file groups are one turn per file - but it lived only in the
    call stack. Persisting it as TaskContracts makes the unit of work durable,
    reviewable, and resumable, and gives each file its own locked write scope,
    acceptance criteria, and verification requirement.
    """
    verifier = str(preflight.get("verifier") or "").strip()
    requirement_refs = [str(item) for item in preflight.get("requirement_ids") or []]
    targets: list[tuple[str, list[str]]] = []
    for target in _preflight_expected_files(preflight):
        targets.append((target, [f"{target} exists and satisfies its milestone requirements."]))
    for target, requirements in _prd_behavioural_file_groups(preflight, project_root, workspace):
        criteria = [
            f"{item.get('id', '')}: {item.get('text', '')}".strip(": ")
            for item in requirements
            if isinstance(item, dict)
        ]
        targets.append((target, criteria or [f"{target} implements its behavioural requirements."]))

    contracts: list[TaskContract] = []
    previous_id = ""
    seen: set[str] = set()
    for index, (target, criteria) in enumerate(targets, 1):
        if not target or target in seen:
            continue
        seen.add(target)
        task_id = f"{milestone_id}-task-{index:03d}"
        contracts.append(
            TaskContract(
                task_id=task_id,
                run_id=milestone_id,
                objective=f"Implement {target} for milestone: {milestone}",
                requirement_refs=requirement_refs,
                dependencies=[previous_id] if previous_id else [],
                allowed_write_paths=[target],
                expected_write_paths=[target],
                planner_proposed_files=[target],
                acceptance_criteria=criteria,
                verification_requirements=[verifier] if verifier else [],
            )
        )
        previous_id = task_id
    return contracts


def _persist_prd_milestone_contracts(
    preflight: dict[str, Any],
    milestone: str,
    milestone_id: str,
    project_root: str,
    workspace: Path,
    session_logger: SessionLogger | None,
) -> list[TaskContract]:
    contracts = _prd_milestone_contracts(
        preflight, milestone, milestone_id, project_root, workspace
    )
    if not contracts:
        return []
    validated: list[TaskContract] = []
    for task_contract in contracts:
        result = validate_contract(task_contract, workspace)
        if result.ok:
            validated.append(task_contract)
        else:
            _log_task_contract_event(
                session_logger,
                "task_contract.rejected",
                {"task_id": task_contract.task_id, "errors": list(result.errors)},
                "Milestone task contract failed validation",
            )
    if not validated:
        return []
    try:
        write_plan_contracts(workspace, milestone_id, validated)
    except OSError as exc:
        swallowed.record("repl.prd_milestone_contracts", exc)
    _log_task_contract_event(
        session_logger,
        "task_contract.milestone_decomposed",
        {
            "milestone_id": milestone_id,
            "tasks": [contract.task_id for contract in validated],
            "files": [path for contract in validated for path in contract.expected_write_paths],
        },
        f"Decomposed {milestone_id} into {len(validated)} atomic task(s)",
    )
    return validated


def _pause_prd_milestone_between_files(
    session_logger: SessionLogger | None,
    *,
    user_input: str,
    relative_path: Path,
    project_root: str,
    milestone_id: str,
    changed: list[str],
    remaining: list[str],
    console: Console,
) -> None:
    """Hand control back after one atomic file pass, naming what comes next.

    `Autonomy: off` used to mean "one milestone per approval", which for a
    scaffolding milestone was a dozen file writes the user never agreed to
    individually. Off now means what it says: finish this file, report it, and
    ask before starting the next one.
    """
    if session_logger is not None:
        try:
            session_logger.set_pending_action(
                {
                    "type": "prd_plan",
                    "awaiting": "prd_plan_selection",
                    "prd_path": relative_path.as_posix(),
                    "project_root": project_root,
                    "next_milestone_id": milestone_id,
                    "created_from_prompt": user_input,
                }
            )
        except Exception as exc:
            swallowed.record("repl.prd_pause_between_files", exc)
    console.print(
        Panel(
            f"Built {', '.join(changed)}.\n\n"
            f"Next in {milestone_id}: {remaining[0]}\n"
            f"{len(remaining)} file(s) left in this milestone.\n\n"
            "Reply `continue` to build the next one, or `build all autonomously` "
            "to finish the milestone without stopping.",
            title="Paused After One File",
            border_style="cyan",
        )
    )
    _log_event(
        session_logger,
        "prd.milestone.file_pass_paused",
        {
            "milestone_id": milestone_id,
            "changed": changed,
            "remaining": remaining,
        },
        "Paused PRD milestone after one atomic file pass",
        workflow_id="prd-build",
    )


def _pause_prd_build_after_slice(
    session_logger: SessionLogger | None,
    *,
    user_input: str,
    relative_path: Path,
    project_root: str,
    milestones: list[str],
    current_index: int,
    development_plan: dict[str, Any],
    console: Console,
    completed: bool = True,
) -> bool:
    remaining = milestones[current_index + 1 :]
    if not remaining:
        return False
    next_id = _milestone_id_from_line(remaining[0])
    _store_pending_prd_plan_execution(
        session_logger,
        user_input=user_input,
        relative_path=relative_path,
        project_root=project_root,
        milestones=remaining,
        development_plan=development_plan,
    )
    prefix = "Finished one PRD slice." if completed else "Paused after one PRD slice attempt."
    message = (
        f"{prefix} Next slice is {next_id}.\n\n"
        f"Reply `continue` or `start {next_id}` to approve and run the next slice. "
        "Reply `build all autonomously` to run the rest."
    )
    console.print(Panel(message, title="PRD Build Paused", border_style="cyan"))
    _log_event(
        session_logger,
        "prd.build.slice_paused",
        {"next_milestone_id": next_id, "remaining": len(remaining)},
        "Paused PRD build after one requested slice",
        workflow_id="prd-build",
    )
    return True


async def _execute_pending_prd_plan(
    pending_action: dict[str, Any],
    reply: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    origin = str(pending_action.get("created_from_prompt") or "").strip()
    prd_path = str(pending_action.get("prd_path") or "").strip()
    if not origin:
        origin = f"build the product from {prd_path}" if prd_path else "build the product from the PRD"
    if not _legacy_routing_enabled():
        await _simple_pending_run(origin, workspace, console, session_logger)
        return
    run_all = _prd_autonomous_execution_requested(reply)
    if run_all:
        console.print("[dim]Executing the full PRD plan because you asked for all milestones.[/dim]")
    else:
        next_id = str(pending_action.get("next_milestone_id") or "the next milestone").strip()
        console.print(f"[dim]Executing one PRD slice: {next_id}.[/dim]")
    await _handle_prd_build_request(
        origin,
        workspace,
        console,
        session_logger=session_logger,
        execute_plan=True,
        max_milestones=None if run_all else 1,
    )


async def _handle_prd_development_plan_request(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    prd_path = _resolve_build_prd(user_input, workspace)
    if prd_path is None:
        console.print("[yellow]I could not find the PRD file to plan from.[/yellow]")
        return
    try:
        parsed = parse_prd_file(prd_path)
    except PRDParseError as exc:
        prd_ref = _resolved_prd_reference(user_input, workspace)
        plan_request = (
            f"{user_input}\n\n"
            f"I could not parse {prd_ref} directly ({exc}). "
            "Use the user's request above as the planning brief. Produce a concise, "
            "numbered, step-by-step implementation plan: files to create, build order, "
            "and verification for each step. Do NOT write any code or files."
        )
        await _run_agent_chat(
            plan_request,
            workspace,
            console,
            session_logger=session_logger,
            read_only=True,
        )
        return
    try:
        relative_path = prd_path.relative_to(workspace)
    except ValueError:
        relative_path = prd_path

    inferred_entities = await _infer_prd_entities(parsed, console, session_logger)
    project = build_project_spec(
        parsed, request_text=user_input, extra_entities=inferred_entities
    )
    setattr(project, "user_request", user_input)
    milestones, _milestone_source = _prd_milestones_for_execution(parsed)
    plan = await _prepare_prd_development_plan(
        parsed,
        relative_path,
        project,
        milestones,
        workspace,
        console,
        session_logger,
        cache_path=_development_plan_cache_path(
            workspace, project, _prd_target_directory(user_input, project)
        ),
        force_replan=bool(_REPLAN_RE.search(user_input)),
    )
    if _prd_development_plan_failed(plan):
        _print_prd_development_plan_failure(plan, console)
        _log_event(
            session_logger,
            "prd.plan.failed",
            {"path": str(prd_path), "error": plan.get("error", "")},
            "PRD development planner failed validation",
            workflow_id="plan-prd",
        )
        return
    _print_prd_development_plan(
        parsed,
        relative_path,
        milestones,
        plan,
        console,
        build_after=False,
        skill_names=_prd_skill_names_for_project(project),
    )
    project_root = _prd_target_directory(user_input, project)
    _store_pending_prd_plan_execution(
        session_logger,
        user_input=user_input,
        relative_path=relative_path,
        project_root=project_root,
        milestones=milestones,
        development_plan=plan,
    )
    _log_assistant_message(
        session_logger,
        "Prepared a PRD development plan and paused for milestone selection.",
        workflow_id="plan-prd",
    )


PRD_BUILD_FRAMING = (
    "Build complete, runnable product files. Do not create TODO-only stubs or placeholder implementations. "
    "Before rewriting any file that already exists, read it first with read_file and EXTEND it - never "
    "regenerate a file from scratch in a way that drops features implemented in earlier milestones. "
    "Keep the app wired together: if the project has a script.js, index.html must load it with "
    '<script src="script.js"></script> and must NOT keep its own inline game logic or a leftover '
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
            f"{n:>5}{'>' if n == line_no else ' '} {lines[n - 1]}" for n in range(start, end + 1)
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
        console.print(
            f"[dim]Checking the game compiles (tsc, attempt {attempt}/{max_attempts})...[/dim]"
        )
        ok, output = _run_frontend_typecheck(workspace)
        if ok:
            console.print(
                "[green]OK: The game compiles cleanly - frontend and game logic are wired together.[/green]"
            )
            _log_event(
                session_logger,
                "project.typecheck.ok",
                {"attempt": attempt},
                "Frontend typecheck passed",
                workflow_id="prd-build",
            )
            return True
        console.print(
            Panel(
                output[-3000:] or "tsc reported errors.",
                title=f"Compile errors (attempt {attempt})",
                border_style="yellow",
            )
        )
        _log_event(
            session_logger,
            "project.typecheck.failed",
            {"attempt": attempt},
            "Frontend typecheck failed",
            workflow_id="prd-build",
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


# File extensions that mean "this workspace already has an app" - so a greenfield
# PRD build should EXTEND, not scaffold from scratch.
_APP_SOURCE_EXTENSIONS = {
    ".html",
    ".css",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".py",
    ".vue",
    ".svelte",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".java",
}
_SCAFFOLD_IGNORED_FILES = {
    ".gitignore",
    "readme.md",
    "readme",
    "license",
    "license.md",
    "license.txt",
}


def _looks_like_frontend_build_request(user_input: str) -> bool:
    """True when the user explicitly asks to build with HTML/CSS/JS."""
    low = user_input.lower()
    return "html" in low and ("css" in low or "javascript" in low or " js" in low or "/js" in low)


def _workspace_has_app_files(workspace: Path) -> bool:
    """True if the workspace already contains real source files (so we should
    extend, not scaffold). PRDs, .gitignore, README/LICENSE and tooling dirs do
    not count."""
    for path in walk_workspace_files(workspace):
        if is_prd_filename(path.name) or path.name.lower() in _SCAFFOLD_IGNORED_FILES:
            continue
        if path.suffix.lower() in _APP_SOURCE_EXTENSIONS:
            return True
    return False


def _starter_index_html(title: str) -> str:
    safe = title.strip() or "App"
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="UTF-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"  <title>{safe}</title>\n"
        '  <link rel="stylesheet" href="style.css" />\n'
        "</head>\n"
        "<body>\n"
        f'  <main id="app">\n    <h1>{safe}</h1>\n  </main>\n'
        '  <script src="script.js"></script>\n'
        "</body>\n"
        "</html>\n"
    )


def _starter_style_css() -> str:
    return (
        ":root { color-scheme: light dark; }\n"
        "* { box-sizing: border-box; }\n"
        "body { margin: 0; font-family: system-ui, sans-serif; }\n"
        "#app { max-width: 720px; margin: 2rem auto; padding: 0 1rem; }\n"
    )


def _starter_script_js(title: str) -> str:
    safe = title.strip() or "App"
    return (
        f"// {safe} - entry point\n"
        '"use strict";\n\n'
        'document.addEventListener("DOMContentLoaded", () => {\n'
        "  // App logic goes here.\n"
        "});\n"
    )


def _scaffold_frontend_from_prd(
    parsed,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> list[str]:
    """Deterministically create the index.html/style.css/script.js a greenfield
    "build with HTML/CSS/JS" PRD needs, so the agent EXTENDS real files instead
    of trying to read a missing index.html first. Existing files are left alone."""
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(
            workspace, session_logger, console, lambda _request: True
        ),
        action_ledger=get_current_run(),
    )
    title = getattr(parsed, "title", "") or "App"
    files = {
        "index.html": _starter_index_html(title),
        "style.css": _starter_style_css(),
        "script.js": _starter_script_js(title),
    }
    created: list[str] = []
    for name, content in files.items():
        if (workspace / name).exists():
            continue
        result = registry.execute("write_file", {"filepath": name, "content": content})
        if result.ok:
            created.append(name)
    if created:
        console.print(
            Panel("Created starter files: " + ", ".join(created), title="Frontend Scaffold")
        )
        _log_event(
            session_logger,
            "prd.build.scaffold",
            {"created": created},
            "Scaffolded greenfield frontend files",
            workflow_id="prd-build",
        )
    return created


def _build_frontend_fill_request(parsed, relative_path: Path) -> str:
    return (
        f"{PRD_BUILD_FRAMING}\n\n"
        "The workspace now contains starter index.html, style.css, and script.js. "
        "Implement the product described by the PRD by reading and EXTENDING those three "
        "files - do not assume any other files exist, and do not create a backend. "
        "index.html must link style.css and load script.js; keep all logic in script.js.\n\n"
        f"=== PRD: {relative_path.as_posix()} ===\n"
        f"{parsed.raw_text or _render_sections(parsed)}"
    )


def _prd_grounding_issue(parsed) -> str:
    confidence = float(getattr(parsed, "extraction_confidence", 1.0) or 0.0)
    if confidence >= 0.5:
        return ""
    warnings = list(getattr(parsed, "extraction_warnings", []) or [])
    details = f" Extraction warnings: {'; '.join(warnings)}" if warnings else ""
    return (
        f"I could not ground this build reliably (extraction confidence {confidence:.0%})."
        f"{details} Please provide a clearer document or a text version before I modify the project."
    )


# A document with at least this many sections is structured enough that
# extracting nothing from it means the headings were not understood, rather
# than that the document is a short prose brief (which builds from raw text).
_PRD_STRUCTURED_SECTION_COUNT = 8


async def _infer_prd_entities(parsed, console, session_logger) -> list:
    """Recover entities the PRD names but never defines.

    Runs only when deterministic extraction found none and the document does
    list entity names, so a well-formed PRD costs nothing. Failure is never
    fatal: it returns [] and the build proceeds exactly as before.
    """
    from shamsu.prd import entity_fields

    try:
        if extract_contract(parsed).entities:
            return []
        names = entity_fields.bare_entity_names(parsed)
    except Exception:
        return []
    if not names:
        return []

    limit = entity_fields.max_inferred_entities()
    console.print(
        f"[dim]The PRD names {len(names)} entities without defining their fields. "
        f"Asking the reasoning model to design the first {min(limit, len(names))}...[/dim]"
    )
    try:
        entities = await asyncio.wait_for(
            entity_fields.infer_entity_fields(parsed, names),
            timeout=float(os.environ.get("SHAMSU_PRD_ENTITY_TIMEOUT_SECONDS", "20")),
        )
    except Exception as exc:
        _log_event(
            session_logger,
            "prd.entity_inference_failed",
            {"error": str(exc)},
            "Entity field inference failed",
            workflow_id="prd-build",
        )
        return []
    if entities:
        console.print(
            f"[dim]Designed {len(entities)} models: "
            f"{', '.join(entity.name for entity in entities[:8])}"
            f"{'...' if len(entities) > 8 else ''}[/dim]"
        )
        _log_event(
            session_logger,
            "prd.entities_inferred",
            {
                "named": len(names),
                "designed": len(entities),
                "models": [entity.name for entity in entities],
            },
            f"Inferred fields for {len(entities)} entities",
            workflow_id="prd-build",
        )
    return entities


def _prd_extraction_is_thin(parsed) -> bool:
    """True when heading matching found nothing substantial to build from."""
    try:
        contract_obj = extract_contract(parsed)
    except Exception:
        return True
    return not contract_obj.entities and not contract_obj.features


def _prd_has_nothing_to_build(parsed) -> bool:
    """True for a clearly structured document that still yielded no work.

    A short prose PRD with no sections is not this case - the generator works
    from its raw text. This is the document that lays out dozens of sections
    and still produces no entity and no feature, which compiles to a plan of
    pure verification steps that writes no code.
    """
    if len(getattr(parsed, "sections", {}) or {}) < _PRD_STRUCTURED_SECTION_COUNT:
        return False
    return _prd_extraction_is_thin(parsed)


async def _resolve_prd_headings(parsed, console, session_logger):
    """Fold this PRD's own section names onto the ones extraction understands.

    Requirements are found by heading name, so a PRD that words its headings
    differently is read as empty and compiles to a plan with nothing in it.
    The deterministic pass inside ``extract_contract`` handles rewordings; when
    it still finds no entities and no features, the reasoning model is asked to
    place the remaining sections. Failure here is never fatal - it leaves the
    document exactly as parsed.
    """
    resolution = prd_headings.resolve_headings(parsed.sections)
    resolved = prd_headings.apply_heading_aliases(parsed, resolution.aliases)
    if not _prd_extraction_is_thin(resolved) or not resolution.unresolved:
        return resolved

    console.print(
        f"[dim]Section names in this document do not match the ones I read by "
        f"default ({len(resolution.unresolved)} unrecognised). Asking the "
        f"reasoning model to place them...[/dim]"
    )
    try:
        model_aliases = await asyncio.wait_for(
            prd_headings.resolve_headings_with_model(parsed, resolution.unresolved),
            timeout=float(os.environ.get("SHAMSU_PRD_HEADING_TIMEOUT_SECONDS", "20")),
        )
    except Exception as exc:  # never block a build on the optional pass
        _log_event(
            session_logger,
            "prd.heading_resolution_failed",
            {"error": str(exc)},
            "Model heading resolution failed",
            workflow_id="prd-build",
        )
        return resolved

    if not model_aliases:
        return resolved

    combined = dict(resolution.aliases)
    combined.update(model_aliases)
    resolved = prd_headings.apply_heading_aliases(parsed, combined)
    contract_obj = extract_contract(resolved)
    console.print(
        f"[dim]Placed {len(model_aliases)} section(s): "
        f"{len(contract_obj.features)} features, "
        f"{len(contract_obj.entities)} entities, "
        f"{len(contract_obj.roles)} roles.[/dim]"
    )
    _log_event(
        session_logger,
        "prd.headings_resolved",
        {
            "deterministic": len(resolution.aliases),
            "model": len(model_aliases),
            "unresolved": len(resolution.unresolved),
            "features": len(contract_obj.features),
            "entities": len(contract_obj.entities),
            "aliases": model_aliases,
        },
        f"Resolved {len(model_aliases)} PRD headings with the reasoning model",
        workflow_id="prd-build",
    )
    return resolved


async def _handle_prd_build_request(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    *,
    execute_plan: bool = False,
    max_milestones: int | None = None,
) -> None:
    prd_path = _resolve_build_prd(user_input, workspace)
    if prd_path is None:
        candidates = _find_workspace_prd_files(workspace)
        if len(candidates) > 1:
            console.print(
                "[yellow]I found multiple PRD files - which one should I build from?[/yellow]"
            )
            for path in candidates[:10]:
                console.print(f"- {path.as_posix()}")
            console.print('Name one, e.g. `build the product from "<file>"`.')
        else:
            console.print(
                "[yellow]I couldn't find a PRD to build from.[/yellow] "
                "I look for a `.md`, `.txt`, `.pdf`, or `.docx` whose name contains `prd` or "
                "`Product Requirements`.\n"
                "If your spec is already here under another name, point me straight at it, "
                'e.g. `build the app from "spec.md"` - I\'ll build from any file you name.'
            )
        return

    try:
        parsed = parse_prd_file(prd_path)
    except PRDParseError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        return

    parsed = await _resolve_prd_headings(parsed, console, session_logger)

    grounding_issue = _prd_grounding_issue(parsed)
    if grounding_issue:
        console.print(
            Panel(grounding_issue, title="Specification Extraction Failed", border_style="red")
        )
        _log_event(
            session_logger,
            "prd.grounding_failed",
            {
                "confidence": parsed.extraction_confidence,
                "warnings": list(parsed.extraction_warnings),
            },
            grounding_issue,
            workflow_id="prd-build",
        )
        return

    if _prd_has_nothing_to_build(parsed):
        # An acceptance-only contract still compiles to a milestone plan, so
        # this used to run to "completion" having written no source file at
        # all. Say so instead of spending a build proving it.
        section_names = ", ".join(list(parsed.sections)[:8]) or "none"
        message = (
            "I read this document but could not find anything to build.\n\n"
            "No data entities and no features were extracted, which means the "
            "plan would contain only verification steps and would produce no "
            "code.\n\n"
            f"Sections I found: {section_names}\n\n"
            "This usually means the document describes the product in prose "
            "without sections I can map to features or data. Naming a section "
            "for the data records the system stores, and one per capability, "
            "is normally enough."
        )
        console.print(
            Panel(message, title="Nothing To Build From This PRD", border_style="red")
        )
        _log_event(
            session_logger,
            "prd.extraction_empty",
            {"sections": len(parsed.sections)},
            "PRD produced no entities and no features",
            workflow_id="prd-build",
        )
        return

    try:
        relative_path = prd_path.relative_to(workspace)
    except ValueError:
        relative_path = prd_path

    output_scope = contract.requested_paths(user_input)
    acceptance = _extract_prd_acceptance_commands(parsed.raw_text or "")

    # A document that lists its data model as bare nouns parses to zero
    # entities, and zero entities leaves the planner with no model layer to
    # generate - a 45-entity specification degraded to a single index.html.
    inferred_entities = await _infer_prd_entities(parsed, console, session_logger)
    project = build_project_spec(
        parsed, request_text=user_input, extra_entities=inferred_entities
    )
    setattr(project, "user_request", user_input)
    _log_prd_contract_summary(project)
    if not project.generation_ready:
        console.print(
            Panel(project.clarification_question, title="PRD Needs Input", border_style="yellow")
        )
        _log_event(
            session_logger,
            "project.needs_input",
            {"project": project.project_name, "question": project.clarification_question},
            "PRD generation stopped for required input",
            workflow_id="prd-build",
        )
        return
    # Every PRD build now enters the same milestone-scoped ReAct executor below.
    # Framework writers, bulk JSON bundles, templates, and deterministic UI
    # hardeners may still be exercised directly by compatibility tests, but they
    # no longer author code for an interactive user build. This keeps one
    # inspect -> act -> verify -> repair loop in charge of every source mutation.
    project_root = _prd_target_directory(user_input, project)
    output_scope = (project_root,)

    milestones, milestone_source = _prd_milestones_for_execution(parsed)
    development_plan = await _prepare_prd_development_plan(
        parsed,
        relative_path,
        project,
        milestones,
        workspace,
        console,
        session_logger,
        cache_path=_development_plan_cache_path(workspace, project, project_root),
        force_replan=bool(_REPLAN_RE.search(user_input)),
    )
    if _prd_development_plan_failed(development_plan):
        _print_prd_development_plan_failure(development_plan, console)
        _log_event(
            session_logger,
            "prd.build.plan_failed",
            {"path": str(prd_path), "error": development_plan.get("error", "")},
            "PRD build stopped because development planner failed validation",
            workflow_id="prd-build",
        )
        return
    milestone_execution_requested = _looks_like_prd_milestone_execution_request(user_input)
    autonomous_requested = _prd_autonomous_execution_requested(user_input)
    if milestone_execution_requested:
        execute_plan = True
        if max_milestones is None:
            max_milestones = 1
    if not execute_plan and not autonomous_requested:
        _print_prd_development_plan(
            parsed,
            relative_path,
            milestones,
            development_plan,
            console,
            build_after=False,
            skill_names=_prd_skill_names_for_project(project),
        )
        _store_pending_prd_plan_execution(
            session_logger,
            user_input=user_input,
            relative_path=relative_path,
            project_root=project_root,
            milestones=milestones,
            development_plan=development_plan,
        )
        _log_event(
            session_logger,
            "prd.build.awaiting_selection",
            {
                "path": str(prd_path),
                "title": parsed.title,
                "sections": list(parsed.sections),
                "plan_source": development_plan.get("source"),
                "project_root": project_root,
                "milestones": len(milestones),
            },
            "Prepared PRD build plan and paused for user milestone selection",
            workflow_id="prd-build",
        )
        _log_assistant_message(
            session_logger,
            "Prepared a PRD build plan and paused for milestone selection.",
            workflow_id="prd-build",
        )
        return

    _print_prd_build_plan(
        parsed,
        relative_path,
        console,
        development_plan=development_plan,
        execution_scope="slice" if max_milestones == 1 else "all",
        skill_names=_prd_skill_names_for_project(project),
    )
    _log_event(
        session_logger,
        "prd.build.planned",
        {
            "path": str(prd_path),
            "title": parsed.title,
            "sections": list(parsed.sections),
            "plan_source": development_plan.get("source"),
        },
        f"Planned PRD build for {prd_path.name}",
        workflow_id="prd-build",
    )

    # `_ensure_git_repo` writes `.gitignore` directly (not through the tool
    # registry), so it runs only after execution is explicitly requested.
    if not output_scope and not _explicitly_read_only(user_input) and not dry_run.active():
        _ensure_git_repo(workspace, console, session_logger)

    console.print(
        "[green]Building now - I'll read the PRD and write files in your workspace. "
        "Type `exit` to stop.[/green]"
    )
    prd_execution_root_path: Path | None = None
    prd_execution_state: dict[str, Any] = {}
    start_milestone_index = 0
    if milestones and milestone_source == "compiled_requirement_ledger":
        contract_obj = getattr(project, "prd_contract", None) or extract_contract(parsed)
        prd_execution_root_path, prd_execution_state = initialize_prd_execution(
            workspace,
            user_input,
            contract_obj,
            prd_path=relative_path.as_posix(),
            execution_key=project_root,
        )
        prd_execution_state = _reopen_invalid_prd_checkpoint(
            prd_execution_root_path,
            prd_execution_state,
            project_root,
            workspace,
            console,
        )
        milestones = milestone_lines_from_state(prd_execution_state)
        start_milestone_index = first_incomplete_milestone_index(prd_execution_state)
        ledger = get_current_run()
        if ledger:
            ledger.log_event(
                "prd_milestone_graph_compiled",
                source=milestone_source,
                milestones=len(milestones),
                artifact="milestones.json",
                execution_dir=prd_execution_root_path.relative_to(workspace).as_posix(),
            )
        if start_milestone_index >= len(milestones):
            console.print(
                "[green]All compiled PRD milestones already have checkpoints. "
                "Running the final verifier over recorded changes.[/green]"
            )
            changed = list(prd_execution_state.get("changed_files") or [])
            await _verify_completed_plan(changed, workspace, console, session_logger)
            return
    if not milestones:
        fallback_preflight = _prd_fallback_preflight(project, project_root)
        result = await _run_agent_chat(
            _build_prd_build_request(
                parsed,
                relative_path,
                output_scope=output_scope,
                acceptance=acceptance,
            )
            + f"\n\nProject root: {project_root}\n"
            + "Create and modify application files only below that directory."
            + "\n\n"
            + _prd_milestone_skill_context(workspace, fallback_preflight),
            workspace,
            console,
            session_logger=session_logger,
            force_long_running=True,
            auto_approve=True,
            allowed_write_paths=output_scope or None,
            allowed_read_paths=output_scope or None,
            allowed_tools=tuple(fallback_preflight.get("allowed_tools") or ()),
            use_long_term_memory=False,
            use_planner=False,
            user_request=_prd_agent_safety_request(project_root),
            hydrate_history=False,
            verify_changes=False,
        )
        changed_files = list(getattr(result, "changed_files", ()) or ())
        if not changed_files:
            ledger = get_current_run()
            if ledger:
                ledger.log_event(
                    "mutation_required_but_missing",
                    route="prd.build",
                    target=", ".join(output_scope) or ".",
                )
        if acceptance:
            passed, failures = _run_prd_validation(
                parsed.raw_text,
                output_scope,
                acceptance,
                workspace,
                console,
                session_logger=session_logger,
            )
            for repair_attempt in range(1, 3):
                if passed or not output_scope:
                    break
                repair_prompt = (
                    f"Validation-guided PRD repair pass {repair_attempt}/2. Fix every failed check "
                    "below while preserving every requirement and every working function from the "
                    "authoritative PRD. Read the current file before writing it. Do not remove working "
                    "features to fix one check. Make a complete correction and run verification. "
                    "Do not ask for confirmation.\n"
                    f"Modify ONLY: {', '.join(output_scope)}.\n\n"
                    f"Authoritative PRD:\n{parsed.raw_text}\n\n"
                    "Current validation failures:\n" + "\n\n".join(failures)
                )
                await _run_agent_chat(
                    repair_prompt,
                    workspace,
                    console,
                    session_logger=session_logger,
                    force_long_running=True,
                    auto_approve=True,
                    allowed_write_paths=output_scope,
                    allowed_read_paths=output_scope,
                    allowed_tools=tuple(fallback_preflight.get("allowed_tools") or ()),
                    use_long_term_memory=False,
                    use_planner=False,
                    user_request=_prd_agent_safety_request(project_root),
                    hydrate_history=False,
                    verify_changes=False,
                )
                passed, failures = _run_prd_validation(
                    parsed.raw_text,
                    output_scope,
                    acceptance,
                    workspace,
                    console,
                    session_logger=session_logger,
                )
        else:
            await _verify_completed_plan(changed_files, workspace, console, session_logger)
        return

    task: MilestoneTask | None = None
    if prd_execution_state.get("task_id"):
        try:
            task = load_task(workspace, str(prd_execution_state["task_id"]))
        except Exception:
            task = None
    if task is None:
        task = _create_prd_build_task(user_input, parsed.title, milestones)
    for previous_index in range(start_milestone_index):
        task = mark_step_done(task, previous_index + 1, "Resumed from PRD execution checkpoint.")
    save_task(task, workspace)
    if prd_execution_root_path is not None and not prd_execution_state.get("task_id"):
        prd_execution_state = attach_task_id(
            prd_execution_root_path,
            prd_execution_state,
            task.task_id,
        )
    console.print(f"[dim]Tracking PRD build task: {task.task_id}[/dim]")
    if start_milestone_index:
        console.print(
            f"[dim]Resuming at milestone {start_milestone_index + 1}/{len(milestones)} "
            f"from {prd_execution_root_path.relative_to(workspace).as_posix()}[/dim]"
        )
    prd_brief = _prd_brief(parsed)
    changed_files: list[str] = list(prd_execution_state.get("changed_files") or [])
    # milestone_id -> why it failed. A failure blocks only the milestones that
    # declare it as a dependency, never the whole build.
    failed_milestones: dict[str, str] = {}
    skipped_milestones: dict[str, str] = {}
    attempted_milestones = 0
    for index in range(start_milestone_index, len(milestones)):
        milestone = milestones[index]
        step = task.steps[index]
        milestone_id = _milestone_id_from_line(milestone)
        preflight: dict[str, Any] = {}
        if prd_execution_root_path is not None:
            preflight = load_milestone_preflight(prd_execution_root_path, milestone_id)
        # Checked BEFORE mark_milestone_running: a milestone that is blocked was
        # never attempted, so it must stay `pending` and must not move the
        # execution state to `running`.
        blocking = _prd_blocking_dependencies(preflight, failed_milestones, skipped_milestones)
        if blocking:
            reason = f"Skipped: depends on {', '.join(blocking)}, which did not complete."
            skipped_milestones[milestone_id] = reason
            console.print(f"[yellow]Milestone {milestone_id}: {reason}[/yellow]")
            ledger = get_current_run()
            if ledger:
                ledger.log_event(
                    "prd_milestone_skipped_blocked",
                    milestone_id=milestone_id,
                    blocked_by=blocking,
                )
            continue
        if prd_execution_root_path is not None:
            prd_execution_state = mark_milestone_running(
                prd_execution_root_path,
                prd_execution_state,
                milestone_id,
            )
            preflight, prd_execution_state = await _prepare_prd_milestone_preflight(
                prd_execution_root_path,
                prd_execution_state,
                milestone_id,
                preflight,
                workspace,
                console,
                session_logger,
            )
            preflight = _scope_prd_preflight(preflight, project_root)
        milestone_transaction_baseline = _prd_transaction_snapshot(workspace)
        milestone_tool_baseline = _prd_tool_snapshot(workspace)
        milestone_changed_baseline = list(changed_files)
        resumed_milestone_changed = next(
            (
                list(item.get("changed_files") or [])
                for item in prd_execution_state.get("milestones") or []
                if isinstance(item, dict) and str(item.get("id") or "") == milestone_id
            ),
            [],
        )
        task = mark_step_running(task, step.id)
        save_task(task, workspace)
        console.print(f"[dim]  -> Milestone {index + 1}/{len(milestones)}: {milestone}[/dim]")
        try:
            # Decompose before implementing: the milestone's atomic tasks are
            # written to .shamsu/plans/<milestone>.contracts.json first, so the
            # unit of work is durable and reviewable rather than implicit in the
            # call stack.
            milestone_contracts = _persist_prd_milestone_contracts(
                preflight,
                milestone,
                milestone_id,
                project_root,
                workspace,
                session_logger,
            )
            # Keyed by target so each file pass can be handed ITS contract -
            # locked write scope, acceptance criteria, verification. Building
            # contracts and then not giving them to the model left every turn
            # grounded only in the prompt text.
            contracts_by_target = {
                path: contract
                for contract in milestone_contracts
                for path in contract.expected_write_paths
            }
            # One approval buys one atomic file pass unless autonomy is on. The
            # milestone stays the planning unit; the file is the execution unit.
            pass_budget = None if is_long_running_enabled(workspace) else 1
            remaining_targets: list[str] = []
            file_pass_changed = await _run_prd_expected_file_passes(
                title=parsed.title,
                relative_path=relative_path,
                prd_brief=prd_brief,
                milestone=milestone,
                preflight=preflight,
                project_root=project_root,
                workspace=workspace,
                console=console,
                session_logger=session_logger,
                max_passes=pass_budget,
                remaining=remaining_targets,
                contracts_by_target=contracts_by_target,
            )
            if not file_pass_changed and _prd_milestone_requires_mutation(preflight):
                # The architecture pass only targets declared files that are
                # missing or invalid. Behavioural requirements name no file, so
                # without this the whole milestone became ONE turn - which is
                # why every non-scaffolding milestone failed.
                file_pass_changed = await _run_prd_behavioural_file_passes(
                    title=parsed.title,
                    relative_path=relative_path,
                    prd_brief=prd_brief,
                    milestone=milestone,
                    preflight=preflight,
                    project_root=project_root,
                    workspace=workspace,
                    console=console,
                    session_logger=session_logger,
                    max_passes=pass_budget,
                    remaining=remaining_targets,
                    contracts_by_target=contracts_by_target,
                )
            if remaining_targets and file_pass_changed:
                # Stop here rather than verifying a milestone that is knowingly
                # half-built: the verifier would fail, the rollback would undo
                # the work that just succeeded, and the user would be told the
                # milestone broke when it was only unfinished.
                _pause_prd_milestone_between_files(
                    session_logger,
                    user_input=user_input,
                    relative_path=relative_path,
                    project_root=project_root,
                    milestone_id=milestone_id,
                    changed=list(file_pass_changed),
                    remaining=remaining_targets,
                    console=console,
                )
                for path in file_pass_changed:
                    if path not in changed_files:
                        changed_files.append(path)
                if prd_execution_root_path is not None:
                    prd_execution_state = checkpoint_milestone(
                        prd_execution_root_path,
                        prd_execution_state,
                        milestone_id,
                        changed_files=list(file_pass_changed),
                        evidence=[f"changed:{path}" for path in file_pass_changed],
                        status="pending",
                        message=(
                            "Paused after one file pass; "
                            f"{len(remaining_targets)} file(s) still to build."
                        ),
                    )
                save_task(task, workspace)
                return
            if file_pass_changed or resumed_milestone_changed:
                accumulated = list(
                    dict.fromkeys([*resumed_milestone_changed, *file_pass_changed])
                )
                result = AgentLoopResult(
                    final=(
                        "Existing milestone mutations are ready; proceed to deterministic "
                        "milestone verification before further model edits."
                    ),
                    changed_files=tuple(accumulated),
                )
            else:
                result = await _run_agent_chat(
                    _build_prd_milestone_request(
                        parsed.title,
                        relative_path,
                        prd_brief,
                        milestones,
                        index + 1,
                        len(milestones),
                        preflight=preflight,
                        project_root=project_root,
                        skill_context=_prd_milestone_skill_context(workspace, preflight),
                    ),
                    workspace,
                    console,
                    session_logger=session_logger,
                    force_long_running=True,
                    auto_approve=True,
                    allowed_write_paths=output_scope,
                    allowed_read_paths=output_scope,
                    allowed_tools=tuple(preflight.get("allowed_tools") or ()),
                    use_long_term_memory=False,
                    use_planner=False,
                    user_request=_prd_agent_safety_request(project_root),
                    hydrate_history=False,
                    verify_changes=False,
                )
        except Exception as exc:
            task = mark_step_failed(task, step.id, str(exc))
            if prd_execution_root_path is not None:
                prd_execution_state, rollback_result = _rollback_failed_prd_milestone(
                    prd_execution_root_path,
                    prd_execution_state,
                    milestone_id,
                    preflight,
                    _prd_transactions_since(workspace, milestone_transaction_baseline),
                    workspace,
                    console,
                    preserved_changed_files=milestone_changed_baseline,
                )
                prd_execution_state = checkpoint_milestone(
                    prd_execution_root_path,
                    prd_execution_state,
                    milestone_id,
                    evidence=_prd_rollback_evidence(rollback_result),
                    status="failed",
                    message=str(exc),
                )
            save_task(task, workspace)
            raise
        milestone_changed = list(resumed_milestone_changed)
        for path in file_pass_changed:
            if path not in milestone_changed:
                milestone_changed.append(path)
        for path in getattr(result, "changed_files", ()) or ():
            if path not in milestone_changed:
                milestone_changed.append(path)
        for path in milestone_changed:
            if path not in changed_files:
                changed_files.append(path)
        if prd_execution_root_path is not None and getattr(result, "awaiting_user", False):
            reason = getattr(result, "final", "") or "Agent requested user input."
            prd_execution_state = block_milestone(
                prd_execution_root_path,
                prd_execution_state,
                milestone_id,
                reason,
            )
            task = mark_step_blocked(task, step.id, reason)
            save_task(task, workspace)
            console.print(
                Panel(
                    f"PRD milestone {milestone_id} is blocked for user input.",
                    title="PRD Build Paused",
                    border_style="yellow",
                )
            )
            return
        agent_stop_reason = ""
        if (
            prd_execution_root_path is not None
            and getattr(result, "stopped", False)
            and not milestone_changed
        ):
            agent_stop_reason = (
                getattr(result, "final", "") or "Agent stopped before completing the milestone."
            )
        step_done_message = "Agent completed this milestone build pass."
        if prd_execution_root_path is not None:
            unresolved_commands = _prd_unrecovered_command_failures(
                workspace, milestone_tool_baseline
            )
            if agent_stop_reason:
                checkpoint_status = "failed"
                verification = _milestone_verification_payload(
                    "failed",
                    files=milestone_changed,
                    summary=agent_stop_reason,
                )
            elif unresolved_commands:
                checkpoint_status = "failed"
                verification = _prd_command_failure_verification(unresolved_commands)
            else:
                checkpoint_status, verification = await _verify_prd_milestone(
                    milestone_id,
                    preflight,
                    milestone_changed,
                    workspace,
                    console,
                    session_logger,
                )
            if checkpoint_status == "failed" and _prd_milestone_repair_enabled():
                (
                    checkpoint_status,
                    verification,
                    milestone_changed,
                    prd_execution_state,
                ) = await _repair_failed_prd_milestone(
                    prd_execution_root_path,
                    prd_execution_state,
                    milestone_id,
                    preflight,
                    verification,
                    milestone_changed,
                    workspace,
                    console,
                    session_logger,
                    parsed.title,
                    relative_path,
                    prd_brief,
                    milestones,
                    index + 1,
                    len(milestones),
                )
                for path in milestone_changed:
                    if path not in changed_files:
                        changed_files.append(path)
            if checkpoint_status == "blocked":
                reason = str(
                    verification.get("summary") or "Milestone repair requested user input."
                )
                prd_execution_state = block_milestone(
                    prd_execution_root_path,
                    prd_execution_state,
                    milestone_id,
                    reason,
                )
                task = mark_step_blocked(task, step.id, reason)
                save_task(task, workspace)
                console.print(
                    Panel(
                        f"PRD milestone {milestone_id} is blocked for user input.",
                        title="PRD Build Paused",
                        border_style="yellow",
                    )
                )
                return
            rollback_result: dict[str, Any] = {}
            if checkpoint_status == "failed":
                prd_execution_state, rollback_result = _rollback_failed_prd_milestone(
                    prd_execution_root_path,
                    prd_execution_state,
                    milestone_id,
                    preflight,
                    _prd_transactions_since(workspace, milestone_transaction_baseline),
                    workspace,
                    console,
                    preserved_changed_files=milestone_changed_baseline,
                )
                milestone_changed = _prd_checkpoint_changed_after_rollback(
                    milestone_changed,
                    rollback_result,
                )
            evidence = [f"changed:{path}" for path in milestone_changed]
            verification_status = str(verification.get("status") or "")
            if verification.get("command"):
                evidence.append(f"verification:{verification_status}:{verification['command']}")
            elif verification_status:
                evidence.append(f"verification:{verification_status}")
            evidence.extend(_prd_rollback_evidence(rollback_result))
            if not evidence:
                evidence = ["agent_completed_milestone_pass_without_verifier"]
            prd_execution_state = checkpoint_milestone(
                prd_execution_root_path,
                prd_execution_state,
                milestone_id,
                changed_files=milestone_changed,
                evidence=evidence,
                status=checkpoint_status,
                message=str(
                    verification.get("summary") or "Agent completed this milestone build pass."
                ),
                verification=verification,
            )
            ledger = get_current_run()
            if ledger:
                ledger.log_event(
                    "prd_milestone_checkpointed",
                    milestone_id=milestone_id,
                    status=checkpoint_status,
                    changed_files=milestone_changed,
                    verification_status=verification_status,
                    execution_dir=prd_execution_root_path.relative_to(workspace).as_posix(),
                )
            if checkpoint_status == "failed":
                failure_message = (
                    f"PRD milestone {milestone_id} remains FAILED after its repair budget: "
                    + str(verification.get("summary") or "Milestone verification failed.")
                )
                _log_assistant_message(
                    session_logger,
                    failure_message,
                    workflow_id="prd-build",
                )
                task = mark_step_failed(
                    task,
                    step.id,
                    str(verification.get("summary") or "Milestone verification failed."),
                )
                save_task(task, workspace)
                # One failed milestone used to `return` out of the WHOLE build,
                # so a single failure ended all 23 - at even 90% per-milestone
                # success that is 0.9^23 ~ 9%, which is why no run ever
                # finished. Keep the failure local: independent milestones
                # still run, only dependents are skipped.
                failed_milestones[milestone_id] = str(
                    verification.get("summary") or "Milestone verification failed."
                )
                console.print(
                    f"[yellow]Milestone {milestone_id} failed; continuing with "
                    f"milestones that do not depend on it.[/yellow]"
                )
                attempted_milestones += 1
                if max_milestones is not None and attempted_milestones >= max_milestones:
                    if _pause_prd_build_after_slice(
                        session_logger,
                        user_input=user_input,
                        relative_path=relative_path,
                        project_root=project_root,
                        milestones=milestones,
                        current_index=index - 1,
                        development_plan=development_plan,
                        console=console,
                        completed=False,
                    ):
                        return
                continue
            step_done_message = (
                "Milestone verified."
                if checkpoint_status == "verified"
                else "Milestone implemented, but no deterministic verifier was available."
            )
        task = mark_step_done(task, step.id, step_done_message)
        if index < len(milestones) - 1:
            if not advance_phase(task, f"milestone-{index + 2}"):
                save_task(task, workspace)
                console.print(
                    Panel(task.next_action, title="PRD Build Paused", border_style="yellow")
                )
                return
        save_task(task, workspace)
        attempted_milestones += 1
        if max_milestones is not None and attempted_milestones >= max_milestones:
            if _pause_prd_build_after_slice(
                session_logger,
                user_input=user_input,
                relative_path=relative_path,
                project_root=project_root,
                milestones=milestones,
                current_index=index,
                development_plan=development_plan,
                console=console,
            ):
                return
    if failed_milestones or skipped_milestones:
        # Honest partial outcome: say which milestones landed and which did not,
        # instead of the old behaviour of aborting on the first failure. The
        # final integration verifier is NOT run - the build is knowingly
        # incomplete, so a whole-project verdict would be misleading.
        report = _prd_build_completion_report(milestones, failed_milestones, skipped_milestones)
        console.print(Panel(report, title="PRD Build: Partial", border_style="yellow"))
        _log_assistant_message(session_logger, report, workflow_id="prd-build")
        ledger = get_current_run()
        if ledger:
            ledger.log_event(
                "prd_build_partial",
                completed=len(milestones) - len(failed_milestones) - len(skipped_milestones),
                total=len(milestones),
                failed=sorted(failed_milestones),
                skipped=sorted(skipped_milestones),
            )
        return
    console.print(f"[green]PRD milestone build flow complete. Task: {task.task_id}[/green]")
    # Integration check across everything the milestones built (mirrors /proceed).
    verified = await _verify_completed_plan(changed_files, workspace, console, session_logger)
    if verified and prd_execution_root_path is not None:
        for milestone in list(prd_execution_state.get("milestones") or []):
            if not isinstance(milestone, dict):
                continue
            prd_execution_state = checkpoint_milestone(
                prd_execution_root_path,
                prd_execution_state,
                str(milestone.get("id") or ""),
                status="verified",
                evidence=["final_verifier_passed"],
                message="Final verifier passed after compiled PRD milestone build.",
            )


def _log_prd_contract_summary(project: Any) -> None:
    ledger = get_current_run()
    if ledger is None:
        return
    contract = getattr(project, "prd_contract", None)
    suitability = getattr(project, "suitability", None)
    entities = list(getattr(project, "entities", []) or [])
    entity_summaries = [
        {
            "name": getattr(entity, "name", ""),
            "fields": [getattr(field, "name", "") for field in getattr(entity, "fields", [])],
        }
        for entity in entities[:50]
    ]
    contract_dict = contract.to_dict() if hasattr(contract, "to_dict") else {}
    suitability_dict = suitability.to_dict() if hasattr(suitability, "to_dict") else {}
    artifact = {
        "project": getattr(project, "project_name", ""),
        "app": getattr(project, "app_name", ""),
        "generation_ready": bool(getattr(project, "generation_ready", False)),
        "needs_input": bool(getattr(project, "needs_input", False)),
        "clarification_question": getattr(project, "clarification_question", ""),
        "archetype": str(getattr(project, "archetype", "")),
        "category": getattr(project, "category", ""),
        "entities": entity_summaries,
        "contract": contract_dict,
        "suitability": suitability_dict,
    }
    try:
        (ledger.run_dir / "prd-contract.json").write_text(
            json.dumps(artifact, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except OSError:
        pass
    requirement_count = 0
    milestone_count = 0
    if contract is not None:
        try:
            requirement_ledger = compile_requirement_ledger(contract)
            requirement_artifacts = save_prd_execution_artifacts(contract, ledger.run_dir)
            requirement_count = len(requirement_ledger.requirements)
            milestone_count = len(requirement_ledger.milestones)
        except Exception:
            requirement_artifacts = {}
    ledger.log_event(
        "prd_contract_extracted",
        project=getattr(project, "project_name", ""),
        generation_ready=bool(getattr(project, "generation_ready", False)),
        needs_input=bool(getattr(project, "needs_input", False)),
        entity_count=len(entities),
        entities=[item["name"] for item in entity_summaries],
        required_stack=list(getattr(contract, "required_stack", []) or []),
        strategy=suitability_dict.get("strategy", ""),
        warnings=list(getattr(contract, "extraction_warnings", []) or []),
        artifact="prd-contract.json",
    )
    if requirement_count or milestone_count:
        ledger.log_event(
            "prd_requirement_ledger_compiled",
            requirements=requirement_count,
            milestones=milestone_count,
            artifacts=requirement_artifacts,
        )


def _prd_target_directory(user_input: str, project: Any) -> str:
    match = re.search(
        r"\b(?:new\s+)?(?:folder|directory)\s+(?:named|called)\s+"
        r"[\x60\"']?(?P<path>[A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)*)[\x60\"']?",
        user_input,
        re.IGNORECASE,
    )
    if match:
        return match.group("path").rstrip(".")
    return str(getattr(project, "project_name", "generated-app") or "generated-app")


def _prd_agent_safety_request(project_root: str) -> str:
    """Keep top-level intent routing out of orchestrated coding child turns."""
    return f"Implement the current coding milestone inside project root {project_root}."


def _planned_django_paths(project: Any, target_dir: str) -> list[str]:
    prefix = Path(target_dir)
    paths = [
        (prefix / str(file_spec.path)).as_posix()
        for file_spec in getattr(project, "generation_order", [])
    ]
    app_name = str(getattr(project, "app_name", "app") or "app")
    paths.extend(
        [
            (prefix / "SHAMSU_SUMMARY.md").as_posix(),
            (prefix / "db.sqlite3").as_posix(),
            (prefix / app_name / "migrations" / "0001_initial.py").as_posix(),
        ]
    )
    return list(dict.fromkeys(paths))


async def _run_freeform_prd_build(
    user_input: str,
    prd_path: Path,
    project: Any,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    prd_text: str = "",
    acceptance: list[tuple[str, str]] | None = None,
) -> FullPipelineResult:
    """Run the structured template-free PRD pipeline.

    This is the right path for complex/bespoke apps (non-Django requested
    stacks, browser+CLI products, CMS-like products). It forces an actual file
    plan, writes files transactionally, and verifies instead of letting a plain
    chat response describe work without doing it.
    """
    target_dir = _prd_target_directory(user_input, project)
    target = Sandbox(workspace).validate(target_dir)
    if target.exists() and any(target.iterdir()):
        message = (
            f"The requested project folder is not empty: {target_dir}. "
            "Choose a new folder so the PRD build cannot overwrite unrelated work."
        )
        console.print(Panel(message, title="PRD Build Needs Input", border_style="yellow"))
        ledger = get_current_run()
        if ledger:
            ledger.log_event("run_needs_input", reason="prd_target_not_empty", target=target_dir)
        _log_assistant_message(session_logger, message, workflow_id="prd-build")
        return FullPipelineResult(
            prd_path=prd_path,
            target_dir=target,
            project=project,
            written_files=[],
            success=False,
            error=message,
        )

    ledger = get_current_run()
    if ledger:
        ledger.log_event(
            "prd_freeform_build_started",
            target=target_dir,
            strategy=getattr(getattr(project, "suitability", None), "strategy", ""),
        )

    search, _uses_real_index = _build_search_agent(workspace, session_logger)
    result = await FullDjangoPipeline(
        workspace,
        search=search,
        session_logger=session_logger,
        approval_func=lambda _request: True,
        long_running=True,
        generate=_pipeline_generate(session_logger),
        user_request=user_input,
    ).run(prd_path, target_dir=target_dir)

    written = list(result.written_files or [])
    acceptance_items = list(acceptance or [])
    if result.success and acceptance_items:
        result = await _validate_freeform_prd_result(
            result,
            prd_text,
            acceptance_items,
            console,
            session_logger=session_logger,
        )
        written = list(result.written_files or [])

    _print_full_pipeline_result(result, console)
    if ledger:
        if written:
            ledger.log_event(
                "prd_freeform_build_finished",
                target=target_dir,
                written_files=written,
                success=bool(result.success),
                error=result.error,
            )
        else:
            ledger.log_event("mutation_required_but_missing", route="prd.build", target=target_dir)

    if result.success:
        message = (
            f"Built {getattr(project, 'project_name', 'the project')} in {target_dir}. "
            f"Generated {len(written)} files and verification passed."
        )
    else:
        message = (
            f"The PRD freeform build generated {len(written)} files in {target_dir}, "
            f"but verification did not pass: {result.error or 'see logs for details'}"
        )
    _log_assistant_message(session_logger, message, workflow_id="prd-build")
    return result


async def _validate_freeform_prd_result(
    result: FullPipelineResult,
    prd_text: str,
    acceptance: list[tuple[str, str]],
    console: Console,
    session_logger: SessionLogger | None = None,
) -> FullPipelineResult:
    written = tuple(path.replace("\\", "/") for path in (result.written_files or []) if path)
    repair_targets = _source_repair_targets(written)
    ledger = get_current_run()
    if not written:
        error = "PRD validation failed: the freeform build reported no generated files."
        if ledger:
            ledger.log_event("prd_freeform_validation_finished", success=False, error=error)
        return replace(result, success=False, error=error)

    target = result.target_dir
    passed, failures = _run_prd_validation(
        prd_text,
        written,
        acceptance,
        target,
        console,
        session_logger=session_logger,
    )
    for repair_attempt in range(1, 3):
        if passed:
            break
        rewritten = await _structured_validation_rewrite(
            prd_text,
            failures,
            repair_targets,
            target,
            console,
            session_logger=session_logger,
        )
        if rewritten:
            passed, failures = _run_prd_validation(
                prd_text,
                written,
                acceptance,
                target,
                console,
                session_logger=session_logger,
            )
            continue
        if not repair_targets:
            break
        repair_prompt = (
            f"Validation-guided PRD repair pass {repair_attempt}/2. Fix every failed check below "
            "while preserving every requirement and every working feature from the authoritative PRD. "
            "Read the current file before writing it. Do not remove working behavior to fix one check. "
            "Make a complete correction and run verification. Do not ask for confirmation.\n"
            f"Modify ONLY: {', '.join(repair_targets)}.\n\n"
            f"Authoritative PRD:\n{prd_text}\n\n"
            "Current validation failures:\n" + "\n\n".join(failures)
        )
        await _run_agent_chat(
            repair_prompt,
            target,
            console,
            session_logger=session_logger,
            force_long_running=True,
            auto_approve=True,
            allowed_write_paths=repair_targets,
            use_long_term_memory=False,
            use_planner=False,
            hydrate_history=False,
            verify_changes=False,
        )
        passed, failures = _run_prd_validation(
            prd_text,
            written,
            acceptance,
            target,
            console,
            session_logger=session_logger,
        )

    if ledger:
        ledger.log_event(
            "prd_freeform_validation_finished",
            success=passed,
            failures=failures[:5],
        )
    if passed:
        return result
    error = "PRD validation failed: " + (failures[0] if failures else "acceptance checks failed")
    return replace(result, success=False, error=error)


_VALIDATION_REPAIR_EXTENSIONS = frozenset(
    {".py", ".js", ".jsx", ".ts", ".tsx", ".html", ".css", ".json"}
)
_VALIDATION_REPAIR_JSON_FILES = frozenset(
    {
        "package.json",
        "tsconfig.json",
        "vite.config.json",
        "biome.json",
        "eslint.config.json",
    }
)

VALIDATION_REWRITE_SYSTEM = """You are SHAMSU performing a validation-guided full-file rewrite.
Output ONLY JSON: {"content": "<the complete corrected file contents>"}.
Rules:
- Rewrite exactly one existing source file.
- Preserve working behavior unless it conflicts with the PRD.
- Fix the listed validation failures against the authoritative PRD.
- Return the complete file from first line to last line, not a patch.
- No Markdown fences or prose outside JSON.
- For CLIs, implement the exact failing command syntax. If an option appears
  after a subcommand, accept it there; do not put it only on the root parser.
"""


def _source_repair_targets(paths: tuple[str, ...]) -> tuple[str, ...]:
    targets: list[str] = []
    for path in paths:
        suffix = Path(path).suffix.lower()
        if suffix == ".json" and Path(path).name.lower() not in _VALIDATION_REPAIR_JSON_FILES:
            continue
        if suffix in _VALIDATION_REPAIR_EXTENSIONS and path not in targets:
            targets.append(path)
    return tuple(targets)


async def _structured_validation_rewrite(
    prd_text: str,
    failures: list[str],
    target_paths: tuple[str, ...],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> list[str]:
    if not target_paths or not failures:
        return []
    ledger = get_current_run()
    llm = _make_llm_manager(session_logger, console, workspace)
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(
            workspace, session_logger, console, lambda _request: True
        ),
        action_ledger=ledger,
    )
    registry.set_allowed_write_paths(target_paths)
    changed: list[str] = []
    timeout_seconds = float(os.environ.get("SHAMSU_VALIDATION_REPAIR_TIMEOUT_SECONDS", "120"))
    num_predict = _env_int_at_least("SHAMSU_VALIDATION_REPAIR_NUM_PREDICT", 4096, 1024)
    for path in target_paths[:2]:
        target = (workspace / path).resolve()
        try:
            target.relative_to(workspace)
            current = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        prompt = _validation_rewrite_prompt(path, current, prd_text, failures)
        try:
            raw = await asyncio.wait_for(
                llm.generate_structured(
                    "coder",
                    VALIDATION_REWRITE_SYSTEM,
                    prompt,
                    FILE_CONTENT_SCHEMA,
                    num_predict=num_predict,
                ),
                timeout=timeout_seconds,
            )
        except Exception as exc:
            if ledger:
                ledger.log_event(
                    "prd_validation_rewrite_failed",
                    target=path,
                    error=f"{type(exc).__name__}: {exc}",
                )
            continue
        data = _loads_freeform_json(raw or "")
        if not isinstance(data, dict) or not isinstance(data.get("content"), str):
            continue
        content = _sanitize_generated_content(str(data["content"]), path)
        if not _valid_validation_rewrite(path, current, content):
            if ledger:
                ledger.log_event("prd_validation_rewrite_rejected", target=path)
            continue
        call_id = ledger.log_tool_call("write_file", {"filepath": path}) if ledger else ""
        result = registry.execute("write_file", {"filepath": path, "content": content})
        if ledger:
            ledger.log_tool_result(call_id, "write_file", result.ok, result.message, result.data)
        if result.ok:
            changed.append(path)
    return changed


def _validation_rewrite_prompt(
    path: str,
    current: str,
    prd_text: str,
    failures: list[str],
) -> str:
    failure_text = "\n\n".join(failures[:8])
    return (
        f"## File to rewrite\n{path}\n\n"
        f"## Authoritative PRD\n{prd_text}\n\n"
        f"## Validation failures to fix\n{failure_text}\n\n"
        f"## Current file content\n{current}\n\n"
        '## Task\nReturn JSON {"content": "..."} with the complete corrected file.'
    )


def _valid_validation_rewrite(path: str, current: str, content: str) -> bool:
    if not content.strip() or content == current:
        return False
    current_lines = max(1, len(current.splitlines()))
    new_lines = len(content.splitlines())
    if new_lines * 2 < current_lines:
        return False
    if Path(path).suffix.lower() == ".py":
        try:
            compile(content, path, "exec")
        except SyntaxError:
            return False
    return True


async def _run_django_prd_build(
    user_input: str,
    prd_path: Path,
    project: Any,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> FullPipelineResult:
    """Run the deterministic PRD pipeline for supported CRUD/API products."""
    target_dir = _prd_target_directory(user_input, project)
    target = Sandbox(workspace).validate(target_dir)
    if target.exists() and any(target.iterdir()):
        message = (
            f"The requested project folder is not empty: {target_dir}. "
            "Choose a new folder so the PRD build cannot overwrite unrelated work."
        )
        console.print(Panel(message, title="PRD Build Needs Input", border_style="yellow"))
        ledger = get_current_run()
        if ledger:
            ledger.log_event("run_needs_input", reason="prd_target_not_empty", target=target_dir)
        _log_assistant_message(session_logger, message, workflow_id="prd-build")
        return FullPipelineResult(
            prd_path=prd_path,
            target_dir=target,
            project=project,
            written_files=[],
            success=False,
            error=message,
        )

    ledger = get_current_run()
    if ledger:
        try:
            prd_text = parse_prd_file(prd_path).raw_text or ""
        except PRDParseError:
            prd_text = ""
        ledger.log_context_preview(
            {
                "task_id": "prd-build",
                "specialist": "deterministic-prd-pipeline",
                "token_estimate": max((len(user_input) + len(prd_text)) // 4, 1),
                "user_request": user_input[:8000],
                "prd_context": prd_text[:16000],
                "snippets": [],
                "omitted_context": {
                    "user_request_chars": max(len(user_input) - 8000, 0),
                    "prd_context_chars": max(len(prd_text) - 16000, 0),
                },
                "deterministic": True,
            }
        )
    transactions = TransactionWorkspace(workspace)
    planned_paths = _planned_django_paths(project, target_dir)
    transaction_id = transactions.begin(
        f"Build {getattr(project, 'project_name', 'project')} from {prd_path.name}",
        [{"op": "prd_build", "path": path} for path in planned_paths],
        destructive=False,
    )
    for path in planned_paths:
        transactions.backup_file(transaction_id, path)
    if ledger:
        ledger.log_mutation_started(transaction_id, "Deterministic PRD-to-project build")
        ledger.log_event(
            "prd_parsed",
            path=str(prd_path),
            project=getattr(project, "project_name", ""),
            target=target_dir,
        )

    command_runner = CommandRunner(
        workspace,
        approval_func=lambda _request: True,
        session_logger=session_logger,
        action_ledger=ledger,
    )
    search, _uses_real_index = _build_search_agent(workspace, session_logger)
    result = await FullDjangoPipeline(
        workspace,
        search=search,
        session_logger=session_logger,
        approval_func=lambda _request: True,
        setup_runner=DjangoSetupRunner(
            workspace,
            command_runner=command_runner,
            session_logger=session_logger,
        ),
        test_runner=DjangoTestRunner(
            workspace,
            command_runner=command_runner,
            session_logger=session_logger,
        ),
        long_running=True,
    ).run(prd_path, target_dir=target_dir)

    demo_credentials: tuple[str, str] | None = None
    if result.success and _requests_demo_login(user_input, project):
        seeded, seed_error = _seed_django_demo_login(target, command_runner)
        if seeded:
            demo_credentials = ("demo@example.com", "ShamsuDemo123!")
            _append_demo_login_docs(target, *demo_credentials)
            if ledger:
                ledger.log_event(
                    "demo_credentials_seeded",
                    target=target_dir,
                    username=demo_credentials[0],
                )
        else:
            result = replace(
                result,
                success=False,
                error=f"Django project passed setup and tests, but demo login seeding failed: {seed_error}",
            )

    touched = [path for path in planned_paths if (workspace / path).exists()]
    for path in touched:
        transactions.record_after(transaction_id, path)
    mutation_status = "applied" if touched else "failed"
    manifest = transactions.finalize(
        transaction_id,
        mutation_status,
        "" if touched else (result.error or "PRD build wrote no files"),
    )
    if ledger:
        ledger.log_mutation_finished(
            transaction_id,
            mutation_status,
            touched_files=touched,
            rollback_available=bool(touched),
            error="" if touched else (result.error or "PRD build wrote no files"),
            operations=list(manifest.get("operations", [])),
            before_hashes=dict(manifest.get("before_hashes", {})),
            after_hashes=dict(manifest.get("after_hashes", {})),
            backups=dict(manifest.get("backups", {})),
            verification={
                "ran": bool(result.setup_result or result.test_result),
                "passed": bool(result.success),
            },
        )
        if not touched:
            ledger.log_event("mutation_required_but_missing", route="prd.build", target=target_dir)
        else:
            verifier_id = ledger.verifier_id_for("django setup and test", "prd_pipeline")
            ledger.log_verification_started(
                "django setup and test",
                verifier_id=verifier_id,
                source="prd_pipeline",
                required=True,
                files=touched,
            )
            ledger.log_verification_result(
                bool(result.success),
                "" if result.success else result.error,
                command="django setup and test",
                verifier_id=verifier_id,
                source="prd_pipeline",
                required=True,
                files=touched,
            )

    _print_full_pipeline_result(result, console)
    if result.success:
        message = (
            f"Built {getattr(project, 'project_name', 'the project')} in {target_dir}. "
            f"Generated {len(touched)} files; setup and tests passed."
        )
        if demo_credentials:
            message += (
                f" Demo login: {demo_credentials[0]} / {demo_credentials[1]}. "
                f"Run it with {sys.executable} manage.py runserver from {target_dir}."
            )
    else:
        message = (
            f"The PRD build created {len(touched)} files in {target_dir}, but verification failed: "
            f"{result.error or 'see the command logs for details'}"
        )
    _log_assistant_message(session_logger, message, workflow_id="prd-build")
    if result.success:
        _record_task_memory(
            workspace,
            f"PRD build request: {user_input}",
            session_logger=session_logger,
            metadata={"workflow": "prd-build", "target": target_dir},
        )
    return result


def _requests_demo_login(user_input: str, project: Any) -> bool:
    lowered = user_input.casefold()
    requested = any(
        term in lowered for term in ("seed", "demo data", "demo login", "login credentials")
    )
    pages = list(getattr(project, "pages", []) or [])
    has_login = any(
        getattr(page, "page_type", "") == "auth" or "login" in getattr(page, "name", "").casefold()
        for page in pages
    )
    return requested and has_login


def _seed_django_demo_login(target: Path, command_runner: CommandRunner) -> tuple[bool, str]:
    code = (
        "from django.contrib.auth import get_user_model; "
        "User=get_user_model(); "
        "user,_=User.objects.get_or_create(username='demo@example.com', "
        "defaults={'email':'demo@example.com','first_name':'Demo'}); "
        "user.email='demo@example.com'; user.first_name='Demo'; "
        "user.set_password('ShamsuDemo123!'); user.save()"
    )
    command = f'"{sys.executable}" manage.py shell -c "{code}"'
    exit_code, stdout, stderr = command_runner.run(command, target)
    if exit_code == 0:
        return True, ""
    return False, (stderr or stdout or f"seed command exited with {exit_code}").strip()


def _append_demo_login_docs(target: Path, username: str, password: str) -> None:
    section = f"\n\n## Demo Login\n\n- Email: `{username}`\n- Password: `{password}`\n"
    for name in ("README.md", "SHAMSU_SUMMARY.md"):
        path = target / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if "## Demo Login" not in text:
            path.write_text(text.rstrip() + section, encoding="utf-8")


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


def _build_prd_build_request(
    parsed,
    relative_path: Path,
    *,
    output_scope: tuple[str, ...] = (),
    acceptance: list[tuple[str, str]] | None = None,
) -> str:
    scope_text = ""
    if output_scope:
        scope_text = (
            "\nWrite scope: modify ONLY these explicitly requested output files: "
            + ", ".join(output_scope)
            + ". Do not edit, overwrite, or create any other workspace file.\n"
        )
    acceptance_items = list(acceptance or [])
    acceptance_text = ""
    if acceptance_items:
        lines = ["\nMandatory acceptance checks (the harness will run these exactly):"]
        for command, expected in acceptance_items:
            suffix = f" -> expected stdout: {expected!r}" if expected else ""
            lines.append(f"- {command}{suffix}")
        acceptance_text = "\n".join(lines) + "\n"
    return (
        f"{PRD_BUILD_FRAMING}\n\n"
        "Build the complete product described by the following PRD. Create all necessary files "
        "in the workspace, working milestone by milestone. Do not claim work you did not do."
        f"{scope_text}{acceptance_text}\n"
        f"=== PRD: {relative_path.as_posix()} ===\n"
        f"{parsed.raw_text or _render_sections(parsed)}"
    )


_ACCEPTANCE_COMMAND_RE = re.compile(
    r"^\s*[-*]\s*`(?P<command>[^`]+)`"
    r"(?:\s+(?:prints?|outputs?|returns?)\s+`(?P<expected>[^`]*)`)?",
    re.IGNORECASE | re.MULTILINE,
)


def _extract_prd_acceptance_commands(text: str) -> list[tuple[str, str]]:
    """Extract explicit backticked acceptance commands and expected stdout."""
    commands: list[tuple[str, str]] = []
    for match in _ACCEPTANCE_COMMAND_RE.finditer(text or ""):
        command = match.group("command").strip()
        if not re.match(
            r"^(?:python3?|py|pytest|npm|npx|node|pnpm|yarn|cargo|go|dotnet|java|mvn|gradle)\b",
            command,
            re.IGNORECASE,
        ):
            continue
        item = (command, (match.group("expected") or "").strip())
        if item not in commands:
            commands.append(item)
    return commands


def _run_prd_acceptance_commands(
    acceptance: list[tuple[str, str]],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    failure_details: list[str] | None = None,
    summary_out: list[str] | None = None,
    log_assistant: bool = True,
) -> bool:
    """Run PRD acceptance commands and record semantic pass/fail evidence."""
    ledger = get_current_run()
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(
            workspace, session_logger, console, lambda _request: True
        ),
        action_ledger=ledger,
    )
    all_passed = True
    lines: list[str] = []
    for command, expected in acceptance:
        if ledger:
            verifier_id = ledger.verifier_id_for(command, "prd_acceptance")
            ledger.log_verification_started(
                command,
                verifier_id=verifier_id,
                source="prd_acceptance",
                required=True,
            )
            call_id = ledger.log_tool_call("run_command", {"command": command})
        else:
            verifier_id = ""
            call_id = ""
        result = registry.execute("run_command", {"command": command})
        stdout = str(result.data.get("stdout", "")).strip()
        matches_expected = not expected or stdout == expected
        passed = bool(result.ok) and matches_expected
        all_passed = all_passed and passed
        if ledger:
            ledger.log_tool_result(call_id, "run_command", passed, result.message, result.data)
            ledger.log_verification_result(
                passed,
                result.message,
                command=command,
                verifier_id=verifier_id,
                source="prd_acceptance",
                required=True,
                files=[],
                expected_stdout=expected,
                actual_stdout=stdout,
                exit_code=result.data.get("exit_code"),
            )
        verdict = "PASS" if passed else "FAIL"
        detail = stdout or str(result.data.get("stderr", "")).strip() or result.message
        lines.append(f"{verdict}  {command}\n{detail}")
        if not passed and failure_details is not None:
            hint = _acceptance_failure_hint(command, detail)
            failure_details.append(
                f"Failed command: {command}\nExpected stdout: {expected or '(exit 0)'}\n"
                f"Actual result:\n{detail}" + (f"\nRepair hint: {hint}" if hint else "")
            )
    summary = (
        "PRD acceptance passed.\n" if all_passed else "PRD acceptance failed.\n"
    ) + "\n\n".join(lines)
    console.print(
        Panel(
            summary,
            title="PRD Acceptance",
            border_style="green" if all_passed else "red",
        )
    )
    if summary_out is not None:
        summary_out.append(summary)
    if log_assistant:
        _log_assistant_message(session_logger, summary, workflow_id="prd-build")
    return all_passed


def _acceptance_failure_hint(command: str, detail: str) -> str:
    if "unrecognized arguments:" not in (detail or "").lower():
        return ""
    try:
        parts = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        parts = []
    option_tokens = [part for part in parts[2:] if part.startswith("-")]
    if option_tokens:
        return (
            "The acceptance command passes options after the subcommand "
            f"({', '.join(option_tokens)}). In argparse, add those options to each "
            "subparser that needs them or parse the command manually; a root parser "
            "option alone will reject this command shape."
        )
    return (
        "Implement the exact acceptance command syntax; argparse root options do not "
        "automatically work after a subcommand."
    )


def _run_prd_validation(
    prd_text: str,
    output_scope: tuple[str, ...],
    acceptance: list[tuple[str, str]],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> tuple[bool, list[str]]:
    failures: list[str] = []
    summaries: list[str] = []
    conformance_passed = _run_prd_conformance_checks(
        prd_text,
        output_scope,
        acceptance,
        workspace,
        console,
        session_logger=session_logger,
        failure_details=failures,
        summary_out=summaries,
    )
    acceptance_passed = _run_prd_acceptance_commands(
        acceptance,
        workspace,
        console,
        session_logger=session_logger,
        failure_details=failures,
        summary_out=summaries,
        log_assistant=False,
    )
    passed = conformance_passed and acceptance_passed
    summary = (
        "PRD validation passed.\n\n" if passed else "PRD validation failed.\n\n"
    ) + "\n\n".join(summaries)
    _log_assistant_message(session_logger, summary, workflow_id="prd-build")
    return passed, failures


def _run_prd_conformance_checks(
    prd_text: str,
    output_scope: tuple[str, ...],
    acceptance: list[tuple[str, str]],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    failure_details: list[str] | None = None,
    summary_out: list[str] | None = None,
) -> bool:
    """Check explicit Python structure and CLI error-handling requirements."""
    ledger = get_current_run()
    lines: list[str] = []
    all_passed = True
    python_files = [path for path in output_scope if Path(path).suffix.lower() == ".py"]
    required_functions = set(re.findall(r"`([A-Za-z_]\w*)\([^`\n]*\)`", prd_text or ""))
    if python_files and required_functions:
        found: set[str] = set()
        parse_errors: list[str] = []
        for relative in python_files:
            try:
                tree = ast.parse((workspace / relative).read_text(encoding="utf-8"))
                found.update(
                    node.name
                    for node in ast.walk(tree)
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
            except (OSError, SyntaxError) as exc:
                parse_errors.append(f"{relative}: {exc}")
        missing = sorted(required_functions - found)
        passed = not missing and not parse_errors
        all_passed = all_passed and passed
        detail = (
            "all named functions present"
            if passed
            else "; ".join(
                parse_errors + ([f"missing functions: {', '.join(missing)}"] if missing else [])
            )
        )
        command = "prd:function-contract"
        lines.append(f"{'PASS' if passed else 'FAIL'}  named Python functions\n{detail}")
        if ledger:
            verifier_id = ledger.verifier_id_for(command, "prd_conformance")
            ledger.log_verification_started(
                command,
                verifier_id=verifier_id,
                source="prd_conformance",
                required=True,
            )
            ledger.log_verification_result(
                passed,
                detail,
                command=command,
                verifier_id=verifier_id,
                source="prd_conformance",
                required=True,
                detail=detail,
            )
        if not passed and failure_details is not None:
            failure_details.append(f"PRD function contract failed: {detail}")

    lower_prd = (prd_text or "").lower()
    needs_argument_probes = (
        "usage" in lower_prd and "missing" in lower_prd and "invalid" in lower_prd
    )
    script_parts: list[str] = []
    if needs_argument_probes:
        for command, _expected in acceptance:
            try:
                parts = shlex.split(command, posix=os.name != "nt")
            except ValueError:
                continue
            if len(parts) >= 2 and re.match(r"^(?:python3?|py)$", parts[0], re.IGNORECASE):
                script_parts = parts[:2]
                break
    if script_parts:
        probes = [
            ("missing arguments", script_parts, ("usage",)),
            ("invalid arguments", script_parts + ["__shamsu_invalid__", "0"], ("usage", "invalid")),
        ]
        registry = AgentToolRegistry(
            workspace,
            session_logger=session_logger,
            approval_manager=_make_approval_manager(
                workspace, session_logger, console, lambda _request: True
            ),
            action_ledger=ledger,
        )
        for label, parts, expected_words in probes:
            command = subprocess.list2cmdline(parts)
            if ledger:
                verifier_id = ledger.verifier_id_for(command, "prd_conformance")
                ledger.log_verification_started(
                    command,
                    verifier_id=verifier_id,
                    source="prd_conformance",
                    required=True,
                )
                call_id = ledger.log_tool_call("run_command", {"command": command})
            else:
                verifier_id = ""
                call_id = ""
            result = registry.execute("run_command", {"command": command})
            stdout = str(result.data.get("stdout") or "")
            stderr = str(result.data.get("stderr") or "")
            combined = (stdout + "\n" + stderr).strip()
            lowered = combined.lower()
            passed = any(word in lowered for word in expected_words) and "traceback" not in lowered
            all_passed = all_passed and passed
            if ledger:
                ledger.log_tool_result(call_id, "run_command", passed, result.message, result.data)
                ledger.log_verification_result(
                    passed,
                    combined,
                    command=command,
                    verifier_id=verifier_id,
                    source="prd_conformance",
                    required=True,
                    detail=combined,
                )
            detail = combined or result.message
            lines.append(f"{'PASS' if passed else 'FAIL'}  {label}: {command}\n{detail}")
            if not passed and failure_details is not None:
                failure_details.append(
                    f"PRD {label} check failed. Command: {command}\nActual result:\n{detail}"
                )

    summary = ("PRD conformance passed.\n" if all_passed else "PRD conformance failed.\n") + (
        "\n\n".join(lines) if lines else "No supplemental checks were inferred."
    )
    console.print(
        Panel(
            summary,
            title="PRD Conformance",
            border_style="green" if all_passed else "red",
        )
    )
    if summary_out is not None:
        summary_out.append(summary)
    return all_passed


def _prd_brief(parsed: Any) -> str:
    """Render only cross-cutting context; preflight carries exact milestone requirements."""
    try:
        contract_obj = extract_contract(parsed)
        lines = [
            f"Product: {contract_obj.title}",
            f"Kind: {contract_obj.project_kind or 'application'}",
        ]
        if contract_obj.product_summary:
            lines.append(f"Summary: {contract_obj.product_summary}")
        if contract_obj.required_stack:
            lines.append("Required stack: " + ", ".join(contract_obj.required_stack))
        if contract_obj.architecture:
            lines.append("Architecture: " + ", ".join(contract_obj.architecture))
        if contract_obj.roles:
            lines.append("Roles: " + ", ".join(contract_obj.roles[:8]))
        if contract_obj.authorization_rules:
            lines.append("Global authorization rules:")
            lines.extend(f"- {item}" for item in contract_obj.authorization_rules[:8])
        if contract_obj.assumptions:
            lines.append("Confirmed assumptions:")
            lines.extend(f"- {item}" for item in contract_obj.assumptions[:5])
        return "\n".join(lines)
    except Exception:
        pass
    return _render_sections(parsed)


# Which file in a conventional web project carries each kind of requirement.
# Requirement kinds that change no source (acceptance, out_of_scope) are absent
# on purpose - they are judged by the verifier, not implemented.
_REQUIREMENT_FILE_ROLES: dict[str, str] = {
    "entity": "models",
    "persistence": "models",
    "role": "models",
    "feature": "views",
    "mechanic": "views",
    "workflow": "views",
    "screen": "views",
    "auth": "views",
    "authorization": "views",
    "security": "views",
    "api": "urls",
}

# Dependency order: models before the views that query them, views before the
# routes that name them.
_FILE_ROLE_ORDER = ("models", "views", "urls")

# A milestone is split into at most this many focused turns. Beyond it the
# milestone is too broad to be worth decomposing turn-by-turn.
_MAX_BEHAVIOURAL_TARGETS = 4


def _prd_app_package(project_root: str, workspace: Path) -> str:
    """The Django app package inside *project_root*, or "" when there isn't one."""
    root = (workspace / project_root) if project_root else workspace
    try:
        children = sorted(item for item in root.iterdir() if item.is_dir())
    except OSError:
        return ""
    for child in children:
        if child.name.startswith((".", "__")):
            continue
        if (child / "models.py").exists() or (child / "apps.py").exists():
            return child.name
    return ""


def _prd_behavioural_file_groups(
    preflight: dict[str, Any], project_root: str, workspace: Path
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Group a milestone's behavioural requirements by the file that carries each.

    Non-scaffolding milestones had no decomposition at all: every requirement
    went into ONE turn, and a 7B asked to implement four requirements across
    three files in a single response truncates. Declared architecture files are
    already handled one at a time by ``_run_prd_expected_file_passes``; this is
    the same treatment for work that names no file of its own.

    Returns [(relative_path, requirements)] in dependency order, or [] when the
    project has no recognisable app layout (in which case the caller keeps the
    existing single-turn behaviour).
    """
    package = _prd_app_package(project_root, workspace)
    if not package:
        return []
    prefix = f"{project_root}/" if project_root else ""
    role_paths = {
        "models": f"{prefix}{package}/models.py",
        "views": f"{prefix}{package}/views.py",
        "urls": f"{prefix}{package}/urls.py",
    }

    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in preflight.get("requirements") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("scope") or "in") != "in":
            continue
        role = _REQUIREMENT_FILE_ROLES.get(str(item.get("kind") or "").lower())
        if not role:
            continue
        grouped.setdefault(role, []).append(item)

    ordered: list[tuple[str, list[dict[str, Any]]]] = []
    for role in _FILE_ROLE_ORDER:
        if grouped.get(role):
            ordered.append((role_paths[role], grouped[role]))
    return ordered[:_MAX_BEHAVIOURAL_TARGETS]


async def _run_prd_behavioural_file_passes(
    *,
    title: str,
    relative_path: str,
    prd_brief: str,
    milestone: str,
    preflight: dict[str, Any],
    project_root: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    max_passes: int | None = None,
    remaining: list[str] | None = None,
    contracts_by_target: dict[str, TaskContract] | None = None,
) -> list[str]:
    """Implement a milestone's behavioural requirements one file per turn.

    ``max_passes`` bounds how many of those turns run before handing control
    back; untouched targets are reported through ``remaining`` so the caller can
    stop, say what is next, and wait. With autonomy off that bound is 1, which
    is what makes one approval mean one file rather than one milestone.
    """
    groups = _prd_behavioural_file_groups(preflight, project_root, workspace)
    if not groups:
        return []
    if max_passes is not None and max_passes > 0 and len(groups) > max_passes:
        if remaining is not None:
            remaining.extend(target for target, _requirements in groups[max_passes:])
        groups = groups[:max_passes]

    skill_context = _prd_milestone_skill_context(workspace, preflight)
    allowed = set(preflight.get("allowed_tools") or [])
    tools = tuple(
        name
        for name in (
            "read_file",
            "file_info",
            "write_file",
            "edit_file",
            "append_file",
            "run_command",
        )
        if name in allowed
    )
    changed: list[str] = []

    for target, requirements in groups:
        exists = (workspace / target).is_file()
        requirement_lines = "\n".join(
            f"- {item.get('id', '')} [{item.get('kind', '')}]: {item.get('text', '')}"
            for item in requirements
        )
        console.print(f"[dim]     · {target} ({len(requirements)} requirement(s))[/dim]")
        # Withhold the patch tools on a file small enough to re-emit whole.
        turn_tools = edit_tools_for_target(target, workspace, tools)
        mutation_instruction = (
            "Read it first, then use edit_file or append_file to add what is missing. "
            "Preserve everything already there"
            if exists
            else "Call write_file with the COMPLETE file content"
        )
        result = await _run_agent_chat(
            f"""Implement one file for {title}.

Authoritative PRD: {relative_path}
Current milestone: {milestone}
Only mutation target: {target}
Project root: {project_root}

Relevant product context:
{prd_brief}

Implement ONLY these requirements, and only the part of them that belongs in {target}:
{requirement_lines}

{mutation_instruction} for exactly `{target}`. Other files in this milestone are
handled by their own separate turns - do not create or modify them, do not list
them, and do not ask which file comes next. Do not answer with a code fence or a
description instead of a tool call.

{skill_context}""",
            workspace,
            console,
            session_logger=session_logger,
            allowed_write_paths=[target],
            allowed_tools=turn_tools,
            required_tool_prefix="write_file" if "edit_file" not in turn_tools else "",
            force_long_running=is_long_running_enabled(workspace),
            auto_approve=True,
            use_long_term_memory=False,
            use_planner=False,
            user_request=_prd_agent_safety_request(project_root),
            hydrate_history=False,
            verify_changes=True,
            task_contract=(contracts_by_target or {}).get(target),
        )
        for path in getattr(result, "changed_files", ()) or ():
            if path not in changed:
                changed.append(str(path))

    return changed


async def _run_prd_expected_file_passes(
    *,
    title: str,
    relative_path: str,
    prd_brief: str,
    milestone: str,
    preflight: dict[str, Any],
    project_root: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    max_passes: int | None = None,
    remaining: list[str] | None = None,
    contracts_by_target: dict[str, TaskContract] | None = None,
) -> list[str]:
    """Materialize required architecture files through small, focused ReAct turns.

    A local 7B model is much more reliable when each turn has one mutation
    target. Valid files are retained, then the normal milestone pass integrates
    and verifies them as a unit.

    ``max_passes`` bounds the turns per approval; anything not reached is
    reported through ``remaining`` so the caller can stop and ask.
    """
    if not _prd_milestone_is_mandatory(preflight):
        return []
    expected = _preflight_expected_files(preflight)
    if not expected:
        return []
    missing = _missing_expected_files(expected, workspace)
    invalid = [
        value.split(" (", 1)[0]
        for value in _invalid_expected_architecture_files(preflight, workspace)
    ]
    targets = list(dict.fromkeys([*missing, *invalid]))
    if not targets:
        return []
    if max_passes is not None and max_passes > 0 and len(targets) > max_passes:
        if remaining is not None:
            remaining.extend(targets[max_passes:])
        targets = targets[:max_passes]

    requirements = "\n".join(
        f"- {item.get('id', '')} [{item.get('kind', '')}]: {item.get('text', '')}"
        for item in preflight.get("requirements") or []
        if isinstance(item, dict)
    )
    existing_file_tools = tuple(
        name
        for name in (
            "read_file",
            "file_info",
            "find_file",
            "write_file",
            "edit_file",
            "append_file",
            "run_command",
        )
        if name in set(preflight.get("allowed_tools") or [])
    )
    skill_context = _prd_milestone_skill_context(workspace, preflight)
    attempts = _env_int_at_least("SHAMSU_PRD_FILE_PASS_ATTEMPTS", 2, 1)
    changed: list[str] = []

    for target in targets:
        rejection_feedback = ""
        # Framework scaffolding is generated by substitution, never by the
        # model. Six consecutive live 7B runs wrote a settings.py that used
        # BASE_DIR without defining it, failing M-001 every time and blocking
        # all 23 milestones; templates/django/constants.py already said these
        # files belong out of the LLM path.
        if _write_prd_boilerplate(target, preflight, workspace, console, session_logger):
            if target not in changed:
                changed.append(target)
            continue
        for attempt in range(1, attempts + 1):
            target_is_missing = bool(_missing_expected_files([target], workspace))
            current_invalid = _invalid_expected_architecture_files(preflight, workspace)
            target_invalid = [
                value for value in current_invalid if value.split(" (", 1)[0] == target
            ]
            if not target_is_missing and not target_invalid:
                break
            console.print(
                f"[dim]     file pass {attempt}/{attempts}: {target}[/dim]"
            )
            action_instruction = (
                "The harness has confirmed that the target is missing. Your FIRST response must "
                "call write_file for the exact target; do not read, search, or list files first."
                if target_is_missing
                else (
                    "The target exists but failed deterministic validation. Read it, then repair "
                    "the exact problem: " + "; ".join(target_invalid)
                )
            )
            repair_guidance = _prd_file_repair_guidance(
                target, preflight, workspace=workspace
            )
            append_missing_entities = _prd_requires_entity_declaration_append(target_invalid)
            deterministic_edit = "Deterministic edit recipe:" in repair_guidance
            if append_missing_entities:
                action_instruction += (
                    " The named entity classes are entirely absent. Do not edit any existing "
                    "class or field. The harness already inspected the current file, so do not read "
                    "or run commands. Your FIRST response MUST call append_file for the exact target "
                    "with complete Django model declarations for every missing entity and every "
                    "listed field."
                )
            rewrite_required = target_is_missing or any(
                token in " ".join(target_invalid).lower()
                for token in (
                    "empty",
                    "invalid python",
                    "incomplete",
                    "no executable declarations",
                    "no persisted model declarations",
                )
            )
            active_tools = (
                ("write_file",)
                if target_is_missing
                else tuple(
                    name
                    for name in existing_file_tools
                    if name == "append_file"
                )
                if append_missing_entities
                else tuple(
                    name
                    for name in existing_file_tools
                    if rewrite_required or name != "write_file"
                )
            )
            mutation_instruction = (
                "Call write_file with the COMPLETE production-ready content"
                if rewrite_required
                else "Call append_file with complete declarations for the missing entities"
                if append_missing_entities
                else (
                    "Preserve all unrelated content. Use edit_file for exact replacements or "
                    "append_file for a missing trailing declaration; do not rewrite the whole file"
                )
            )
            before_errors = _prd_target_validation_errors(target, current_invalid)
            transaction_baseline = _prd_transaction_snapshot(workspace)
            result = await _run_agent_chat(
                f"""Implement exactly one required file for {title}.

Authoritative PRD: {relative_path}
Current milestone: {milestone}
Only mutation target: {target}
Project root: {project_root}

Relevant product context:
{prd_brief}

Binding requirements for this milestone:
{requirements or '- Complete the named milestone contract.'}

{action_instruction}
{rejection_feedback}
{repair_guidance}
Use the ReAct tool loop. {mutation_instruction} for exactly `{target}`. Do not
merely show a code fence, describe the code, list
other files, or ask which file comes next. For an __init__.py package marker,
empty content is valid but the write_file call must still be made. After a
successful mutation, run only a focused syntax check when one is applicable.
Do not modify any other file.

{skill_context}""",
                workspace,
                console,
                session_logger=session_logger,
                force_long_running=is_long_running_enabled(workspace),
                auto_approve=True,
                allowed_write_paths=(target,),
                allowed_read_paths=(project_root,),
                allowed_tools=active_tools,
                use_long_term_memory=False,
                use_planner=False,
                user_request=_prd_agent_safety_request(project_root),
                required_tool_prefix=(
                    "append_file"
                    if append_missing_entities
                    else "edit_file"
                    if deterministic_edit
                    else ""
                ),
                hydrate_history=False,
                verify_changes=True,
                task_contract=(contracts_by_target or {}).get(target),
            )
            after_invalid = _invalid_expected_architecture_files(preflight, workspace)
            after_errors = _prd_target_validation_errors(target, after_invalid)
            introduced_errors = sorted(after_errors - before_errors)
            no_validation_progress = bool(before_errors) and after_errors >= before_errors
            if _prd_entity_validation_progress(before_errors, after_errors):
                # A small model often gets a whole missing declaration mostly right in
                # one append, leaving one malformed field or import. Keep that bounded
                # progress for the next focused pass; final architecture validation still
                # prevents the milestone from verifying until every issue is gone.
                introduced_errors = [
                    error
                    for error in introduced_errors
                    if _prd_fatal_file_regression(error)
                ]
                no_validation_progress = False
            if introduced_errors or no_validation_progress:
                if target_is_missing and not any(
                    _prd_fatal_file_regression(error) for error in introduced_errors
                ):
                    # The file did not exist before this attempt, so a rollback
                    # deletes the ONLY copy and leaves the target missing -
                    # strictly worse than an incomplete file, because the
                    # milestone then fails on "missing expected architecture
                    # files" instead of the real defect and burns the repair
                    # budget on the wrong problem (observed live 2026-08-01:
                    # models.py was rolled back to nothing twice). Keep a
                    # syntactically valid creation and let the next focused
                    # pass append what is missing; final architecture
                    # validation still blocks the milestone until it is whole.
                    ledger = get_current_run()
                    if ledger:
                        ledger.log_event(
                            "prd_file_pass_incomplete_creation_kept",
                            target=target,
                            introduced_errors=introduced_errors,
                        )
                    rejection_feedback = _prd_file_pass_rejection_feedback(
                        introduced_errors, sorted(before_errors)
                    )
                    for path in getattr(result, "changed_files", ()) or ():
                        if path not in changed:
                            changed.append(path)
                    continue
                transaction_ids = _prd_transactions_since(workspace, transaction_baseline)
                rollback_messages: list[str] = []
                for transaction_id in reversed(transaction_ids):
                    ok, message = rollback_transaction(workspace, transaction_id)
                    rollback_messages.append(message)
                    ledger = get_current_run()
                    if ledger:
                        ledger.log_rollback(transaction_id, ok, message)
                ledger = get_current_run()
                if ledger:
                    ledger.log_event(
                        "prd_file_pass_regression_rolled_back",
                        target=target,
                        introduced_errors=introduced_errors,
                        no_validation_progress=no_validation_progress,
                        transaction_ids=transaction_ids,
                        messages=rollback_messages,
                    )
                # Tell the NEXT attempt what was wrong. Without this the retry
                # prompt was byte-identical after a rollback, and the 7B model
                # deterministically re-emitted the same placeholder scaffold
                # every time (observed live 2026-08-01).
                rejection_feedback = _prd_file_pass_rejection_feedback(
                    introduced_errors, sorted(before_errors)
                )
                continue
            for path in getattr(result, "changed_files", ()) or ():
                if path not in changed:
                    changed.append(path)
    return changed


def _write_prd_boilerplate(
    target: str,
    preflight: dict[str, Any],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
) -> bool:
    """Generate a Django scaffolding file deterministically. True when written.

    Returns False for anything carrying product logic (models.py, views.py),
    which stays with the model - that is what it is good at, and it produced
    correct entity models in the same runs where it broke settings.py.
    """
    from shamsu.prd.boilerplate import render_boilerplate

    expected = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    relative = _workspace_relative_paths([target], workspace)
    if not relative:
        return False
    content = render_boilerplate(
        relative[0],
        expected,
        custom_user_model=any(
            str(entity).lower() == "user"
            for entity, _fields in _prd_required_entity_fields(preflight)
        ),
    )
    if content is None:
        return False
    ledger = get_current_run()
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(
            workspace, session_logger, console, lambda _request: True
        ),
        action_ledger=ledger,
    )
    call_id = ledger.log_tool_call("write_file", {"filepath": relative[0]}) if ledger else ""
    result = registry.execute("write_file", {"filepath": relative[0], "content": content})
    if ledger:
        ledger.log_tool_result(call_id, "write_file", bool(result.ok), result.message, result.data)
        ledger.log_event(
            "prd_boilerplate_generated",
            target=relative[0],
            ok=bool(result.ok),
            message=str(result.message)[:200],
        )
    if result.ok:
        console.print(f"[dim]     boilerplate (deterministic): {relative[0]}[/dim]")
    return bool(result.ok)


def _prd_file_pass_rejection_feedback(
    introduced_errors: list[str], unresolved_errors: list[str]
) -> str:
    """One paragraph telling the retry exactly why the last write was undone."""
    reasons = introduced_errors or unresolved_errors
    detail = "; ".join(reasons[:6]) if reasons else "it did not satisfy the contract"
    return (
        "PREVIOUS ATTEMPT REJECTED AND ROLLED BACK. The last write failed validation: "
        f"{detail}. Do NOT write a placeholder, stub, or scaffold comment such as "
        '"# Define your models here." - write the COMPLETE working implementation '
        "with the required imports and behavior this time."
    )


def _prd_target_validation_errors(target: str, invalid: list[str]) -> set[str]:
    errors: set[str] = set()
    for value in invalid:
        path, separator, detail = value.partition(" (")
        if path != target or not separator:
            continue
        for item in detail.rsplit(")", 1)[0].split(";"):
            item = item.strip()
            if not item:
                continue
            marker = "missing required entities or fields:"
            if item.lower().startswith(marker):
                values = item.split(":", 1)[1]
                errors.update(
                    f"missing entity contract:{value.strip()}"
                    for value in values.split(",")
                    if value.strip()
                )
                continue
            errors.add(item)
    return errors


_ENTITY_CONTRACT_PREFIX = "missing entity contract:"


def _prd_missing_entity_atoms(errors: set[str]) -> set[str]:
    """Expand grouped entity contracts into one item per missing field.

    The validator renders every field a single entity is missing as ONE
    slash-joined value ("User.name/role"). Counting or subtracting those
    strings makes fixing one of two fields look like a brand-new error, since
    "User.name/role" and "User.name" simply differ. Observed live 2026-08-02:
    a repair that correctly added `role` was rolled back as a regression.
    """
    atoms: set[str] = set()
    for error in errors:
        if not error.startswith(_ENTITY_CONTRACT_PREFIX):
            continue
        value = error[len(_ENTITY_CONTRACT_PREFIX) :].strip()
        entity, _dot, fields = value.partition(".")
        entity = entity.strip()
        if not entity:
            continue
        if not fields.strip():
            atoms.add(entity)
            continue
        atoms.update(
            f"{entity}.{field.strip()}" for field in fields.split("/") if field.strip()
        )
    return atoms


def _prd_entity_validation_progress(before: set[str], after: set[str]) -> bool:
    """Return true when a bounded pass reduced unresolved entity contracts."""
    before_atoms = _prd_missing_entity_atoms(before)
    after_atoms = _prd_missing_entity_atoms(after)
    return bool(before_atoms) and len(after_atoms) < len(before_atoms)


def _prd_repair_regressions(
    invalid_before: set[str], invalid_after: set[str]
) -> list[str]:
    """Architecture entries a repair genuinely made worse.

    Compares parsed per-file contents rather than raw strings, so partially
    satisfying an entity contract counts as progress and is kept.
    """
    before_list = sorted(invalid_before)
    after_list = sorted(invalid_after)
    targets = {
        value.split(" (", 1)[0] for value in set(invalid_before) | set(invalid_after)
    }
    regressions: list[str] = []
    for target in sorted(targets):
        before = _prd_target_validation_errors(target, before_list)
        after = _prd_target_validation_errors(target, after_list)
        new_other = {
            item
            for item in after - before
            if not item.startswith(_ENTITY_CONTRACT_PREFIX)
        }
        new_atoms = _prd_missing_entity_atoms(after) - _prd_missing_entity_atoms(before)
        if not new_other and not new_atoms:
            continue
        detail = sorted(new_other | {f"missing {atom}" for atom in new_atoms})
        regressions.append(f"{target} ({'; '.join(detail)})")
    return regressions


def _prd_fatal_file_regression(error: str) -> bool:
    lowered = error.lower()
    return any(
        marker in lowered
        for marker in (
            "invalid python",
            "empty",
            "no executable declarations",
            "no persisted model declarations",
        )
    )


def _prd_requires_entity_declaration_append(target_invalid: list[str]) -> bool:
    """Whether validation says whole entity classes, rather than fields, are absent."""
    details = " ".join(target_invalid)
    marker = "missing required entities or fields:"
    if marker not in details:
        return False
    missing = details.split(marker, 1)[1].rsplit(")", 1)[0]
    values = [value.strip() for value in missing.split(",") if value.strip()]
    return bool(values) and all("." not in value for value in values)


_RELATION_FIELDS = {"ForeignKey", "OneToOneField", "ManyToManyField"}


def _django_dangling_relation_errors(module: ast.Module, defined: set[str]) -> list[str]:
    """Relation fields whose quoted target model does not exist in this file.

    A 7B routinely switches from a direct reference to a STRING one mid-file and
    invents a role-shaped name: live 2026-08-02 it wrote
    `ForeignKey('Teacher')` and `ForeignKey('Student')` while defining neither,
    having already given `User` a `role` field. Django only reports that at
    `manage.py check` time (fields.E300/E307), by which point the milestone has
    failed. Catch it at write time instead.

    Only bare quoted names are checked: `'self'`, `'app.Model'`, and settings
    references like AUTH_USER_MODEL are all legitimate and left alone.
    """
    errors: list[str] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        name = (
            target.attr if isinstance(target, ast.Attribute)
            else target.id if isinstance(target, ast.Name)
            else ""
        )
        if name not in _RELATION_FIELDS or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        reference = first.value.strip()
        if not reference or reference.lower() == "self" or "." in reference:
            continue
        if reference in defined:
            continue
        errors.append(
            f"{name} references undefined model '{reference}' "
            f"(defined here: {', '.join(sorted(defined)) or 'none'})"
        )
    return sorted(dict.fromkeys(errors))


def _django_model_structure_errors(content: str) -> list[str]:
    """Find valid-Python model declarations that Django cannot use correctly."""
    try:
        module = ast.parse(content)
    except SyntaxError:
        return []
    classes = {
        node.name: node for node in module.body if isinstance(node, ast.ClassDef)
    }
    errors: list[str] = []
    for node in module.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            declared = classes.get(alias.asname or alias.name)
            if declared is not None and node.lineno > declared.lineno:
                errors.append(f"local model class shadowed by later import: {declared.name}")
    settings_used = any(
        isinstance(node, ast.Name) and node.id == "settings" and isinstance(node.ctx, ast.Load)
        for node in ast.walk(module)
    )
    settings_imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "django.conf"
        and any((alias.asname or alias.name) == "settings" for alias in node.names)
        for node in module.body
    )
    if settings_used and not settings_imported:
        errors.append("Django settings is referenced but not imported")
    errors.extend(_django_dangling_relation_errors(module, set(classes)))
    for class_node in classes.values():
        for statement in class_node.body:
            if not isinstance(statement, ast.AnnAssign) or statement.value is not None:
                continue
            annotation = statement.annotation
            if not isinstance(annotation, ast.Call):
                continue
            function = annotation.func
            if not (
                isinstance(function, ast.Attribute)
                and isinstance(function.value, ast.Name)
                and function.value.id == "models"
            ):
                continue
            if isinstance(statement.target, ast.Name):
                errors.append(
                    "Django field uses annotation instead of assignment: "
                    f"{class_node.name}.{statement.target.id}"
                )
    return list(dict.fromkeys(errors))


def _django_model_structure_edit_recipes(
    target: str,
    content: str,
) -> list[dict[str, Any]]:
    """Build exact, source-derived edits for common small-model Django mistakes."""
    try:
        module = ast.parse(content)
    except SyntaxError:
        return []
    lines = content.splitlines(keepends=True)
    classes = {
        node.name: node for node in module.body if isinstance(node, ast.ClassDef)
    }
    recipes: list[dict[str, Any]] = []

    for node in module.body:
        if not isinstance(node, ast.ImportFrom):
            continue
        shadowed = [
            alias
            for alias in node.names
            if (alias.asname or alias.name) in classes
            and node.lineno > classes[alias.asname or alias.name].lineno
        ]
        if not shadowed:
            continue
        start = node.lineno - 1
        end = int(getattr(node, "end_lineno", node.lineno))
        old_string = "".join(lines[start:end])
        kept = [alias for alias in node.names if alias not in shadowed]
        if kept:
            rendered = ", ".join(
                alias.name + (f" as {alias.asname}" if alias.asname else "")
                for alias in kept
            )
            ending = "\n" if old_string.endswith("\n") else ""
            new_string = f"from {node.module} import {rendered}{ending}"
        else:
            new_string = ""
        recipes.append(
            {
                "name": "edit_file",
                "arguments": {
                    "filepath": target,
                    "old_string": old_string,
                    "new_string": new_string,
                },
            }
        )

    settings_used = any(
        isinstance(node, ast.Name) and node.id == "settings" and isinstance(node.ctx, ast.Load)
        for node in ast.walk(module)
    )
    settings_imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "django.conf"
        and any((alias.asname or alias.name) == "settings" for alias in node.names)
        for node in module.body
    )
    if settings_used and not settings_imported:
        first_import = next(
            (node for node in module.body if isinstance(node, (ast.Import, ast.ImportFrom))),
            None,
        )
        if first_import is not None:
            end = int(getattr(first_import, "end_lineno", first_import.lineno))
            old_string = "".join(lines[:end])
            prefix = "".join(lines[: first_import.lineno - 1])
            import_text = "".join(lines[first_import.lineno - 1 : end])
            recipes.append(
                {
                    "name": "edit_file",
                    "arguments": {
                        "filepath": target,
                        "old_string": old_string,
                        "new_string": prefix + "from django.conf import settings\n" + import_text,
                    },
                }
            )

    for class_node in classes.values():
        for statement in class_node.body:
            if not (
                isinstance(statement, ast.AnnAssign)
                and statement.value is None
                and isinstance(statement.target, ast.Name)
                and isinstance(statement.annotation, ast.Call)
                and isinstance(statement.annotation.func, ast.Attribute)
                and isinstance(statement.annotation.func.value, ast.Name)
                and statement.annotation.func.value.id == "models"
            ):
                continue
            start = statement.lineno - 1
            end = int(getattr(statement, "end_lineno", statement.lineno))
            context_start = class_node.lineno - 1
            annotation = ast.get_source_segment(content, statement.annotation)
            if not annotation:
                continue
            indent = " " * int(getattr(statement, "col_offset", 0))
            ending = "\n" if "".join(lines[start:end]).endswith("\n") else ""
            replacement = f"{indent}{statement.target.id} = {annotation}{ending}"
            recipes.append(
                {
                    "name": "edit_file",
                    "arguments": {
                        "filepath": target,
                        "old_string": "".join(lines[context_start:end]),
                        "new_string": "".join(lines[context_start:start]) + replacement,
                    },
                }
            )
    return recipes


def _prd_file_repair_guidance(
    target: str,
    preflight: dict[str, Any],
    *,
    workspace: Path | None = None,
) -> str:
    """Derive exact cross-file repair facts the model should not have to guess."""
    posix = target.lower().replace("\\", "/")
    expected = _preflight_expected_files(preflight)
    app_init_paths = {
        (Path(path).parent / "__init__.py").as_posix()
        for path in expected
        if path.lower().replace("\\", "/").endswith("/apps.py")
    }
    if Path(target).as_posix() in app_init_paths and workspace is not None:
        try:
            content = (workspace / target).read_text(encoding="utf-8")
        except OSError:
            content = ""
        import_match = re.search(
            r"(?m)^\s*(?:from\s+\.models\s+import|from\s+\.\s+import\s+models|import\s+.*\bmodels\b)[^\r\n]*(?:\r?\n)?",
            content,
        )
        if import_match:
            recipe = {
                "name": "edit_file",
                "arguments": {
                    "filepath": target,
                    "old_string": import_match.group(0),
                    "new_string": "",
                },
            }
            return (
                "Django app package markers must not import models during app registry loading. "
                "An empty __init__.py is valid.\nDeterministic edit recipe: your next response "
                "must call this tool without changing its arguments:\n" + json.dumps(recipe)
            )
    if posix.endswith("/urls.py") and workspace is not None:
        try:
            content = (workspace / target).read_text(encoding="utf-8")
        except OSError:
            content = ""
        django_root = (workspace / target).parent.parent
        recipes: list[dict[str, Any]] = []
        missing_modules: list[str] = []
        for match in re.finditer(
            r"(?m)^.*include\(\s*['\"](?P<module>[^'\"]+)['\"]\s*\).*?(?:\r?\n|$)",
            content,
        ):
            included = match.group("module")
            module_base = django_root.joinpath(*included.split("."))
            if module_base.with_suffix(".py").is_file() or (module_base / "__init__.py").is_file():
                continue
            missing_modules.append(included)
            recipes.append(
                {
                    "name": "edit_file",
                    "arguments": {
                        "filepath": target,
                        "old_string": match.group(0),
                        "new_string": "",
                    },
                }
            )
        if recipes:
            return (
                "The URL configuration includes module(s) that do not exist: "
                + ", ".join(missing_modules)
                + ". Remove those route entries until the owning milestone creates the modules."
                + "\nDeterministic edit recipe: your next response must call every tool below "
                "in order without changing its arguments:\n"
                + "\n".join(json.dumps(item) for item in recipes)
            )
    if posix.endswith("/models.py") and workspace is not None:
        try:
            content = (workspace / target).read_text(encoding="utf-8")
        except OSError:
            content = ""
        structure_errors = _django_model_structure_errors(content)
        recipes = _django_model_structure_edit_recipes(target, content)
        facts: list[str] = []
        if structure_errors:
            facts.append("Django model structure errors: " + "; ".join(structure_errors) + ".")
        if recipes:
            facts.append(
                "Deterministic edit recipe: your next responses must call every tool below "
                "in order without changing its arguments:\n"
                + "\n".join(json.dumps(item) for item in recipes)
            )
        if facts:
            return "\n".join(facts)
    if posix.endswith("/settings.py"):
        url_modules = sorted(
            {
                ".".join(Path(path).with_suffix("").parts[-2:])
                for path in _preflight_expected_files(preflight)
                if path.lower().replace("\\", "/").endswith("/urls.py")
            }
        )
        app_modules = sorted(
            {
                Path(path).parent.name
                for path in _preflight_expected_files(preflight)
                if path.lower().replace("\\", "/").endswith("/apps.py")
            }
        )
        facts: list[str] = []
        custom_user_models = (
            _django_custom_user_models(preflight, workspace) if workspace is not None else []
        )
        if url_modules:
            facts.append(f"ROOT_URLCONF must be `{url_modules[0]}`")
        if app_modules:
            facts.append("INSTALLED_APPS must include " + ", ".join(app_modules))
        if custom_user_models:
            facts.append(f"AUTH_USER_MODEL must be `{custom_user_models[0]}`")
        has_wsgi_file = any(
            path.lower().replace("\\", "/").endswith("/wsgi.py")
            for path in _preflight_expected_files(preflight)
        )
        facts.append(
            "WSGI_APPLICATION must reference the expected wsgi.py module"
            if has_wsgi_file
            else (
                "no wsgi.py exists in this architecture, so remove the WSGI_APPLICATION "
                "assignment entirely"
            )
        )
        facts.append(
            "the complete file must retain SECRET_KEY, DEBUG, ALLOWED_HOSTS, INSTALLED_APPS, "
            "MIDDLEWARE, ROOT_URLCONF, TEMPLATES, DATABASES, locale/time-zone settings, and "
            "STATIC_URL"
        )
        recipes: list[dict[str, Any]] = []
        if workspace is not None:
            try:
                content = (workspace / target).read_text(encoding="utf-8")
            except OSError:
                content = ""
            root_match = re.search(
                r"(?m)^ROOT_URLCONF\s*=\s*(?P<quote>['\"])(?P<module>[^'\"]+)(?P=quote)[^\r\n]*",
                content,
            )
            if (
                root_match
                and url_modules
                and root_match.group("module") != url_modules[0]
            ):
                old_string = root_match.group(0)
                recipes.append(
                    {
                        "name": "edit_file",
                        "arguments": {
                            "filepath": target,
                            "old_string": old_string,
                            "new_string": f"ROOT_URLCONF = '{url_modules[0]}'",
                        },
                    }
                )
            if not has_wsgi_file:
                wsgi_match = re.search(
                    r"(?m)^WSGI_APPLICATION[^\r\n]*(?:\r?\n)?", content
                )
                if wsgi_match:
                    recipes.append(
                        {
                            "name": "edit_file",
                            "arguments": {
                                "filepath": target,
                                "old_string": wsgi_match.group(0),
                                "new_string": "",
                            },
                        }
                    )
            if custom_user_models:
                auth_match = re.search(
                    r"(?m)^AUTH_USER_MODEL\s*=\s*(?P<quote>['\"])(?P<model>[^'\"]+)(?P=quote)[^\r\n]*",
                    content,
                )
                expected_user = custom_user_models[0]
                if auth_match and auth_match.group("model") != expected_user:
                    recipes.append(
                        {
                            "name": "edit_file",
                            "arguments": {
                                "filepath": target,
                                "old_string": auth_match.group(0),
                                "new_string": f"AUTH_USER_MODEL = '{expected_user}'",
                            },
                        }
                    )
                elif not auth_match:
                    recipes.append(
                        {
                            "name": "append_file",
                            "arguments": {
                                "filepath": target,
                                "content": f"\nAUTH_USER_MODEL = '{expected_user}'\n",
                            },
                        }
                    )
        recipe = ""
        if recipes:
            recipe = (
                "\nDeterministic edit recipe: your next response must call every tool below "
                "in order without changing its arguments:\n"
                + "\n".join(json.dumps(item) for item in recipes)
            )
        return (
            "Cross-file facts from the architecture contract: "
            + "; ".join(facts)
            + "."
            + recipe
        )
    if not posix.endswith("/manage.py"):
        return ""
    settings_modules = sorted(
        {
            ".".join(Path(path).with_suffix("").parts[-2:])
            for path in _preflight_expected_files(preflight)
            if path.lower().replace("\\", "/").endswith("/settings.py")
        }
    )
    if not settings_modules:
        return ""
    module = settings_modules[0]
    guidance = (
        f"Cross-file fact from the architecture contract: settings.py is in the `{module}` "
        f"module. The manage.py DJANGO_SETTINGS_MODULE value must be exactly `{module}`; "
        "any other module name is invalid."
    )
    if workspace is not None:
        try:
            content = (workspace / target).read_text(encoding="utf-8")
        except OSError:
            content = ""
        match = re.search(
            r"(?m)^(?P<prefix>\s*os\.environ\.setdefault\(\s*['\"]DJANGO_SETTINGS_MODULE['\"]\s*,\s*)"
            r"(?P<quote>['\"])(?P<module>[^'\"]+)(?P=quote)(?P<suffix>\s*\).*)$",
            content,
        )
        if match and match.group("module") != module:
            old_string = match.group(0)
            new_string = (
                match.group("prefix")
                + match.group("quote")
                + module
                + match.group("quote")
                + match.group("suffix")
            )
            guidance += (
                "\nDeterministic edit recipe: your next response must call this exact tool "
                "without changing its arguments: "
                + json.dumps(
                    {
                        "name": "edit_file",
                        "arguments": {
                            "filepath": target,
                            "old_string": old_string,
                            "new_string": new_string,
                        },
                    }
                )
            )
    return guidance


def _build_prd_milestone_request(
    title: str,
    relative_path: Path,
    prd_brief: str,
    milestones: list[str],
    milestone_index: int,
    milestone_count: int,
    preflight: dict[str, Any] | None = None,
    project_root: str = "",
    skill_context: str = "",
) -> str:
    checklist = _render_prd_milestone_window(milestones, milestone_index - 1)
    preflight_context = render_preflight_context(preflight or {})
    preflight_block = f"\n\n{preflight_context}" if preflight_context else ""
    skills_block = f"\n\n{skill_context}" if skill_context else ""
    root_line = f"Project root: {project_root}\n" if project_root else ""
    return (
        f"{PRD_BUILD_FRAMING}\n\n"
        f"Project: {title}\n"
        f"{root_line}"
        f"PRD source reference: {relative_path.as_posix()} (already extracted by the harness)\n"
        f"Current milestone {milestone_index}/{milestone_count}: {milestones[milestone_index - 1]}\n\n"
        "FIRST list and read the files already in the workspace so you build ON TOP of the previous "
        "milestones instead of replacing their work. THEN implement only the current milestone by "
        "editing and extending those files. Every feature from earlier milestones must keep working, "
        "entry files must still load their scripts (no orphaned inline logic), and you must write "
        "complete runnable files. Verify with run_command when possible.\n\n"
        f"{prd_brief}\n\n"
        f"{checklist}"
        f"{preflight_block}"
        f"{skills_block}"
    )


def _render_prd_milestone_window(milestones: list[str], current_index: int) -> str:
    """Keep nearby checkpoint context without resending the whole milestone graph."""
    start = max(0, current_index - 2)
    stop = min(len(milestones), current_index + 2)
    nearby = milestones[start:stop]
    completed = max(0, current_index - start)
    return render_progress_checklist(nearby, completed, header="Nearby milestones")


def _scope_prd_preflight(preflight: dict[str, Any], project_root: str) -> dict[str, Any]:
    """Anchor milestone file contracts below the requested project directory."""
    scoped = dict(preflight)
    scoped["allowed_tools"] = list(
        dict.fromkeys(
            [
                str(name)
                for name in preflight.get("allowed_tools") or []
                if str(name) != "ask_user"
            ]
            + ["append_file", "file_info", "find_file"]
        )
    )
    if (
        len(preflight.get("expected_files") or []) > 1
        and str(preflight.get("rollback_policy") or "").strip().lower()
        == "rollback changed files on failed verifier"
    ):
        scoped["rollback_policy"] = (
            "keep valid partial changes; no rollback for an incomplete milestone"
        )
    root = Path(project_root).as_posix().strip("/")
    if not root or root == ".":
        return scoped
    expected: list[str] = []
    for value in preflight.get("expected_files") or []:
        path = Path(str(value)).as_posix().lstrip("/")
        if not path:
            continue
        expected.append(path if path == root or path.startswith(root + "/") else f"{root}/{path}")
    scoped["expected_files"] = list(dict.fromkeys(expected))
    scoped["project_root"] = root
    return scoped


def _reopen_invalid_prd_checkpoint(
    root: Path,
    state: dict[str, Any],
    project_root: str,
    workspace: Path,
    console: Console,
) -> dict[str, Any]:
    """Reopen the first completed checkpoint whose durable artifacts are invalid."""
    updated = state
    for milestone in state.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        status = str(milestone.get("status") or "pending")
        if status not in {"implemented", "verified"}:
            break
        milestone_id = str(milestone.get("id") or "")
        preflight = _scope_prd_preflight(
            load_milestone_preflight(root, milestone_id), project_root
        )
        expected = _preflight_expected_files(preflight)
        problems = [
            *_missing_expected_files(expected, workspace),
            *_invalid_expected_architecture_files(preflight, workspace),
            *_prd_requirement_evidence_errors(preflight, workspace),
        ]
        if not problems:
            continue
        reason = "Checkpoint revalidation failed: " + ", ".join(problems[:12])
        updated = checkpoint_milestone(
            root,
            updated,
            milestone_id,
            status="pending",
            evidence=["checkpoint_revalidation_failed"],
            message=reason,
        )
        console.print(
            Panel(
                f"Reopening {milestone_id}: {reason}",
                title="PRD Checkpoint Revalidation",
                border_style="yellow",
            )
        )
        break
    return updated


def _prd_fallback_preflight(project: Any, project_root: str) -> dict[str, Any]:
    contract_obj = getattr(project, "prd_contract", None)
    stack = " ".join(
        [
            str(getattr(contract_obj, "stack_hint", "") or ""),
            *[str(item) for item in getattr(contract_obj, "required_stack", ()) or ()],
        ]
    ).lower()
    skills = ["developer", "prd-planner"]
    if any(token in stack for token in ("react", "vite", "node", "typescript")):
        skills.append("react-vite")
    if getattr(contract_obj, "project_kind", "") in {"web_app", "game"}:
        skills.append("ui-designer")
    if "sqlite" in stack:
        skills.append("sqlite-persistence")
    elif any(token in stack for token in ("postgres", "postgresql", "mysql", "mariadb", "mssql", "database")):
        skills.append("sql-databases")
    skills.append("testing")
    return {
        "milestone_id": "M-FALLBACK",
        "title": "Complete PRD implementation",
        "project_root": project_root,
        "active_skills": list(dict.fromkeys(skills)),
        "requirements": [],
    }


def _prd_milestone_skill_context(workspace: Path, preflight: dict[str, Any]) -> str:
    """Render the exact compiled milestone skills, without global selection loss."""
    names = list(dict.fromkeys(str(item) for item in preflight.get("active_skills") or []))
    if not names:
        return ""
    catalog = discover_skills(workspace)
    selected: list[SelectedSkill] = []
    missing: list[str] = []
    for name in names:
        skill = catalog.skills.get(name)
        if skill is None:
            missing.append(name)
            continue
        selected.append(
            SelectedSkill(skill=skill, score=100.0, reasons=("compiled milestone contract",))
        )
    ledger = get_current_run()
    if ledger:
        ledger.log_event(
            "prd_milestone_skills_injected",
            milestone_id=str(preflight.get("milestone_id") or ""),
            selected=[item.skill.name for item in selected],
            missing=missing,
        )
    if missing:
        preflight["missing_skills"] = missing
    selection = SkillSelection(
        mode="on",
        selected=tuple(selected),
        issues=catalog.issues,
        budget_tokens=_env_int_at_least("SHAMSU_PRD_SKILL_BUDGET_TOKENS", 3600, 900),
    )
    return render_skill_context(selection)


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


def _milestone_id_from_line(line: str) -> str:
    match = re.match(r"^\s*(M-\d{3})\b", line)
    if match:
        return match.group(1)
    slug = re.sub(r"[^A-Za-z0-9]+", "-", line.strip()).strip("-").upper()
    return f"M-CUSTOM-{slug[:24] or 'MILESTONE'}"


def _prd_model_preflight_enabled() -> bool:
    raw = os.environ.get("SHAMSU_PRD_MODEL_PREFLIGHT", "0").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _prd_milestone_repair_enabled() -> bool:
    raw = os.environ.get("SHAMSU_PRD_MILESTONE_REPAIR", "1").strip().lower()
    return raw not in {"0", "false", "no", "off", "disabled"}


def _prd_milestone_rollback_enabled(preflight: dict[str, Any]) -> bool:
    raw = os.environ.get("SHAMSU_PRD_MILESTONE_ROLLBACK", "1").strip().lower()
    if raw in {"0", "false", "no", "off", "disabled"}:
        return False
    policy = str(preflight.get("rollback_policy") or "").strip().lower()
    if not policy:
        return False
    disabled_tokens = ("no rollback", "do not rollback", "do not roll back", "keep changes")
    if any(token in policy for token in disabled_tokens):
        return False
    return "rollback" in policy or "roll back" in policy


def _prd_milestone_repair_budget(preflight: dict[str, Any]) -> int:
    cap = _env_int_at_least("SHAMSU_PRD_REPAIR_MAX_ATTEMPTS", 2, 0)
    try:
        requested = int(preflight.get("attempt_budget", 2))
    except (TypeError, ValueError, AttributeError):
        requested = 2
    return min(max(0, requested), cap)


PRD_MODEL_PREFLIGHT_SYSTEM = """You are SHAMSU preparing one bounded PRD milestone.
Return ONLY JSON matching the schema.
Do not invent requirements, tools, or shell commands.
You may narrow active_skills and allowed_tools to the provided allowlists.
You may add safe relative expected files when they are clearly needed for this milestone.
Return implementation_steps as 3 to 6 ordered, file-aware actions for this milestone.
Keep notes short and operational."""


async def _prepare_prd_milestone_preflight(
    root: Path,
    state: dict[str, Any],
    milestone_id: str,
    deterministic_preflight: dict[str, Any],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not _prd_model_preflight_enabled():
        preflight = dict(deterministic_preflight)
        preflight.setdefault("preflight_source", "deterministic")
        return preflight, state

    ledger = get_current_run()
    if ledger:
        ledger.log_event("prd_model_preflight_started", milestone_id=milestone_id)
    try:
        raw = await asyncio.wait_for(
            _make_llm_manager(session_logger, console, workspace).generate_structured(
                "planner",
                PRD_MODEL_PREFLIGHT_SYSTEM,
                _build_prd_model_preflight_prompt(deterministic_preflight, workspace),
                model_preflight_schema(),
                temperature=0.0,
                num_predict=_env_int_at_least("SHAMSU_PRD_PREFLIGHT_NUM_PREDICT", 768, 256),
            ),
            timeout=float(os.environ.get("SHAMSU_PRD_PREFLIGHT_TIMEOUT_SECONDS", "20")),
        )
        candidate = _loads_freeform_json(raw or "")
        preflight, errors = validate_model_preflight(deterministic_preflight, candidate)
    except Exception as exc:
        errors = [f"{type(exc).__name__}: {exc}"]
        preflight, _ = validate_model_preflight(deterministic_preflight, None)

    state = record_milestone_preflight(
        root, state, milestone_id, preflight, validation_errors=errors
    )
    source = str(preflight.get("preflight_source") or "deterministic")
    if ledger:
        ledger.log_event(
            "prd_model_preflight_finished",
            milestone_id=milestone_id,
            source=source,
            accepted=source == "model",
            validation_errors=errors,
            expected_files=list(preflight.get("expected_files") or []),
            active_skills=list(preflight.get("active_skills") or []),
        )
    if errors:
        console.print(
            f"[dim]Model preflight for {milestone_id} was rejected; using deterministic preflight.[/dim]"
        )
    elif source == "model":
        console.print(f"[dim]Model preflight accepted for {milestone_id}.[/dim]")
    return preflight, state


def _build_prd_model_preflight_prompt(preflight: dict[str, Any], workspace: Path) -> str:
    payload = {
        "compiled_preflight": {
            "milestone_id": preflight.get("milestone_id"),
            "title": preflight.get("title"),
            "stack_profile": preflight.get("stack_profile") or {},
            "requirement_ids": list(preflight.get("requirement_ids") or []),
            "active_skills_allowlist": list(preflight.get("active_skills") or []),
            "allowed_tools_allowlist": list(preflight.get("allowed_tools") or []),
            "expected_files": list(preflight.get("expected_files") or []),
            "verifier": preflight.get("verifier"),
            "attempt_budget": preflight.get("attempt_budget"),
            "rollback_policy": preflight.get("rollback_policy"),
            "requirements": [
                {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "text": item.get("text"),
                    "verification": item.get("verification"),
                    "implementing_files": list(item.get("implementing_files") or []),
                }
                for item in list(preflight.get("requirements") or [])[:16]
                if isinstance(item, dict)
            ],
        },
        "workspace_file_sample": _workspace_file_inventory_for_preflight(workspace),
        "rules": [
            "Return the same milestone_id.",
            "Return exactly the same requirement_ids set.",
            "Use only skills from active_skills_allowlist.",
            "Use only tools from allowed_tools_allowlist.",
            "Expected files must be safe relative file paths.",
            "Verifier is a short strategy label, not a shell command.",
            "implementation_steps must be concrete, ordered, and bounded to this milestone.",
            "Keep the stack_profile fixed; do not introduce frameworks or commands outside its selected blueprints.",
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=True)


def _workspace_file_inventory_for_preflight(workspace: Path, limit: int = 80) -> list[str]:
    try:
        files = walk_workspace_files(workspace, limit=limit)
    except Exception:
        return []
    result: list[str] = []
    root = workspace.resolve()
    for path in files[:limit]:
        try:
            result.append(Path(path).resolve().relative_to(root).as_posix())
        except (OSError, TypeError, ValueError):
            result.append(str(path).replace("\\", "/"))
    return result


# --- Plan mode: plan -> review -> proceed -------------------------------------

_FOLLOW_PLAN_PHRASES = (
    "follow the plan",
    "proceed with the plan",
    "execute the plan",
    "run the plan",
    "do the plan",
    "go with the plan",
    "start the plan",
    "implement the plan",
    "build the plan",
    "let's proceed",
    "lets proceed",
)


def _looks_like_follow_plan(text: str) -> bool:
    lowered = text.lower().strip()
    return any(phrase in lowered for phrase in _FOLLOW_PLAN_PHRASES)


def _plan_autonomous_execution_requested(text: str) -> bool:
    lowered = text.lower().strip()
    return any(
        phrase in lowered
        for phrase in (
            "build all autonomously",
            "run all autonomously",
            "execute all autonomously",
            "finish all autonomously",
            "run the rest autonomously",
            "build the rest autonomously",
        )
    )


async def _resolve_plan_route(task: str, workspace: Path, llm: LLMManager) -> str:
    """Classify the task the same way _handle_request routes it, so the plan (and
    later its execution) fit the kind of work: a PRD build, a code edit, a bugfix, etc."""
    if _looks_like_prd_build_request(task, workspace):
        return "prd_build"
    try:
        decision = await _route_prompt(task, llm)
        return decision.intent or "code_edit"
    except Exception:
        return "code_edit"


# Natural ways of asking for a plan in ordinary chat. Without these, only the
# literal `plan <task>` prefix planned anything, so "make me a plan to add auth"
# fell through to QA and got chatted at instead of planned.
_PLAN_REQUEST_PHRASES = (
    "make a plan",
    "make me a plan",
    "create a plan",
    "write a plan",
    "draft a plan",
    "draw up a plan",
    "come up with a plan",
    "put together a plan",
    "give me a plan",
    "build me a plan",
    "plan out",
    "plan how",
    "plan first",
)

# Questions ABOUT an existing plan are not requests for a new one.
_PLAN_QUESTION_PREFIXES = (
    "what is the plan",
    "what's the plan",
    "whats the plan",
    "show me the plan",
    "show the plan",
    "explain the plan",
    "read the plan",
)

_PLAN_MODE_OFF_COMMANDS = frozenset(
    {"plan off", "exit plan", "exit plan mode", "cancel plan", "leave plan mode", "plan mode off"}
)


def _looks_like_plan_request(user_input: str) -> bool:
    """True for a natural-language "plan this for me" request.

    Deliberately phrase-based rather than a bare "plan" keyword: "what's the
    plan" and "explain the plan" are questions about a plan that already exists.
    """
    text = user_input.lower().strip()
    if any(text.startswith(prefix) for prefix in _PLAN_QUESTION_PREFIXES):
        return False
    if re.match(r"^plan\s+(?:a|an|the|this|that|how|out)\b", text):
        return True
    return any(phrase in text for phrase in _PLAN_REQUEST_PHRASES)


def _run_plan_with_ledger(
    task: str,
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
) -> None:
    """Run `_handle_plan` inside a tracked run, like any other acting route."""
    ledger = start_run(workspace, user_input, session_logger=session_logger)
    set_current_run(ledger)
    try:
        with console.status(_thinking_status_for_input(user_input), spinner="dots"):
            _run_request(_handle_plan(task, workspace, console, session_logger=session_logger))
    except Exception as exc:
        ledger.fail(str(exc))
        clear_current_run()
        _report_request_error(exc, console, session_logger)
        return
    _finish_current_run(workspace, ledger)
    clear_current_run()


def _print_plan_mode_banner(console: Console) -> None:
    console.print(
        Panel(
            "Tell me what you want built, changed, or fixed.\n\n"
            "I'll research the workspace and write a step-by-step plan to "
            "`.shamsu/plans/` for you to review - [bold]I won't touch any project "
            "files yet[/bold]. Reply `proceed` once it looks right, or edit the file first.\n\n"
            "[dim]/plan off to leave plan mode.[/dim]",
            title="Plan mode",
            border_style="cyan",
        )
    )


async def _handle_plan(
    task: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    """Produce a reviewable plan for `task`, save it under .shamsu/plans/, and store
    it as a pending action so a later `proceed` executes it step by step."""
    task = task.strip()
    if not task:
        console.print("[yellow]Usage: plan <what you want built, changed, or fixed>[/yellow]")
        return
    llm = _make_llm_manager(session_logger, console, workspace)
    route = await _resolve_plan_route(task, workspace, llm)
    search, _uses_real_index = _build_search_agent(workspace, session_logger)
    workflow = PlanningWorkflow(workspace, llm=llm, search=search, session_logger=session_logger)
    try:
        plan = await workflow.run(task, route=route)
    except Exception as exc:
        console.print(f"[red]Could not build a plan: {exc}[/red]")
        return
    console.print(Panel(plan.markdown, title=f"Plan ({route}) - {len(plan.steps)} step(s)"))
    try:
        rel = plan.path.relative_to(workspace).as_posix()
    except ValueError:
        rel = str(plan.path)
    console.print(
        f"[dim]Saved to {rel} - edit it if you like, then reply `proceed` "
        "(or run /proceed) to execute it, or `no` to discard.[/dim]"
    )
    if session_logger is not None:
        session_logger.set_pending_action(
            {
                "type": "plan",
                "awaiting": "plan_approval",
                "plan_id": plan.plan_id,
                "route": route,
                "created_from_prompt": task,
            }
        )


async def _execute_pending_plan(
    pending_action: dict[str, Any],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    *,
    run_all: bool | None = None,
) -> None:
    if not _legacy_routing_enabled():
        await _simple_pending_run(
            _pending_plan_instruction(pending_action), workspace, console, session_logger
        )
        return
    plan_id = str(pending_action.get("plan_id", ""))
    route = str(pending_action.get("route", "code_edit"))
    task = str(pending_action.get("created_from_prompt") or pending_action.get("task") or "")
    if pending_action.get("awaiting") == "plan_continue":
        markdown = str(pending_action.get("plan_markdown") or "")
        steps = [str(step) for step in (pending_action.get("steps") or [])]
        contracts = contracts_from_markdown(plan_id or "continued-plan", task, steps, markdown)
    else:
        try:
            markdown = read_plan(workspace, plan_id)
        except Exception:
            console.print(
                "[red]Could not read the approved plan file - it may have been moved or deleted. "
                "Make a new plan with `plan <task>`.[/red]"
            )
            return
        steps = parse_plan_steps(markdown)
        contracts = _load_or_create_plan_contracts(plan_id, task, steps, markdown, workspace, session_logger)
    if run_all is None:
        run_all = is_long_running_enabled(workspace)
    max_steps = None if run_all else 1
    await _execute_plan(
        task,
        route,
        markdown,
        steps,
        workspace,
        console,
        session_logger,
        contracts=contracts,
        max_steps=max_steps,
        force_long_running_steps=bool(run_all),
    )


def _load_or_create_plan_contracts(
    plan_id: str,
    task: str,
    steps: list[str],
    markdown: str,
    workspace: Path,
    session_logger: SessionLogger | None = None,
) -> list[TaskContract]:
    contracts = load_plan_contracts(workspace, plan_id)
    if not contracts:
        contracts = contracts_from_markdown(plan_id, task, steps, markdown)
        write_plan_contracts(workspace, plan_id, contracts)
        _log_task_contract_event(
            session_logger,
            "task_contract.compat_created",
            {"plan_id": plan_id, "contracts": len(contracts)},
            f"Created {len(contracts)} compatibility Task Contract(s) from markdown",
        )
    return contracts


def _log_task_contract_event(
    session_logger: SessionLogger | None,
    event_type: str,
    payload: dict[str, Any],
    summary: str,
) -> None:
    try:
        if session_logger is not None:
            session_logger.log(event_type, payload, summary, workflow_id="task-contract")
    except Exception:
        pass
    ledger = get_current_run()
    if ledger is not None:
        try:
            ledger.log_event(event_type, **payload)
        except Exception:
            pass


def _pause_plan_for_question(
    task: str,
    route: str,
    plan_markdown: str,
    steps: list[str],
    resume_index: int,
    changed_files: list[str],
    workspace: Path,
    session_logger: SessionLogger | None,
) -> None:
    """Record where a plan stopped so the user's answer resumes it (gap J5).

    Stored alongside the pending QUESTION the agent loop already saved: the
    question captures what was asked, this captures what to do with the answer.
    """
    if session_logger is None:
        return
    try:
        session_logger.set_pending_action(
            {
                "type": "plan",
                "awaiting": "plan_resume",
                "task": task,
                "route": route,
                "plan_markdown": plan_markdown,
                "steps": list(steps),
                "resume_index": resume_index,
                "changed_files": list(changed_files),
            }
        )
    except Exception:
        pass


def _pause_plan_after_atomic_step(
    task: str,
    route: str,
    plan_markdown: str,
    remaining_steps: list[str],
    changed_files: list[str],
    console: Console,
    session_logger: SessionLogger | None,
) -> None:
    if not remaining_steps:
        return
    if session_logger is not None:
        try:
            session_logger.set_pending_action(
                {
                    "type": "plan",
                    "awaiting": "plan_continue",
                    "task": task,
                    "route": route,
                    "plan_markdown": plan_markdown,
                    "steps": list(remaining_steps),
                    "changed_files": list(changed_files),
                }
            )
        except Exception:
            pass
    next_step = remaining_steps[0]
    console.print(
        Panel(
            f"Finished one approved plan step.\n\nNext step: {next_step}\n\n"
            "Reply `continue` or `proceed` to run the next step. "
            "Reply `build all autonomously` to run the rest.",
            title="Plan Paused",
            border_style="cyan",
        )
    )
    _log_event(
        session_logger,
        "plan.atomic_step_paused",
        {"remaining_steps": len(remaining_steps), "changed_files": changed_files},
        "Paused approved plan after one atomic step",
        workflow_id="plan",
    )


def _take_paused_plan(session_logger: SessionLogger | None) -> dict[str, Any] | None:
    """Pop a plan paused on a question, if this reply is answering one."""
    if session_logger is None:
        return None
    try:
        pending = session_logger.get_pending_action()
    except Exception:
        return None
    if pending.get("awaiting") != "plan_resume":
        return None
    try:
        session_logger.clear_pending_action()
    except Exception:
        pass
    return pending


async def _resume_paused_plan(
    paused: dict[str, Any],
    answer_prompt: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
) -> None:
    """Re-enter plan execution at the step that asked, with the answer in hand.

    The remaining steps are replayed as a fresh plan run whose first step is the
    one that stopped - reusing `_execute_plan` rather than duplicating the
    milestone/verify machinery.
    """
    steps = [str(step) for step in (paused.get("steps") or [])]
    resume_index = int(paused.get("resume_index", 0) or 0)
    if not _legacy_routing_enabled():
        instruction = _pending_plan_instruction(paused, skip=resume_index)
        answer = (answer_prompt or "").strip()
        await _simple_pending_run(
            f"{answer}\n\n{instruction}".strip() if answer else instruction,
            workspace,
            console,
            session_logger,
        )
        return
    remaining = steps[resume_index:]
    if not remaining:
        return
    console.print(
        f"[green]Resuming the plan at step {resume_index + 1}/{len(steps)} with your answer.[/green]"
    )
    task = str(paused.get("task", ""))
    await _execute_plan(
        f"{task}\n\n(The user answered: {answer_prompt})" if task else answer_prompt,
        str(paused.get("route", "code_edit")),
        str(paused.get("plan_markdown", "")),
        remaining,
        workspace,
        console,
        session_logger=session_logger,
        max_steps=None if is_long_running_enabled(workspace) else 1,
        force_long_running_steps=is_long_running_enabled(workspace),
    )


async def _execute_plan(
    task: str,
    route: str,
    plan_markdown: str,
    steps: list[str],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    contracts: list[TaskContract] | None = None,
    max_steps: int | None = None,
    force_long_running_steps: bool = False,
) -> None:
    """Execute an approved plan. Each step runs as its own agent pass with the plan
    as authoritative context, tracked as a MilestoneTask (visible via `tasks`)."""
    if plan_has_no_steps(plan_markdown):
        # The planner produced nothing. Executing anyway used to hand the agent
        # the placeholder sentence as if it were a task; a single pass over an
        # empty plan is not a fallback, it is a guess with no instruction in it.
        console.print(
            Panel(
                "This plan has no steps - the planner did not produce any.\n\n"
                "Open the plan file and add the steps you want, then run `/proceed`. "
                "Or re-run `plan <task>` with a more specific task.",
                title="Nothing To Execute",
                border_style="red",
            )
        )
        _log_event(
            session_logger,
            "plan.execute.refused_empty",
            {"task": task, "route": route},
            "Refused to execute a plan with no steps",
            workflow_id="plan",
        )
        return
    if not steps:
        contracts = contracts or contracts_from_markdown("ad-hoc-plan", task, [], plan_markdown)
        contract = run_file_preflight(contracts[0], workspace) if contracts else None
        if contract is not None:
            validation = validate_contract(contract, workspace)
            if not validation.ok:
                console.print("[red]Task Contract validation failed: " + "; ".join(validation.errors) + "[/red]")
                return
        console.print(
            "[cyan]No discrete steps found in the plan - executing it in a single pass.[/cyan]"
        )
        await _run_agent_chat(
            _plan_single_request(task, plan_markdown)
            + (f"\n\n{contract_prompt(contract)}" if contract is not None else ""),
            workspace,
            console,
            session_logger=session_logger,
            force_long_running=force_long_running_steps,
            auto_approve=True,
            use_planner=False,
            allowed_write_paths=tuple(contract.allowed_write_paths) if contract and contract.allowed_write_paths else None,
            task_contract=contract,
        )
        return

    contracts = list(contracts or contracts_from_markdown("ad-hoc-plan", task, steps, plan_markdown))
    step_limit = max_steps if max_steps is not None and max_steps > 0 else None
    task_obj = _create_plan_task(task, steps)
    save_task(task_obj, workspace)
    console.print(
        f"[green]Executing the approved plan - {len(steps)} step(s). Tracking task {task_obj.task_id}.[/green]"
    )
    changed_files: list[str] = []
    for index, step_text in enumerate(steps):
        step = task_obj.steps[index]
        contract = contracts[index] if index < len(contracts) else contracts_from_markdown(
            "ad-hoc-plan", task, [step_text], plan_markdown
        )[0]
        _log_task_contract_event(
            session_logger,
            "task_contract.preflight_started",
            {"task_id": contract.task_id, "objective": contract.objective},
            f"Preflight started for {contract.task_id}",
        )
        contract = run_file_preflight(contract, workspace)
        contracts[index] = contract
        if contract.run_id and contract.run_id != "ad-hoc-plan":
            write_plan_contracts(workspace, contract.run_id, contracts)
        validation = validate_contract(contract, workspace)
        if not validation.ok:
            task_obj = mark_step_failed(task_obj, step.id, "; ".join(validation.errors))
            save_task(task_obj, workspace)
            console.print("[red]Task Contract validation failed: " + "; ".join(validation.errors) + "[/red]")
            return
        _log_task_contract_event(
            session_logger,
            "task_contract.scope_locked",
            {
                "task_id": contract.task_id,
                "candidate_files": contract.candidate_files,
                "allowed_write_paths": contract.allowed_write_paths,
                "expected_write_paths": contract.expected_write_paths,
            },
            f"Locked write scope for {contract.task_id}",
        )

        def _persist_contract_update(updated: TaskContract, *, contract_index: int = index) -> None:
            contracts[contract_index] = updated
            if updated.run_id and updated.run_id != "ad-hoc-plan":
                write_plan_contracts(workspace, updated.run_id, contracts)

        task_obj = mark_step_running(task_obj, step.id)
        save_task(task_obj, workspace)
        console.print(f"[cyan]  -> Step {index + 1}/{len(steps)}: {step_text}[/cyan]")
        try:
            result = await _run_agent_chat(
                _plan_step_request(task, steps, index + 1, len(steps))
                + "\n\n"
                + contract_prompt(contract),
                workspace,
                console,
                session_logger=session_logger,
                force_long_running=force_long_running_steps,
                auto_approve=True,
                use_planner=False,
                runtime_task_id=f"task-{task_obj.task_id}-{step.id}",
                allowed_write_paths=tuple(contract.allowed_write_paths) if contract.allowed_write_paths else None,
                task_contract=contract,
                task_contract_persist=_persist_contract_update,
            )
        except Exception as exc:
            task_obj = mark_step_failed(task_obj, step.id, str(exc))
            save_task(task_obj, workspace)
            raise
        for path in getattr(result, "changed_files", ()) or ():
            if path not in changed_files:
                changed_files.append(path)
        # The step asked the user something instead of finishing (gap J5). The
        # pending-question check only ran at the top of the REPL loop, so this
        # used to mark the step "done" - a plain lie - and run every later step
        # on the unanswered assumption, with a subsequent step free to overwrite
        # the question nobody had seen yet. Pause here and resume on the answer.
        if getattr(result, "awaiting_user", False):
            save_task(task_obj, workspace)  # leave the step RUNNING, not done
            _pause_plan_for_question(
                task, route, plan_markdown, steps, index, changed_files, workspace, session_logger
            )
            console.print(
                f"[yellow]Plan paused at step {index + 1}/{len(steps)} - answer above and "
                "I'll pick up from here.[/yellow]"
            )
            return
        task_obj = mark_step_done(task_obj, step.id, "Agent completed this plan step.")
        save_task(task_obj, workspace)
        if step_limit is not None and index + 1 >= step_limit:
            remaining = steps[index + 1 :]
            if remaining:
                _pause_plan_after_atomic_step(
                    task,
                    route,
                    plan_markdown,
                    remaining,
                    changed_files,
                    console,
                    session_logger,
                )
                return
    console.print(f"[green]Plan execution complete. Task: {task_obj.task_id}[/green]")
    # Integration check: a later step may have broken an earlier step's file, so
    # verify the whole changed set once at the end (never claim done unverified).
    await _verify_completed_plan(changed_files, workspace, console, session_logger)


async def _verify_completed_plan(
    changed_files: list[str],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
) -> bool | None:
    """Run one deterministic lightweight verifier over everything the plan
    changed and report an honest verdict. Best-effort: never raises."""
    if not changed_files:
        return None
    try:
        outcome = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: verify_only(
                workspace,
                list(changed_files),
                lightweight=True,
                session_logger=session_logger,
            ),
        )
    except Exception:
        return None
    if outcome.unverifiable:
        # E2: lightweight mode has no verifier for this stack (a node build
        # needs `npm install`), which used to mean a PERMANENT shrug - JS/TS
        # users never got a verified verdict at all. If the heavy path could
        # verify it, OFFER it instead of silently downgrading. One approval,
        # at the natural end-of-plan pause, never automatic.
        heavy_command = ""
        try:
            heavy_command = default_verify_command(list(changed_files), lightweight=False)
        except Exception:
            heavy_command = ""
        if not heavy_command:
            console.print(
                "[dim]No deterministic verifier available for these changes - left UNVERIFIED.[/dim]"
            )
            return None
        request = ApprovalRequest(
            action_type="run_command",
            description="Run the full verifier (includes dependency install) to confirm the plan's changes.",
            risk_level="medium",
            preview=heavy_command,
            working_dir=str(workspace),
            reason=(
                "The quick check cannot verify this stack. The full check installs "
                "dependencies and builds, which can take minutes."
            ),
        )
        if not _make_approval_manager(workspace, session_logger, console).ask(request):
            console.print(
                "[dim]Skipped the full verifier - the plan's changes are left UNVERIFIED.[/dim]"
            )
            return None
        try:
            with console.status("Running the full verifier (install + build)...", spinner="dots"):
                heavy = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: verify_only(
                        workspace,
                        list(changed_files),
                        lightweight=False,
                        session_logger=session_logger,
                    ),
                )
        except Exception:
            console.print("[dim]The full verifier errored - left UNVERIFIED.[/dim]")
            return None
        if heavy.verified:
            console.print(
                Panel(heavy.summary, title="Plan verified (full build)", border_style="green")
            )
            return True
        else:
            console.print(
                Panel(
                    f"{heavy.summary}\nThe full build did NOT pass - review the affected "
                    "files before relying on the result.",
                    title="Plan UNVERIFIED",
                    border_style="red",
                )
            )
        return False
    if outcome.verified:
        console.print(Panel(outcome.summary, title="Plan verified", border_style="green"))
        return True
    else:
        console.print(
            Panel(
                f"{outcome.summary}\nThe plan's changes did NOT pass verification - "
                "review the affected files before relying on the result.",
                title="Plan UNVERIFIED",
                border_style="red",
            )
        )
        return False


def _prd_verification_summary(outcome: Any) -> str:
    """Attach the primary failing output to the compact milestone verdict."""
    summary = str(getattr(outcome, "summary", "") or "Verification failed.")
    failed_result = next(
        (
            result
            for result in getattr(outcome, "steps", ()) or ()
            if not bool(getattr(result, "passed", False))
        ),
        None,
    )
    if failed_result is None:
        return summary
    step = getattr(failed_result, "step", None)
    stage = str(getattr(step, "stage", "") or "")
    stdout = str(getattr(failed_result, "stdout", "") or "").strip()
    stderr = str(getattr(failed_result, "stderr", "") or "").strip()
    ordered = (("stdout", stdout), ("stderr", stderr)) if stage == "migration" else (
        ("stderr", stderr),
        ("stdout", stdout),
    )
    diagnostic = "\n".join(
        f"{label}:\n{value}" for label, value in ordered if value
    ).strip()
    if not diagnostic:
        return summary
    if len(diagnostic) > 1800:
        diagnostic = diagnostic[-1800:]
    return f"{summary}\nPrimary error:\n{diagnostic}"


def _prd_verification_cwd(outcome: Any, workspace: Path) -> str:
    """Return the verifier cwd as a workspace-relative repair fact."""
    failed_step = getattr(outcome, "failed_step", None)
    steps = tuple(getattr(outcome, "steps", ()) or ())
    step = failed_step or (getattr(steps[0], "step", None) if steps else None)
    cwd = getattr(step, "cwd", None)
    if cwd is None:
        return "."
    try:
        relative = Path(cwd).resolve().relative_to(workspace.resolve())
    except (OSError, ValueError):
        return str(cwd)
    return relative.as_posix() or "."


def _prd_requirement_evidence_errors(
    preflight: dict[str, Any],
    workspace: Path,
) -> list[str]:
    """Check durable behavior evidence that compile/framework checks cannot prove."""
    requirements = [
        item
        for item in preflight.get("requirements") or []
        if isinstance(item, dict)
        and str(item.get("scope") or "in") == "in"
        and str(item.get("priority") or "must") == "must"
    ]
    kinds = {str(item.get("kind") or "").lower() for item in requirements}
    if not kinds & {"auth", "authorization", "role"}:
        return []
    roots = _workspace_relative_paths(
        [str(preflight.get("project_root") or ".")], workspace
    )
    root = workspace / (roots[0] if roots else ".")
    if not root.is_dir():
        return ["requirement evidence root does not exist"]
    source_parts: list[str] = []
    test_parts: list[str] = []
    url_parts: list[str] = []
    url_sources: dict[str, str] = {}
    placeholder_files: list[str] = []
    for path in root.rglob("*.py"):
        if any(part in {".venv", "node_modules", "migrations", "__pycache__"} for part in path.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lowered = content.lower()
        if re.search(r"\b(?:add|implement)\s+[^\r\n#]{0,60}\blogic\s+here\b", lowered):
            try:
                placeholder_files.append(path.relative_to(workspace).as_posix())
            except ValueError:
                placeholder_files.append(path.name)
        if path.name.startswith("test") or "tests" in {part.lower() for part in path.parts}:
            test_parts.append(lowered)
        else:
            source_parts.append(lowered)
        if path.name == "urls.py":
            url_parts.append(lowered)
            try:
                url_sources[path.relative_to(workspace).as_posix()] = lowered
            except ValueError:
                url_sources[path.as_posix()] = lowered
    source = "\n".join(source_parts)
    tests = "\n".join(test_parts)
    urls = "\n".join(url_parts)
    expected_urls = [
        path
        for path in _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
        if path.replace("\\", "/").endswith("/config/urls.py")
    ]
    root_urls = "\n".join(url_sources.get(path, "") for path in expected_urls)
    included_urls = root_urls
    for module in re.findall(r"include\(\s*['\"](?P<module>[^'\"]+)['\"]", root_urls):
        suffix = module.replace(".", "/") + ".py"
        included_urls += "\n" + "\n".join(
            content for path, content in url_sources.items() if path.endswith(suffix)
        )
    reachable_urls = included_urls or urls
    errors: list[str] = []
    if placeholder_files and kinds & {"auth", "authorization"}:
        errors.append(
            "placeholder behavior remains in " + ", ".join(sorted(set(placeholder_files))[:6])
        )
    texts = [str(item.get("text") or "").lower() for item in requirements]
    required_roles = {
        role
        for item, text in zip(requirements, texts, strict=True)
        if str(item.get("kind") or "").lower() == "role"
        for role in ("admin", "teacher", "student")
        if role in text
    }
    wants_login = any(
        str(item.get("kind") or "").lower() == "auth" and "login" in text
        for item, text in zip(requirements, texts, strict=True)
    )
    wants_logout = any(
        str(item.get("kind") or "").lower() == "auth" and "logout" in text
        for item, text in zip(requirements, texts, strict=True)
    )
    wants_session = any(
        str(item.get("kind") or "").lower() == "auth" and "session" in text
        for item, text in zip(requirements, texts, strict=True)
    )
    missing_roles = sorted(
        role for role in required_roles if not re.search(rf"['\"]{role}['\"]", source)
    )
    if missing_roles:
        errors.append(
            "roles have no executable source declarations: " + ", ".join(missing_roles)
        )
    if wants_login:
        login_import = re.search(
            r"from\s+django\.contrib\.auth\s+import[^\r\n]*\blogin\b", source
        )
        if not login_import or "authenticate(" not in source or not re.search(
            r"(?<!def )\blogin\s*\(", source
        ):
            errors.append("login does not call Django authenticate() and login()")
        if "login" not in reachable_urls:
            errors.append("login endpoint is not wired into Django URL patterns")
    if wants_logout:
        logout_import = re.search(
            r"from\s+django\.contrib\.auth\s+import[^\r\n]*\blogout\b", source
        )
        if not logout_import or not re.search(r"(?<!def )\blogout\s*\(", source):
            errors.append("logout does not call Django logout()")
        if "logout" not in reachable_urls:
            errors.append("logout endpoint is not wired into Django URL patterns")
    if wants_session and "session" not in tests:
        errors.append("session persistence has no focused test evidence")
    if (wants_login or wants_logout) and not (
        "test" in tests and "login" in tests and "logout" in tests
    ):
        errors.append("login/logout behavior has no focused Django tests")

    authorization_texts = [
        text
        for item, text in zip(requirements, texts, strict=True)
        if str(item.get("kind") or "").lower() == "authorization"
    ]
    if authorization_texts:
        if "request.user" not in source:
            errors.append("authorization rules do not inspect the authenticated user")
        required_terms = {
            term
            for text in authorization_texts
            for term in ("teacher", "student", "course", "submission", "grade")
            if term in text
        }
        missing_terms = sorted(term for term in required_terms if term not in source)
        if missing_terms:
            errors.append(
                "authorization implementation omits required domain terms: "
                + ", ".join(missing_terms)
            )
        if not tests or not re.search(r"\b(?:403|forbidden|permissiondenied)\b", tests):
            errors.append("authorization rules have no forbidden-access tests")
    return list(dict.fromkeys(errors))


def _prd_behavior_test_count(outcome: Any) -> int | None:
    counts: list[int] = []
    for result in getattr(outcome, "steps", ()) or ():
        stage = str(getattr(getattr(result, "step", None), "stage", "") or "")
        if stage != "test":
            continue
        output = "\n".join(
            [
                str(getattr(result, "stdout", "") or ""),
                str(getattr(result, "stderr", "") or ""),
            ]
        )
        counts.extend(
            int(match.group("count"))
            for match in re.finditer(
                r"(?:Found|Ran)\s+(?P<count>\d+)\s+test(?:\(s\)|s)?",
                output,
                re.IGNORECASE,
            )
        )
    return max(counts) if counts else None


def _prd_requires_behavior_tests(preflight: dict[str, Any]) -> bool:
    return any(
        isinstance(item, dict)
        and str(item.get("scope") or "in") == "in"
        and str(item.get("priority") or "must") == "must"
        and str(item.get("kind") or "").lower()
        in {"auth", "authorization", "feature", "screen", "workflow", "mechanic"}
        for item in preflight.get("requirements") or []
    )


async def _verify_prd_milestone(
    milestone_id: str,
    preflight: dict[str, Any],
    changed_files: list[str],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
) -> tuple[str, dict[str, Any]]:
    """Return the milestone checkpoint status plus compact verifier evidence."""
    if preflight.get("missing_skills"):
        summary = "Required milestone skills are unavailable: " + ", ".join(
            str(item) for item in preflight["missing_skills"]
        )
        verification = _milestone_verification_payload("failed", files=[], summary=summary)
        _log_prd_milestone_verification(milestone_id, verification)
        return "failed", verification
    if not changed_files and _prd_milestone_requires_mutation(preflight):
        summary = "The mandatory milestone completed without a confirmed source mutation."
        verification = _milestone_verification_payload("failed", files=[], summary=summary)
        _log_prd_milestone_verification(milestone_id, verification)
        console.print(Panel(summary, title=f"Milestone {milestone_id} FAILED", border_style="red"))
        return "failed", verification
    expected_files = _preflight_expected_files(preflight)
    missing_expected = _missing_expected_files(expected_files, workspace)
    if missing_expected and _prd_milestone_is_mandatory(preflight):
        summary = "Mandatory milestone is missing expected architecture files: " + ", ".join(
            missing_expected[:12]
        )
        verification = _milestone_verification_payload(
            "failed",
            files=[path for path in expected_files if path not in missing_expected],
            summary=summary,
        )
        _log_prd_milestone_verification(milestone_id, verification)
        console.print(Panel(summary, title=f"Milestone {milestone_id} FAILED", border_style="red"))
        return "failed", verification
    invalid_expected = _invalid_expected_architecture_files(preflight, workspace)
    if invalid_expected and _prd_milestone_is_mandatory(preflight):
        summary = "Mandatory milestone contains hollow or invalid architecture files: " + ", ".join(
            invalid_expected[:12]
        )
        verification = _milestone_verification_payload(
            "failed",
            files=expected_files,
            summary=summary,
        )
        _log_prd_milestone_verification(milestone_id, verification)
        console.print(Panel(summary, title=f"Milestone {milestone_id} FAILED", border_style="red"))
        return "failed", verification
    verifier_files = _milestone_verifier_files(preflight, changed_files, workspace)
    if not verifier_files:
        missing = _missing_expected_files(expected_files, workspace)
        if expected_files and len(missing) == len(expected_files):
            summary = (
                "Milestone produced no verifier inputs and none of its expected files exist: "
                + ", ".join(missing[:8])
            )
            verification = _milestone_verification_payload(
                "failed",
                files=[],
                summary=summary,
            )
            _log_prd_milestone_verification(milestone_id, verification)
            console.print(
                Panel(summary, title=f"Milestone {milestone_id} FAILED", border_style="red")
            )
            return "failed", verification
        summary = "No deterministic verifier is available for this milestone (UNVERIFIED)."
        verification = _milestone_verification_payload(
            "unverifiable",
            files=[],
            summary=summary,
            unverifiable=True,
        )
        _log_prd_milestone_verification(milestone_id, verification)
        # Same reasoning as the unverifiable branch below: a missing verifier
        # is a gap in the gate, not a defect in the work, and failing here
        # blocked every dependent milestone.
        if _prd_milestone_is_mandatory(preflight):
            console.print(
                Panel(
                    f"{summary}\nContinuing; this milestone is recorded as implemented "
                    f"but UNVERIFIED.",
                    title=f"Milestone {milestone_id} unverified",
                    border_style="yellow",
                )
            )
        else:
            console.print(f"[dim]{summary}[/dim]")
        return "implemented", verification

    stack = _milestone_stack(verifier_files, preflight)
    stack_hint = _milestone_stack_hint(preflight)
    # The milestone's own declared verifier (e.g. `python manage.py check`)
    # runs as a required acceptance step - it used to be a stack hint only,
    # leaving the declared check unexecuted ("no deterministic verifier").
    acceptance_commands = _prd_milestone_acceptance_commands(preflight, workspace)
    try:
        outcome = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: verify_only(
                workspace,
                verifier_files,
                stack=stack,
                stack_hint=stack_hint,
                lightweight=True,
                session_logger=session_logger,
                acceptance_commands=acceptance_commands,
            ),
        )
    except Exception as exc:
        summary = f"Milestone verifier errored before producing a verdict: {exc}"
        verification = _milestone_verification_payload(
            "failed",
            files=verifier_files,
            summary=summary,
        )
        _log_prd_milestone_verification(milestone_id, verification)
        console.print(Panel(summary, title=f"Milestone {milestone_id} FAILED", border_style="red"))
        return "failed", verification

    if (
        not outcome.verified
        and not outcome.unverifiable
        and not str(outcome.command or "").strip()
        and outcome.exit_code is None
    ):
        outcome = replace(
            outcome,
            unverifiable=True,
            summary=(
                outcome.summary
                or "No deterministic verifier is available for this milestone (UNVERIFIED)."
            ),
        )

    verification = _milestone_verification_payload(
        outcome.status(),
        files=verifier_files,
        summary=_prd_verification_summary(outcome),
        verified=outcome.verified,
        unverifiable=outcome.unverifiable,
        exit_code=outcome.exit_code,
        command=outcome.command,
        cwd=_prd_verification_cwd(outcome, workspace),
    )
    test_count = _prd_behavior_test_count(outcome)
    missing_behavior_tests = (
        outcome.verified
        and _prd_requires_behavior_tests(preflight)
        and test_count == 0
    )
    if missing_behavior_tests:
        verification = {
            **verification,
            "status": "failed",
            "verified": False,
            "summary": (
                "Requirement evidence validation failed: behavior milestone verifier "
                "discovered 0 tests; add focused requirement tests"
            ),
        }
        _log_prd_milestone_verification(milestone_id, verification)
        console.print(
            Panel(
                verification["summary"],
                title=f"Milestone {milestone_id} FAILED",
                border_style="red",
            )
        )
        return "failed", verification
    _log_prd_milestone_verification(milestone_id, verification)
    if outcome.verified:
        console.print(
            Panel(outcome.summary, title=f"Milestone {milestone_id} verified", border_style="green")
        )
        return "verified", verification
    if outcome.unverifiable:
        # "No verifier exists for this kind of change" is a gap in the gate,
        # not a defect in the work. Failing the milestone for it also blocked
        # every dependent, so one uncheckable behavioural milestone ended the
        # build: M-002 unverifiable took 15 further milestones down with it.
        # Report it honestly as unverified - never as verified - and let the
        # build continue. Checkpoint revalidation and the final whole-project
        # verification still apply.
        if _prd_milestone_is_mandatory(preflight):
            console.print(
                Panel(
                    f"{outcome.summary}\nContinuing; this milestone is recorded as "
                    f"implemented but UNVERIFIED.",
                    title=f"Milestone {milestone_id} unverified",
                    border_style="yellow",
                )
            )
        else:
            console.print(f"[dim]{outcome.summary}[/dim]")
        return "implemented", verification
    console.print(
        Panel(
            f"{outcome.summary}\nStopping before the next PRD milestone.",
            title=f"Milestone {milestone_id} UNVERIFIED",
            border_style="red",
        )
    )
    return "failed", verification


async def _repair_failed_prd_milestone(
    root: Path,
    state: dict[str, Any],
    milestone_id: str,
    preflight: dict[str, Any],
    verification: dict[str, Any],
    milestone_changed: list[str],
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    title: str,
    relative_path: Path,
    prd_brief: str,
    milestones: list[str],
    milestone_index: int,
    milestone_count: int,
) -> tuple[str, dict[str, Any], list[str], dict[str, Any]]:
    budget = _prd_milestone_repair_budget(preflight)
    if budget <= 0:
        return "failed", verification, milestone_changed, state

    ledger = get_current_run()
    if ledger:
        ledger.log_event(
            "prd_milestone_repair_budget_started",
            milestone_id=milestone_id,
            attempts=budget,
            verification_status=verification.get("status"),
        )
    console.print(
        f"[dim]Milestone {milestone_id} verifier failed; starting up to {budget} repair pass(es).[/dim]"
    )
    latest_status = "failed"
    latest_verification = dict(verification)
    all_changed = list(dict.fromkeys(milestone_changed))
    for attempt in range(1, budget + 1):
        state = record_milestone_repair(
            root,
            state,
            milestone_id,
            attempt=attempt,
            phase="started",
            status="repairing",
            changed_files=all_changed,
            verification=latest_verification,
            message=str(latest_verification.get("summary") or "Milestone verification failed."),
        )
        if ledger:
            ledger.log_event(
                "prd_milestone_repair_started",
                milestone_id=milestone_id,
                attempt=attempt,
                attempts=budget,
                verification_status=latest_verification.get("status"),
            )
        repair_tool_baseline = _prd_tool_snapshot(workspace)
        repair_transaction_baseline = _prd_transaction_snapshot(workspace)
        invalid_before = set(_invalid_expected_architecture_files(preflight, workspace))
        migration_source_files = _prd_migration_source_repair_files(
            latest_verification, preflight, workspace
        )
        migration_source_guidance = _prd_migration_source_edit_guidance(
            latest_verification, preflight, workspace
        )
        semantic_source_files = _prd_semantic_source_repair_files(
            latest_verification, preflight, workspace
        )
        semantic_source_guidance = _prd_semantic_source_edit_guidance(
            latest_verification, preflight, workspace
        )
        # A plain runtime exception (the most common Django failure) matched
        # neither branch above, so repair ran with no root cause, no scoped
        # file, and no forced tool - and the model just talked.
        runtime_source_files = _prd_runtime_exception_repair_files(
            latest_verification, workspace
        )
        runtime_source_guidance = _prd_runtime_exception_edit_guidance(
            latest_verification, workspace
        )
        # Django's own check errors (fields.EXXX) carry no file or line, so the
        # traceback parser skips them and the repair used to get nothing.
        check_source_files = _prd_django_check_repair_files(
            latest_verification, preflight, workspace
        )
        check_source_guidance = _prd_django_check_edit_guidance(
            latest_verification, preflight, workspace
        )
        focused_source_files = (
            migration_source_files
            or semantic_source_files
            or check_source_files
            or runtime_source_files
        )
        focused_source_guidance = (
            migration_source_guidance
            or semantic_source_guidance
            or check_source_guidance
            or runtime_source_guidance
        )
        semantic_behavior_failure = str(latest_verification.get("summary") or "").startswith(
            "Requirement evidence validation failed:"
        )
        semantic_repair_context = (
            _prd_semantic_repair_source_context(preflight, workspace)
            if semantic_behavior_failure and not focused_source_files
            else ""
        )
        repair_tools = tuple(preflight.get("allowed_tools") or ())
        if focused_source_files:
            repair_tools = tuple(
                name
                for name in repair_tools
                if name in {"read_file", "file_info", "edit_file"}
            )
        try:
            result = await _run_agent_chat(
                _build_prd_milestone_repair_request(
                    title,
                    relative_path,
                    prd_brief,
                    milestones,
                    milestone_index,
                    milestone_count,
                    preflight,
                    latest_verification,
                    all_changed,
                    attempt,
                    budget,
                )
                + ("\n\n" + focused_source_guidance if focused_source_guidance else "")
                + ("\n\n" + semantic_repair_context if semantic_repair_context else "")
                + "\n\n"
                + _prd_milestone_skill_context(workspace, preflight),
                workspace,
                console,
                session_logger=session_logger,
                force_long_running=True,
                auto_approve=True,
                allowed_write_paths=(
                    tuple(focused_source_files)
                    or _prd_milestone_repair_write_scope(preflight, all_changed, workspace)
                    or None
                ),
                allowed_read_paths=(str(preflight.get("project_root")),)
                if str(preflight.get("project_root") or "")
                else None,
                allowed_tools=repair_tools,
                use_long_term_memory=False,
                use_planner=False,
                user_request=_prd_agent_safety_request(
                    str(preflight.get("project_root") or ".")
                ),
                required_tool_prefix=(
                    "edit_file"
                    if focused_source_files
                    else "write_file"
                    if semantic_behavior_failure
                    else ""
                ),
                hydrate_history=False,
                verify_changes=False,
            )
        except Exception as exc:
            latest_status = "failed"
            latest_verification = _milestone_verification_payload(
                "failed",
                files=all_changed,
                summary=f"Milestone repair attempt errored: {exc}",
            )
            state = record_milestone_repair(
                root,
                state,
                milestone_id,
                attempt=attempt,
                phase="finished",
                status=latest_status,
                changed_files=[],
                verification=latest_verification,
                message=latest_verification["summary"],
            )
            if ledger:
                ledger.log_event(
                    "prd_milestone_repair_finished",
                    milestone_id=milestone_id,
                    attempt=attempt,
                    status=latest_status,
                    changed_files=[],
                    verification_status=latest_verification.get("status"),
                    error=f"{type(exc).__name__}: {exc}",
                )
            break
        repair_changed = list(getattr(result, "changed_files", ()) or ())
        invalid_after = set(_invalid_expected_architecture_files(preflight, workspace))
        wrong_entrypoints = _unexpected_prd_entrypoint_changes(preflight, repair_changed, workspace)
        # Compare parsed contents, not raw strings: a repair that satisfied one
        # of two missing fields used to read as a new error and get rolled back,
        # discarding real progress (observed live 2026-08-02, run 8).
        introduced_invalid = sorted(
            set(_prd_repair_regressions(invalid_before, invalid_after)) | set(wrong_entrypoints)
        )
        if introduced_invalid:
            transaction_ids = _prd_transactions_since(
                workspace, repair_transaction_baseline
            )
            for transaction_id in reversed(transaction_ids):
                ok, message = rollback_transaction(workspace, transaction_id)
                if ledger:
                    ledger.log_rollback(transaction_id, ok, message)
            latest_status = "failed"
            rejected = (
                "Repair introduced architecture regressions and was rolled back: "
                + ", ".join(introduced_invalid)
            )
            latest_verification = {
                **latest_verification,
                "status": "failed",
                "verified": False,
                "summary": str(latest_verification.get("summary") or "Verification failed.")
                + "\nRejected repair attempt: "
                + rejected,
            }
            state = record_milestone_repair(
                root,
                state,
                milestone_id,
                attempt=attempt,
                phase="finished",
                status=latest_status,
                changed_files=[],
                verification=latest_verification,
                message=latest_verification["summary"],
            )
            if attempt < budget:
                continue
            break
        all_changed = list(dict.fromkeys([*all_changed, *repair_changed]))
        if getattr(result, "awaiting_user", False):
            latest_status = "blocked"
            latest_verification = _milestone_verification_payload(
                "blocked",
                files=all_changed,
                summary=getattr(result, "final", "") or "Milestone repair requested user input.",
            )
            state = record_milestone_repair(
                root,
                state,
                milestone_id,
                attempt=attempt,
                phase="finished",
                status=latest_status,
                changed_files=repair_changed,
                verification=latest_verification,
                message=latest_verification["summary"],
            )
            if ledger:
                ledger.log_event(
                    "prd_milestone_repair_finished",
                    milestone_id=milestone_id,
                    attempt=attempt,
                    status=latest_status,
                    changed_files=repair_changed,
                    verification_status=latest_verification.get("status"),
                )
            break
        if getattr(result, "stopped", False) and not repair_changed:
            latest_status = "failed"
            unresolved_commands = _prd_unrecovered_command_failures(
                workspace, repair_tool_baseline
            )
            if unresolved_commands:
                latest_verification = _prd_command_failure_verification(unresolved_commands)
            stall_message = (
                str(latest_verification.get("summary") or "")
                if unresolved_commands
                else getattr(result, "final", "")
                or "Milestone repair stopped before completion."
            )
            state = record_milestone_repair(
                root,
                state,
                milestone_id,
                attempt=attempt,
                phase="finished",
                status=latest_status,
                changed_files=repair_changed,
                verification=latest_verification,
                message=stall_message,
            )
            if ledger:
                ledger.log_event(
                    "prd_milestone_repair_finished",
                    milestone_id=milestone_id,
                    attempt=attempt,
                    status=latest_status,
                    changed_files=repair_changed,
                    verification_status=latest_verification.get("status"),
                )
            if attempt < budget:
                continue
            break

        unresolved_commands = _prd_unrecovered_command_failures(
            workspace, repair_tool_baseline
        )
        if unresolved_commands:
            latest_status = "failed"
            latest_verification = _prd_command_failure_verification(unresolved_commands)
        else:
            latest_status, latest_verification = await _verify_prd_milestone(
                milestone_id,
                preflight,
                all_changed,
                workspace,
                console,
                session_logger,
            )
        state = record_milestone_repair(
            root,
            state,
            milestone_id,
            attempt=attempt,
            phase="finished",
            status=latest_status,
            changed_files=repair_changed,
            verification=latest_verification,
            message=str(latest_verification.get("summary") or "Milestone repair pass completed."),
        )
        if ledger:
            ledger.log_event(
                "prd_milestone_repair_finished",
                milestone_id=milestone_id,
                attempt=attempt,
                status=latest_status,
                changed_files=repair_changed,
                verification_status=latest_verification.get("status"),
            )
        if latest_status != "failed":
            break

    return latest_status, latest_verification, all_changed, state


def _prd_milestone_repair_write_scope(
    preflight: dict[str, Any],
    changed_files: list[str],
    workspace: Path,
) -> tuple[str, ...]:
    project_root = str(preflight.get("project_root") or "")
    if project_root:
        return (project_root,)
    expected = _preflight_expected_files(preflight)
    scope = _workspace_relative_paths([*expected, *changed_files], workspace)
    return tuple(scope)


def _build_prd_milestone_repair_request(
    title: str,
    relative_path: Path,
    prd_brief: str,
    milestones: list[str],
    milestone_index: int,
    milestone_count: int,
    preflight: dict[str, Any],
    verification: dict[str, Any],
    changed_files: list[str],
    attempt: int,
    budget: int,
) -> str:
    recovery_guidance = _prd_verifier_recovery_guidance(verification, preflight)
    verifier_lines = [
        f"- status: {verification.get('status') or 'failed'}",
        f"- command: {verification.get('command') or 'not available'}",
        f"- working_directory: {verification.get('cwd') or '.'}",
        f"- exit_code: {verification.get('exit_code')}",
        f"- files: {', '.join(verification.get('files') or []) or 'none'}",
        f"- summary: {verification.get('summary') or 'Verification failed.'}",
    ]
    if recovery_guidance:
        verifier_lines.append(f"- required_recovery: {recovery_guidance}")
    changed = ", ".join(changed_files) if changed_files else "none recorded yet"
    return (
        f"Repair PRD milestone {milestone_index}/{milestone_count}: {milestones[milestone_index - 1]}\n"
        f"Repair attempt {attempt}/{budget} for milestone {preflight.get('milestone_id') or ''}.\n\n"
        "The milestone verifier failed. Fix the current milestone only, preserve every completed "
        "milestone, and do not ask the user unless a real blocker prevents progress.\n\n"
        f"## Project\n{title}\n\n"
        f"## PRD source reference\n{relative_path.as_posix()} (already extracted by the harness)\n\n"
        f"## PRD brief\n{prd_brief}\n\n"
        f"{render_preflight_context(preflight)}\n\n"
        "## Failed verifier\n" + "\n".join(verifier_lines) + "\n\n"
        f"Changed files so far: {changed}\n\n"
        "Read the implicated files before editing. Make the smallest complete fix that satisfies "
        "the milestone requirements, then run the verifier or the closest focused check before "
        "finishing. Report only what changed and the verification result."
    )


def _prd_verifier_recovery_guidance(
    verification: dict[str, Any],
    preflight: dict[str, Any] | None = None,
) -> str:
    """Translate deterministic verifier failures into exact safe recovery actions."""
    command = str(verification.get("command") or "")
    cwd = str(verification.get("cwd") or ".")
    summary = str(verification.get("summary") or "")
    if summary.startswith("Requirement evidence validation failed:"):
        kinds = {
            str(item.get("kind") or "").lower()
            for item in (preflight or {}).get("requirements") or []
            if isinstance(item, dict)
        }
        expected = _preflight_expected_files(preflight or {})
        models = [path for path in expected if path.replace("\\", "/").endswith("/models.py")]
        urls = [path for path in expected if path.replace("\\", "/").endswith("/urls.py")]
        targets: list[str] = []
        if "role" in kinds:
            targets.extend(models[:1])
        if kinds & {"auth", "authorization"}:
            app_root = Path(models[0]).parent.as_posix() if models else "backend/core"
            targets.extend(
                [
                    f"{app_root}/views.py",
                    f"{app_root}/urls.py",
                    f"{app_root}/tests/__init__.py",
                    f"{app_root}/tests/test_auth.py",
                    *urls[:1],
                ]
            )
        return (
            "This is a semantic requirement failure, not a migration failure. Do not rerun "
            "makemigrations until source behavior changes. Read and edit the smallest relevant "
            "targets, then add focused tests: "
            + ", ".join(dict.fromkeys(targets))
            + ". Fix every listed evidence error before running Django tests."
        )
    if "manage.py makemigrations --check --dry-run" in command:
        guidance = (
            f"From `{cwd}`, call run_command with `python manage.py makemigrations` to generate "
            "the missing migration files. Do not edit settings to suppress migration warnings; "
            "then rerun `python manage.py makemigrations --check --dry-run`."
        )
        required = {
            entity.lower(): set(fields)
            for entity, fields in _prd_required_entity_fields(preflight or {})
        }
        additions = re.findall(
            r"Add field\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+to\s+(?P<entity>[A-Za-z_][A-Za-z0-9_]*)",
            summary,
            re.IGNORECASE,
        )
        unexpected = [
            f"{entity}.{field}"
            for field, entity in additions
            if entity.lower() in required and field not in required[entity.lower()]
        ]
        expected_new = [
            f"{entity}.{field}"
            for field, entity in additions
            if entity.lower() in required and field in required[entity.lower()]
        ]
        if unexpected:
            guidance += (
                " Remove these out-of-contract model fields instead of migrating them: "
                + ", ".join(unexpected)
                + "."
            )
        if expected_new:
            guidance += (
                " Required new fields must use a migration-safe deterministic default or an "
                "appropriate nullable/blank declaration so makemigrations never asks for input: "
                + ", ".join(expected_new)
                + "."
            )
        return guidance
    return ""


def _prd_migration_source_repair_files(
    verification: dict[str, Any],
    preflight: dict[str, Any],
    workspace: Path,
) -> list[str]:
    """Focus migration repair on models when Django proposes out-of-contract fields."""
    if "manage.py makemigrations --check --dry-run" not in str(
        verification.get("command") or ""
    ):
        return []
    if not _prd_migration_source_edit_recipes(verification, preflight, workspace):
        return []
    expected = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    return [path for path in expected if path.lower().replace("\\", "/").endswith("/models.py")]


def _prd_semantic_source_repair_files(
    verification: dict[str, Any],
    preflight: dict[str, Any],
    workspace: Path,
) -> list[str]:
    if not _prd_semantic_source_edit_recipes(verification, preflight, workspace):
        return []
    expected = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    return [path for path in expected if path.lower().replace("\\", "/").endswith("/models.py")]


def _prd_runtime_exception_diagnostic(
    verification: dict[str, Any],
    workspace: Path,
) -> tuple[str, int, str, str] | None:
    """Recover ``(relative_path, line, exception_type, message)`` from a failed
    verifier's traceback text.

    The verifier summary is often front-truncated, so the ``Traceback (most
    recent call last):`` header the standard parser anchors on may be gone.
    That anchor exists to stop arbitrary log lines being read as exceptions;
    here the text is already known to be a FAILED command's output, so the
    last workspace frame plus the final exception line are trustworthy.
    """
    from shamsu.diagnostics.parsers.python_fallback import (
        FINAL_EXCEPTION_RE,
        FRAME_RE,
        VENDOR_MARKERS,
    )

    summary = str(verification.get("summary") or "")
    if not summary.strip():
        return None
    frame: tuple[str, int] | None = None
    exception: tuple[str, str] | None = None
    for raw_line in summary.splitlines():
        frame_match = FRAME_RE.match(raw_line)
        if frame_match:
            path = frame_match.group("file")
            normalized = path.replace("\\", "/")
            if any(marker.replace("\\", "/") in normalized for marker in VENDOR_MARKERS):
                continue
            relatives = _workspace_relative_paths([path], workspace)
            if relatives and (workspace / relatives[0]).is_file():
                frame = (relatives[0], int(frame_match.group("line")))
            continue
        exception_match = FINAL_EXCEPTION_RE.match(raw_line.strip())
        if exception_match:
            exception = (
                exception_match.group("etype"),
                exception_match.group("message").strip(),
            )
    if frame is None or exception is None:
        return None
    return frame[0], frame[1], exception[0], exception[1]


_DJANGO_CHECK_ERROR_RE = re.compile(
    r"^\s*(?P<app>[A-Za-z_]\w*)\.(?P<model>[A-Za-z_]\w*)(?:\.(?P<field>[A-Za-z_]\w*))?:\s*"
    r"\((?P<code>[a-z_]+\.[EW]\d{3})\)\s*(?P<message>.+?)\s*$"
)


def _prd_django_check_diagnostic(
    verification: dict[str, Any],
    preflight: dict[str, Any],
    workspace: Path,
) -> tuple[str, list[str]] | None:
    """Recover ``(models_path, errors)`` from a Django ``manage.py check`` failure.

    Django reports these as ``core.Grade.graded_by: (fields.E300) ...`` - no
    file, no line, so the traceback parser skips them entirely and the repair
    turn got no root cause and no forced edit. That is exactly how run 11 died
    with a fully machine-readable error in hand ("no structured diagnostics
    were extracted").
    """
    summary = str(verification.get("summary") or "")
    if not summary.strip():
        return None
    found: dict[str, list[str]] = {}
    for line in summary.splitlines():
        match = _DJANGO_CHECK_ERROR_RE.match(line)
        if not match:
            continue
        # Errors only - a (fields.W042) warning does not fail the check.
        if ".E" not in match.group("code"):
            continue
        app = match.group("app")
        detail = (
            f"{match.group('model')}"
            + (f".{match.group('field')}" if match.group("field") else "")
            + f" ({match.group('code')}): {match.group('message')}"
        )
        found.setdefault(app, []).append(detail)
    if not found:
        return None
    expected = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    for app, errors in found.items():
        for path in expected:
            parts = Path(path).parts
            if len(parts) >= 2 and parts[-1] == "models.py" and parts[-2] == app:
                if (workspace / path).is_file():
                    return path, sorted(dict.fromkeys(errors))
    return None


def _prd_django_check_repair_files(
    verification: dict[str, Any],
    preflight: dict[str, Any],
    workspace: Path,
) -> list[str]:
    diagnostic = _prd_django_check_diagnostic(verification, preflight, workspace)
    return [diagnostic[0]] if diagnostic else []


def _prd_django_check_edit_guidance(
    verification: dict[str, Any],
    preflight: dict[str, Any],
    workspace: Path,
) -> str:
    """Turn Django's own check errors into an exact, editable instruction."""
    diagnostic = _prd_django_check_diagnostic(verification, preflight, workspace)
    if diagnostic is None:
        return ""
    path, errors = diagnostic
    lines = [
        "## Harness-parsed Django check errors",
        f"- file: {path}",
        *(f"- {error}" for error in errors),
        "",
        "These are model definition errors in the file above, not missing files. Do NOT "
        "rewrite the file and do NOT reprint it - a full reprint gets truncated. Your "
        "FIRST response must call edit_file on that exact path with the smallest unique "
        "old_string that anchors each broken field.",
    ]
    if any("E300" in error or "E307" in error for error in errors):
        lines.append(
            "A relation points at a model that does not exist. Point it at a model that "
            "IS defined in this file - if the missing name is a role (Student, Teacher, "
            "Instructor), the correct target is the User model, which carries a role field."
        )
    return "\n".join(lines)


def _prd_runtime_exception_repair_files(
    verification: dict[str, Any],
    workspace: Path,
) -> list[str]:
    """Scope repair to the one source file a runtime exception points at."""
    diagnostic = _prd_runtime_exception_diagnostic(verification, workspace)
    return [diagnostic[0]] if diagnostic else []


def _prd_runtime_exception_edit_guidance(
    verification: dict[str, Any],
    workspace: Path,
) -> str:
    """Hand the model the exact defect and force a minimal edit.

    Live 2026-08-01: given only a raw traceback, a 7B model answered a
    one-line NameError by reprinting all 60 lines of settings.py - which the
    response limit truncated, losing code and producing no usable mutation. The
    parsed root cause plus an explicit "edit, do not reprint" instruction is
    what turns that into a single applicable edit.
    """
    diagnostic = _prd_runtime_exception_diagnostic(verification, workspace)
    if diagnostic is None:
        return ""
    path, line, code, message = diagnostic
    lines = [
        "## Harness-parsed root cause",
        f"- file: {path}",
        f"- line: {line}",
        f"- error: {code}: {message}",
        "",
        "This is a single runtime defect in an existing file, not a missing file. Do NOT "
        "rewrite the file and do NOT reprint it in a code fence - a full reprint gets "
        "truncated and loses code. Your FIRST response must call edit_file on the exact "
        "path above, with the smallest unique old_string that anchors the defect and a "
        "new_string that fixes it. Leave every other line unchanged.",
    ]
    undefined = re.search(r"name '([^']+)' is not defined", message)
    if code == "NameError" and undefined:
        symbol = undefined.group(1)
        lines.append(
            f"`{symbol}` is used but never defined. Define it ABOVE its first use: anchor "
            f"old_string on the existing line that uses `{symbol}` (or the import block at "
            f"the top) and put the definition plus that same line in new_string."
        )
    return "\n".join(lines)


def _prd_semantic_repair_source_context(
    preflight: dict[str, Any],
    workspace: Path,
) -> str:
    """Give a small model exact behavior-file evidence before forcing its first write."""
    expected = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    model_paths = [path for path in expected if path.replace("\\", "/").endswith("/models.py")]
    root_paths = _workspace_relative_paths(
        [str(preflight.get("project_root") or ".")], workspace
    )
    root = workspace / (root_paths[0] if root_paths else ".")
    candidates: list[str] = []
    for models_path in model_paths[:1]:
        app_root = Path(models_path).parent.as_posix()
        candidates.extend(
            [
                models_path,
                    f"{app_root}/views.py",
                    f"{app_root}/urls.py",
                    f"{app_root}/tests/__init__.py",
                    f"{app_root}/tests/test_auth.py",
            ]
        )
    candidates.extend(
        path for path in expected if path.replace("\\", "/").endswith("/config/urls.py")
    )
    if root.is_dir():
        for path in root.rglob("test*.py"):
            if any(part in {".venv", "node_modules", "__pycache__"} for part in path.parts):
                continue
            try:
                candidates.append(path.relative_to(workspace).as_posix())
            except ValueError:
                continue
    sections = [
        "## Harness-provided semantic repair files",
        "Do not list or search the project. The current source evidence is below. Your FIRST "
        "response must call write_file for one implicated behavior file with its complete "
        "corrected content; do not run a command first. Then wire routes, add focused tests, "
        "and run `python manage.py test` only after all required writes succeed.",
    ]
    for relative in list(dict.fromkeys(candidates))[:8]:
        target = workspace / relative
        if not target.is_file():
            sections.append(f"\n### {relative}\n<MISSING - create this file if required>")
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            sections.append(f"\n### {relative}\n<UNREADABLE>")
            continue
        if len(content) > 5000:
            content = content[:5000] + "\n... [truncated by harness]"
        sections.append(f"\n### {relative}\n```\n{content.rstrip()}\n```")
    return "\n".join(sections)


def _prd_semantic_source_edit_guidance(
    verification: dict[str, Any],
    preflight: dict[str, Any],
    workspace: Path,
) -> str:
    recipes = _prd_semantic_source_edit_recipes(verification, preflight, workspace)
    if not recipes:
        return ""
    return (
        "Deterministic semantic-source repair: your next responses must call these edit_file "
        "tools in order without changing their arguments. Do not run framework commands until "
        "every edit succeeds:\n" + "\n".join(json.dumps(recipe) for recipe in recipes)
    )


def _prd_semantic_source_edit_recipes(
    verification: dict[str, Any],
    preflight: dict[str, Any],
    workspace: Path,
) -> list[dict[str, Any]]:
    summary = str(verification.get("summary") or "")
    marker = "roles have no executable source declarations:"
    if marker not in summary:
        return []
    required_roles = [
        role.strip().lower()
        for role in summary.split(marker, 1)[1].split(";", 1)[0].split(",")
        if role.strip()
    ]
    if not required_roles:
        return []
    expected = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    model_paths = [
        path for path in expected if path.lower().replace("\\", "/").endswith("/models.py")
    ]
    recipes: list[dict[str, Any]] = []
    for path in model_paths:
        target = workspace / path
        try:
            content = target.read_text(encoding="utf-8")
            module = ast.parse(content)
        except (OSError, SyntaxError):
            continue
        lines = content.splitlines(keepends=True)
        user_class = next(
            (
                node
                for node in module.body
                if isinstance(node, ast.ClassDef) and node.name.lower() == "user"
            ),
            None,
        )
        if user_class is None:
            continue
        assignment = next(
            (
                node
                for node in user_class.body
                if isinstance(node, ast.Assign)
                and any(isinstance(item, ast.Name) and item.id == "role" for item in node.targets)
                and isinstance(node.value, ast.Call)
            ),
            None,
        )
        if assignment is None:
            continue
        start = assignment.lineno - 1
        end = int(getattr(assignment, "end_lineno", assignment.lineno))
        field_string = "".join(lines[start:end])
        stripped = field_string.rstrip("\r\n")
        ending = field_string[len(stripped) :]
        choices_keyword = next(
            (keyword for keyword in assignment.value.keywords if keyword.arg == "choices"),
            None,
        )
        existing_roles: list[str] = []
        if choices_keyword is not None:
            if not isinstance(choices_keyword.value, (ast.List, ast.Tuple)):
                continue
            for item in choices_keyword.value.elts:
                if not isinstance(item, (ast.List, ast.Tuple)) or not item.elts:
                    continue
                value = item.elts[0]
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    existing_roles.append(value.value.lower())
        all_roles = list(dict.fromkeys([*existing_roles, *required_roles]))
        choices = "[" + ", ".join(
            f"('{role}', '{role.title()}')" for role in all_roles
        ) + "]"
        if choices_keyword is None:
            replacement = re.sub(r"\)\s*$", f", choices={choices})", stripped) + ending
        else:
            old_choices = ast.get_source_segment(content, choices_keyword.value)
            if not old_choices:
                continue
            replacement = stripped.replace(old_choices, choices, 1) + ending
        context_start = user_class.lineno - 1
        recipes.append(
            {
                "name": "edit_file",
                "arguments": {
                    "filepath": path,
                    "old_string": "".join(lines[context_start:end]),
                    "new_string": "".join(lines[context_start:start]) + replacement,
                },
            }
        )
    return recipes


def _prd_migration_source_edit_guidance(
    verification: dict[str, Any],
    preflight: dict[str, Any],
    workspace: Path,
) -> str:
    recipes = _prd_migration_source_edit_recipes(verification, preflight, workspace)
    if not recipes:
        return ""
    return (
        "Deterministic migration-source repair: your next responses must call these edit_file "
        "tools in order without changing their arguments. Do not run makemigrations until every "
        "edit succeeds:\n" + "\n".join(json.dumps(recipe) for recipe in recipes)
    )


def _prd_migration_source_edit_recipes(
    verification: dict[str, Any],
    preflight: dict[str, Any],
    workspace: Path,
) -> list[dict[str, Any]]:
    required = {
        entity.lower(): set(fields)
        for entity, fields in _prd_required_entity_fields(preflight)
    }
    additions = re.findall(
        r"Add field\s+(?P<field>[A-Za-z_][A-Za-z0-9_]*)\s+to\s+(?P<entity>[A-Za-z_][A-Za-z0-9_]*)",
        str(verification.get("summary") or ""),
        re.IGNORECASE,
    )
    if not additions:
        return []
    expected = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    model_paths = [
        path for path in expected if path.lower().replace("\\", "/").endswith("/models.py")
    ]
    recipes: list[dict[str, Any]] = []
    for path in model_paths:
        target = workspace / path
        try:
            content = target.read_text(encoding="utf-8")
            module = ast.parse(content)
        except (OSError, SyntaxError):
            continue
        lines = content.splitlines(keepends=True)
        classes = {
            node.name.lower(): node for node in module.body if isinstance(node, ast.ClassDef)
        }
        for field, entity in additions:
            class_node = classes.get(entity.lower())
            if class_node is None:
                continue
            assignment = next(
                (
                    node
                    for node in class_node.body
                    if isinstance(node, ast.Assign)
                    and any(isinstance(item, ast.Name) and item.id == field for item in node.targets)
                ),
                None,
            )
            if assignment is None or not getattr(assignment, "lineno", None):
                continue
            start = int(assignment.lineno) - 1
            end = int(getattr(assignment, "end_lineno", assignment.lineno))
            field_string = "".join(lines[start:end])
            if field not in required.get(entity.lower(), set()):
                replacement = ""
            else:
                value = assignment.value
                if not isinstance(value, ast.Call):
                    continue
                keywords = {item.arg for item in value.keywords if item.arg}
                if keywords & {"default", "null"}:
                    continue
                call_name = value.func.attr if isinstance(value.func, ast.Attribute) else ""
                default = "''" if call_name in {"CharField", "TextField", "EmailField"} else None
                if default is None:
                    continue
                stripped = field_string.rstrip("\r\n")
                ending = field_string[len(stripped) :]
                replacement = re.sub(
                    r"\)\s*$", f", default={default})", stripped
                ) + ending
            context_start = int(class_node.lineno) - 1
            old_string = "".join(lines[context_start:end])
            new_string = "".join(lines[context_start:start]) + replacement
            recipes.append(
                {
                    "name": "edit_file",
                    "arguments": {
                        "filepath": path,
                        "old_string": old_string,
                        "new_string": new_string,
                    },
                }
            )
    return recipes


def _prd_transaction_snapshot(workspace: Path) -> set[str]:
    try:
        return set(TransactionWorkspace(workspace).list_transaction_ids())
    except Exception:
        return set()


def _prd_tool_snapshot(workspace: Path) -> int:
    ledger = get_current_run()
    if ledger is None:
        return 0
    return len(action_ledger_store.load_tool_calls(workspace, ledger.run_id))


def _prd_unrecovered_command_failures(
    workspace: Path,
    before_tools: int,
) -> list[dict[str, Any]]:
    """Return commands whose latest milestone-local result still failed."""
    ledger = get_current_run()
    if ledger is None:
        return []
    records = action_ledger_store.load_tool_calls(workspace, ledger.run_id)[before_tools:]
    return _prd_unrecovered_command_failures_from_records(records)


def _prd_unrecovered_command_failures_from_records(
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return latest command failures that were not made stale by a later edit."""
    arguments = {
        str(record.get("tool_call_id") or ""): record.get("arguments") or {}
        for record in records
        if record.get("phase") == "called"
    }
    latest: dict[str, tuple[int, dict[str, Any]]] = {}
    order: list[str] = []
    latest_mutation_index = -1
    for index, record in enumerate(records):
        if (
            record.get("phase") == "finished"
            and bool(record.get("ok"))
            and record.get("tool") in {"edit_file", "write_file", "append_file", "apply_patch"}
        ):
            latest_mutation_index = index
        if record.get("tool") != "run_command" or record.get("phase") != "finished":
            continue
        call_args = arguments.get(str(record.get("tool_call_id") or ""), {})
        command = str(call_args.get("command") or "").strip()
        if not command:
            continue
        if command not in latest:
            order.append(command)
        latest[command] = (index, record)
    return [
        record
        for command in order
        for index, record in [latest[command]]
        if not bool(record.get("ok")) and index > latest_mutation_index
    ]


def _prd_command_failure_verification(
    failures: list[dict[str, Any]],
) -> dict[str, Any]:
    primary = failures[0]
    data = primary.get("data") if isinstance(primary.get("data"), dict) else {}
    command = str(data.get("resolved_command") or "run_command")
    outputs = [
        ("stdout", str(data.get("stdout") or "").strip()),
        ("stderr", str(data.get("stderr") or "").strip()),
        ("diagnostics", str(data.get("diagnostics") or "").strip()),
    ]
    diagnostic = "\n".join(
        f"{label}:\n{value}" for label, value in outputs if value
    ).strip()[:2400]
    if not diagnostic:
        diagnostic = str(primary.get("message") or "").strip()[:2400]
    summary = f"Milestone command failed and was not recovered: {command}."
    if diagnostic:
        summary += f"\n{diagnostic}"
    if len(failures) > 1:
        summary += f"\n{len(failures) - 1} additional command failure(s) remain."
    return _milestone_verification_payload(
        "failed",
        files=[],
        summary=summary,
        exit_code=int(data.get("exit_code", 1) or 1),
        command=command,
    )


def _prd_transactions_since(workspace: Path, before: set[str]) -> list[str]:
    try:
        store = TransactionWorkspace(workspace)
        candidates: list[tuple[str, str]] = []
        for transaction_id in store.list_transaction_ids():
            if transaction_id in before:
                continue
            manifest = store.load_manifest(transaction_id) or {}
            if manifest.get("status") == "rolled_back":
                continue
            created_at = str(manifest.get("created_at") or "")
            candidates.append((created_at, transaction_id))
        return [transaction_id for _created_at, transaction_id in sorted(candidates)]
    except Exception:
        return []


def _rollback_failed_prd_milestone(
    root: Path,
    state: dict[str, Any],
    milestone_id: str,
    preflight: dict[str, Any],
    transaction_ids: list[str],
    workspace: Path,
    console: Console,
    *,
    preserved_changed_files: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    policy = str(preflight.get("rollback_policy") or "")
    if not _prd_milestone_rollback_enabled(preflight):
        return state, {
            "attempted": False,
            "status": "disabled",
            "policy": policy,
            "transaction_ids": list(transaction_ids),
            "message": "Milestone rollback policy did not request rollback.",
        }
    if not transaction_ids:
        return state, {
            "attempted": False,
            "status": "no_transactions",
            "policy": policy,
            "transaction_ids": [],
            "message": "No mutation transactions were recorded for this failed milestone.",
        }

    ledger = get_current_run()
    if ledger:
        ledger.log_event(
            "prd_milestone_rollback_started",
            milestone_id=milestone_id,
            transaction_ids=list(transaction_ids),
            policy=policy,
        )
    state = record_milestone_rollback(
        root,
        state,
        milestone_id,
        phase="started",
        status="rolling_back",
        transaction_ids=transaction_ids,
        policy=policy,
        message="Rolling back failed milestone transactions.",
    )
    store = TransactionWorkspace(workspace)
    restored_files: list[str] = []
    failed_transactions: list[dict[str, Any]] = []
    for transaction_id in reversed(transaction_ids):
        manifest = store.load_manifest(transaction_id) or {}
        touched = [str(path) for path in list(manifest.get("touched_files") or []) if str(path)]
        ok, message = rollback_transaction(workspace, transaction_id)
        if ledger:
            ledger.log_rollback(transaction_id, ok, message)
        if ok:
            restored_files.extend(touched)
        else:
            failed_transactions.append({"transaction_id": transaction_id, "message": message})

    status = "rolled_back" if not failed_transactions else "rollback_failed"
    message = (
        f"Rolled back {len(transaction_ids)} failed milestone transaction(s)."
        if status == "rolled_back"
        else f"Rollback failed for {len(failed_transactions)} transaction(s)."
    )
    state = record_milestone_rollback(
        root,
        state,
        milestone_id,
        phase="finished",
        status=status,
        transaction_ids=transaction_ids,
        restored_files=restored_files,
        failed_transactions=failed_transactions,
        policy=policy,
        message=message,
        preserved_changed_files=preserved_changed_files,
    )
    if ledger:
        ledger.log_event(
            "prd_milestone_rollback_finished",
            milestone_id=milestone_id,
            status=status,
            transaction_ids=list(transaction_ids),
            restored_files=list(dict.fromkeys(restored_files)),
            failed_transactions=failed_transactions,
        )
    border = "yellow" if status == "rolled_back" else "red"
    console.print(Panel(message, title=f"Milestone {milestone_id} rollback", border_style=border))
    return state, {
        "attempted": True,
        "status": status,
        "policy": policy,
        "transaction_ids": list(transaction_ids),
        "restored_files": list(dict.fromkeys(restored_files)),
        "failed_transactions": failed_transactions,
        "message": message,
    }


def _prd_checkpoint_changed_after_rollback(
    changed_files: list[str],
    rollback_result: dict[str, Any],
) -> list[str]:
    if rollback_result.get("attempted") and rollback_result.get("status") == "rolled_back":
        return []
    return list(dict.fromkeys(changed_files))


def _prd_rollback_evidence(rollback_result: dict[str, Any]) -> list[str]:
    if not rollback_result:
        return []
    status = str(rollback_result.get("status") or "")
    if status in {"disabled", "no_transactions"}:
        return [f"rollback:{status}"]
    transaction_ids = list(rollback_result.get("transaction_ids") or [])
    suffix = ",".join(str(item) for item in transaction_ids[:6])
    if len(transaction_ids) > 6:
        suffix += f",+{len(transaction_ids) - 6}"
    return [f"rollback:{status}:{suffix}" if suffix else f"rollback:{status}"]


def _milestone_verifier_files(
    preflight: dict[str, Any],
    changed_files: list[str],
    workspace: Path,
) -> list[str]:
    expected = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    existing = [path for path in expected if (workspace / path).is_file()]
    changed = [
        path
        for path in _workspace_relative_paths(changed_files, workspace)
        if (workspace / path).is_file()
    ]
    if existing or changed:
        # Expected architecture goes first so a one-file resumed repair cannot
        # redefine a nested project's verification root.
        return list(dict.fromkeys([*existing, *changed]))
    return _project_verifier_entry_files(preflight, workspace)


def _unexpected_prd_entrypoint_changes(
    preflight: dict[str, Any],
    changed_files: list[str],
    workspace: Path,
) -> list[str]:
    """Reject repair-created duplicate framework entry points at the wrong depth."""
    expected = set(_workspace_relative_paths(_preflight_expected_files(preflight), workspace))
    expected_manage = {path for path in expected if Path(path).name == "manage.py"}
    if not expected_manage:
        return []
    changed = _workspace_relative_paths(changed_files, workspace)
    return [
        f"{path} (unexpected duplicate Django entry point; expected {sorted(expected_manage)[0]})"
        for path in changed
        if Path(path).name == "manage.py" and path not in expected_manage
    ]


def _project_verifier_entry_files(preflight: dict[str, Any], workspace: Path) -> list[str]:
    root_text = str(preflight.get("project_root") or ".")
    roots = _workspace_relative_paths([root_text], workspace)
    root = workspace / (roots[0] if roots else ".")
    if not root.is_dir():
        return []
    names = {
        "package.json",
        "pyproject.toml",
        "requirements.txt",
        "manage.py",
        "Cargo.toml",
        "go.mod",
    }
    found: list[str] = []
    for path in root.rglob("*"):
        if len(found) >= 32:
            break
        if not path.is_file() or path.name not in names:
            continue
        try:
            relative = path.relative_to(workspace).as_posix()
        except ValueError:
            continue
        if any(part in {"node_modules", ".venv", ".git"} for part in path.parts):
            continue
        found.append(relative)
    return list(dict.fromkeys(found))


def _prd_blocking_dependencies(
    preflight: dict[str, Any],
    failed: dict[str, str],
    skipped: dict[str, str],
) -> list[str]:
    """Declared dependencies of this milestone that failed or were skipped.

    Empty means the milestone can run: a failure elsewhere in the build must
    not stop work that does not depend on it.
    """
    if not isinstance(preflight, dict):
        return []
    unavailable = {*failed, *skipped}
    return [
        str(dependency)
        for dependency in preflight.get("dependencies") or []
        if str(dependency) in unavailable
    ]


def _prd_build_completion_report(
    milestones: list[str],
    failed: dict[str, str],
    skipped: dict[str, str],
) -> str:
    """Per-milestone outcome for the end of a build that had failures."""
    lines = [
        f"PRD build finished: {len(milestones) - len(failed) - len(skipped)}"
        f"/{len(milestones)} milestone(s) completed."
    ]
    if failed:
        lines.append("")
        lines.append("Failed:")
        lines.extend(f"- {name}: {reason}" for name, reason in failed.items())
    if skipped:
        lines.append("")
        lines.append("Skipped (blocked by a failed dependency):")
        lines.extend(f"- {name}: {reason}" for name, reason in skipped.items())
    return "\n".join(lines)


def _prd_milestone_is_mandatory(preflight: dict[str, Any]) -> bool:
    return any(
        str(item.get("scope") or "in") == "in"
        and str(item.get("priority") or "must") == "must"
        for item in preflight.get("requirements") or []
        if isinstance(item, dict)
    )


def _prd_milestone_requires_mutation(preflight: dict[str, Any]) -> bool:
    non_mutating_kinds = {"acceptance", "test", "out_of_scope"}
    return any(
        str(item.get("scope") or "in") == "in"
        and str(item.get("priority") or "must") == "must"
        and str(item.get("kind") or "") not in non_mutating_kinds
        for item in preflight.get("requirements") or []
        if isinstance(item, dict)
    )


def _preflight_expected_files(preflight: dict[str, Any]) -> list[str]:
    values = preflight.get("expected_files") if isinstance(preflight, dict) else []
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(str(path) for path in values if str(path)))


def _missing_expected_files(expected_files: list[str], workspace: Path) -> list[str]:
    return [
        path
        for path in _workspace_relative_paths(expected_files, workspace)
        if not (workspace / path).is_file()
    ]


def _invalid_expected_architecture_files(
    preflight: dict[str, Any],
    workspace: Path,
) -> list[str]:
    """Reject files that exist only to make framework commands exit zero."""
    invalid: list[str] = []
    entity_milestone = any(
        isinstance(item, dict) and str(item.get("kind") or "") == "entity"
        for item in preflight.get("requirements") or []
    )
    expected_paths = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    settings_modules = {
        ".".join(Path(path).with_suffix("").parts[-2:])
        for path in expected_paths
        if path.lower().replace("\\", "/").endswith("/settings.py")
    }
    url_modules = {
        ".".join(Path(path).with_suffix("").parts[-2:])
        for path in expected_paths
        if path.lower().replace("\\", "/").endswith("/urls.py")
    }
    app_modules = {
        Path(path).parent.name
        for path in expected_paths
        if path.lower().replace("\\", "/").endswith("/apps.py")
    }
    custom_user_models = _django_custom_user_models(preflight, workspace)
    app_init_paths = {
        (Path(path).parent / "__init__.py").as_posix()
        for path in expected_paths
        if path.lower().replace("\\", "/").endswith("/apps.py")
    }
    for relative in expected_paths:
        target = workspace / relative
        if not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            invalid.append(f"{relative} (unreadable)")
            continue
        stripped = content.strip()
        if target.name == "__init__.py":
            if relative in app_init_paths and re.search(
                r"(?m)^\s*(?:from\s+\.models\s+import|from\s+\.\s+import\s+models|import\s+.*\bmodels\b)",
                content,
            ):
                invalid.append(f"{relative} (imports Django models during app loading)")
            continue
        if not stripped:
            invalid.append(f"{relative} (empty)")
            continue
        posix = relative.lower().replace("\\", "/")
        if target.suffix.lower() == ".py":
            try:
                module = ast.parse(content)
            except SyntaxError:
                invalid.append(f"{relative} (invalid Python)")
                continue
            if not module.body:
                invalid.append(f"{relative} (no executable declarations)")
                continue
        if posix.endswith("/manage.py"):
            if "execute_from_command_line" not in content:
                invalid.append(f"{relative} (not a Django entry point)")
            elif settings_modules:
                module_match = re.search(
                    r"DJANGO_SETTINGS_MODULE['\"]\s*,\s*['\"](?P<module>[^'\"]+)['\"]",
                    content,
                )
                if not module_match or module_match.group("module") not in settings_modules:
                    invalid.append(f"{relative} (wrong Django settings module)")
        elif posix.endswith("/settings.py"):
            settings_errors: list[str] = []
            if not all(
                token in content for token in ("INSTALLED_APPS", "DATABASES", "SECRET_KEY")
            ):
                settings_errors.append("incomplete Django settings")
            if url_modules:
                root_match = re.search(
                    r"ROOT_URLCONF\s*=\s*['\"](?P<module>[^'\"]+)['\"]",
                    content,
                )
                if not root_match or root_match.group("module") not in url_modules:
                    settings_errors.append("wrong ROOT_URLCONF module")
            if app_modules and not all(
                re.search(rf"['\"]{re.escape(module)}(?:\.apps\.[A-Za-z0-9_]+)?['\"]", content)
                for module in app_modules
            ):
                settings_errors.append("required Django app is not installed")
            if custom_user_models:
                auth_match = re.search(
                    r"AUTH_USER_MODEL\s*=\s*['\"](?P<model>[^'\"]+)['\"]",
                    content,
                )
                if not auth_match or auth_match.group("model") not in custom_user_models:
                    settings_errors.append("custom Django user model is not configured")
            wsgi_match = re.search(
                r"WSGI_APPLICATION\s*=\s*['\"](?P<module>[A-Za-z0-9_.]+)['\"]",
                content,
            )
            if wsgi_match:
                module = wsgi_match.group("module").removesuffix(".application")
                project_root = target.parent.parent
                module_path = project_root.joinpath(*module.split(".")).with_suffix(".py")
                if not module_path.is_file():
                    settings_errors.append("WSGI module does not exist")
            if settings_errors:
                invalid.append(f"{relative} ({'; '.join(settings_errors)})")
        elif posix.endswith("/urls.py"):
            url_errors: list[str] = []
            if "urlpatterns" not in content:
                url_errors.append("no URL patterns")
            django_root = target.parent.parent
            for included in re.findall(r"include\(\s*['\"](?P<module>[^'\"]+)['\"]", content):
                module_base = django_root.joinpath(*included.split("."))
                if not (
                    module_base.with_suffix(".py").is_file()
                    or (module_base / "__init__.py").is_file()
                ):
                    url_errors.append(f"included URL module {included} does not exist")
            if url_errors:
                invalid.append(f"{relative} ({'; '.join(url_errors)})")
        elif posix.endswith("/apps.py") and "AppConfig" not in content:
            invalid.append(f"{relative} (no Django app config)")
        elif entity_milestone and posix.endswith("/models.py") and not (
            "class " in content and ("models.Model" in content or "AbstractUser" in content)
        ):
            invalid.append(f"{relative} (no persisted model declarations)")
        elif entity_milestone and posix.endswith("/models.py"):
            model_errors = _django_model_structure_errors(content)
            if model_errors:
                invalid.append(f"{relative} ({'; '.join(model_errors)})")
        elif target.name == "package.json":
            try:
                package = json.loads(content)
            except json.JSONDecodeError:
                invalid.append(f"{relative} (invalid package JSON)")
            else:
                if not isinstance(package, dict) or not isinstance(package.get("scripts"), dict):
                    invalid.append(f"{relative} (missing package scripts)")
    return invalid


def _django_custom_user_models(preflight: dict[str, Any], workspace: Path) -> list[str]:
    """Discover custom Django user models declared by expected app model files."""
    models: list[str] = []
    expected_paths = _workspace_relative_paths(_preflight_expected_files(preflight), workspace)
    for relative in expected_paths:
        if not relative.lower().replace("\\", "/").endswith("/models.py"):
            continue
        target = workspace / relative
        if not target.is_file():
            continue
        try:
            content = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        app_name = target.parent.name
        for class_name in re.findall(
            r"(?m)^class\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\([^\n]*(?:AbstractUser|AbstractBaseUser)[^\n]*\)\s*:",
            content,
        ):
            models.append(f"{app_name}.{class_name}")
    return list(dict.fromkeys(models))


def _missing_django_entity_requirements(
    content: str,
    preflight: dict[str, Any],
) -> list[str]:
    """Return PRD entity classes/fields absent from a Django models module."""
    required = _prd_required_entity_fields(preflight)
    if not required:
        return []
    return _missing_django_entity_requirements_from_required(content, required)


def _prd_required_entity_fields(preflight: dict[str, Any]) -> list[tuple[str, list[str]]]:
    """Parse normalized ``Entity: fields ...`` requirement records."""
    required: list[tuple[str, list[str]]] = []
    for item in preflight.get("requirements") or []:
        if not isinstance(item, dict) or str(item.get("kind") or "") != "entity":
            continue
        text = str(item.get("text") or "").strip()
        match = re.match(
            r"^(?P<entity>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*fields?\s+(?P<fields>.+)$",
            text,
            re.IGNORECASE,
        )
        if not match:
            continue
        fields = [
            value.strip().split()[0]
            for value in re.split(r"[,;]", match.group("fields"))
            if value.strip()
        ]
        required.append((match.group("entity"), fields))
    return required


def _missing_django_entity_requirements_from_required(
    content: str,
    required: list[tuple[str, list[str]]],
) -> list[str]:
    try:
        module = ast.parse(content)
    except SyntaxError:
        return [entity for entity, _fields in required]
    declarations: dict[str, set[str]] = {}
    for node in module.body:
        if not isinstance(node, ast.ClassDef):
            continue
        fields: set[str] = set()
        for statement in node.body:
            if isinstance(statement, ast.Assign):
                fields.update(
                    target.id for target in statement.targets if isinstance(target, ast.Name)
                )
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                if isinstance(statement.target, ast.Name):
                    fields.add(statement.target.id)
        base_names = {
            base.id if isinstance(base, ast.Name) else base.attr
            for base in node.bases
            if isinstance(base, (ast.Name, ast.Attribute))
        }
        if "AbstractUser" in base_names:
            fields.update(
                {
                    "username",
                    "first_name",
                    "last_name",
                    "email",
                    "password",
                    "is_staff",
                    "is_active",
                    "date_joined",
                }
            )
        declarations[node.name.lower()] = fields
    missing: list[str] = []
    for entity, fields in required:
        declared = declarations.get(entity.lower())
        if declared is None:
            missing.append(entity)
            continue
        absent = [field for field in fields if field not in declared]
        if absent:
            missing.append(f"{entity}." + "/".join(absent))
    return missing


def _workspace_relative_paths(paths: list[str], workspace: Path) -> list[str]:
    workspace = workspace.resolve()
    normalized: list[str] = []
    for path in paths:
        text = str(path).strip()
        if not text or not _safe_verifier_path_text(text):
            continue
        candidate = Path(text)
        try:
            absolute = (
                candidate.resolve()
                if candidate.is_absolute()
                else (workspace / candidate).resolve()
            )
            relative = absolute.relative_to(workspace)
        except (OSError, ValueError):
            continue
        normalized.append(relative.as_posix())
    return list(dict.fromkeys(normalized))


def _safe_verifier_path_text(path: str) -> bool:
    unsafe = set("\r\n;&|<>`$\"'")
    return not any(char in unsafe or char.isspace() for char in path)


def _milestone_stack(verifier_files: list[str], preflight: dict[str, Any]) -> str:
    stack = stack_of(verifier_files)
    if stack:
        return stack
    hint = _milestone_stack_hint(preflight)
    if any(token in hint for token in ("react", "vite", "node", "npm")):
        return "node"
    if any(token in hint for token in ("python", "django")):
        return "python"
    return ""


def _milestone_stack_hint(preflight: dict[str, Any]) -> str:
    if not isinstance(preflight, dict):
        return ""
    parts: list[str] = []
    verifier = str(preflight.get("verifier") or "")
    if verifier:
        parts.append(verifier)
    parts.extend(str(skill) for skill in preflight.get("active_skills") or [])
    parts.extend(str(path) for path in preflight.get("expected_files") or [])
    return " ".join(parts).lower()


# Runners a milestone-declared verifier may start. Everything else stays a
# descriptive hint, never an executed command - preflight text is
# model-authored and has contained things like "rm -rf".
_PRD_VERIFIER_RUNNER_RE = re.compile(
    r"^(?:python3?|py|pytest|npm|npx|node|pnpm|yarn|cargo|go|dotnet|java|mvn|gradle)\s+\S",
    re.IGNORECASE,
)
_PRD_VERIFIER_FILE_TOKEN_RE = re.compile(r"^[\w][\w./\\-]*\.[A-Za-z0-9]{1,12}$")


def _prd_milestone_acceptance_commands(
    preflight: dict[str, Any], workspace: Path
) -> list[str]:
    """Promote the milestone's declared verifier into a real verification step.

    The 2026-08-01 dogfood: milestones declared `python manage.py check`, but
    the verifier string was only ever a stack HINT, so verification reported
    "no deterministic verifier" while the declared one sat unused. Only a
    single-line, allowlisted runner command qualifies, and any file it invokes
    must already exist under the project root - early milestones run before
    the framework entry point is generated, and failing them on a command that
    cannot run yet would be a false verdict."""
    raw = str(preflight.get("verifier") or "").strip().strip("`").strip()
    if not raw or "\n" in raw or not _PRD_VERIFIER_RUNNER_RE.match(raw):
        return []
    if any(char in raw for char in (";", "&", "|", ">", "<", "$")):
        return []
    file_tokens = [
        token for token in raw.split()[1:] if _PRD_VERIFIER_FILE_TOKEN_RE.match(token)
    ]
    if file_tokens:
        roots = _workspace_relative_paths(
            [str(preflight.get("project_root") or ".")], workspace
        )
        root = workspace / (roots[0] if roots else ".")
        if not root.is_dir():
            root = workspace
        for token in file_tokens:
            name = Path(token.replace("\\", "/")).name
            found = any(
                not any(part in {"node_modules", ".venv", ".git"} for part in path.parts)
                for path in itertools.islice(root.rglob(name), 200)
            )
            if not found:
                return []
    return [raw]


def _milestone_verification_payload(
    status: str,
    *,
    files: list[str],
    summary: str,
    verified: bool = False,
    unverifiable: bool = False,
    exit_code: int | None = None,
    command: str = "",
    cwd: str = ".",
) -> dict[str, Any]:
    return {
        "status": status,
        "verified": verified,
        "unverifiable": unverifiable,
        "exit_code": exit_code,
        "command": command,
        "cwd": cwd,
        "files": list(dict.fromkeys(files)),
        "summary": summary,
    }


def _log_prd_milestone_verification(
    milestone_id: str,
    verification: dict[str, Any],
) -> None:
    ledger = get_current_run()
    if not ledger:
        return
    status = str(verification.get("status") or "unknown")
    command = str(verification.get("command") or "")
    files = list(verification.get("files") or [])
    summary = str(verification.get("summary") or "")[:500]
    verifier_id = f"prd_milestone:{milestone_id}"
    ledger.log_event(
        f"prd_milestone_verification_{status}",
        milestone_id=milestone_id,
        command=command,
        files=files,
        exit_code=verification.get("exit_code"),
        summary=summary,
    )
    if status not in {"verified", "failed"}:
        return
    ledger.log_verification_started(
        command,
        verifier_id=verifier_id,
        source="prd_milestone",
        required=True,
        files=files,
        milestone_id=milestone_id,
    )
    ledger.log_verification_result(
        status == "verified",
        summary,
        command=command,
        verifier_id=verifier_id,
        source="prd_milestone",
        required=True,
        files=files,
        exit_code=verification.get("exit_code"),
        milestone_id=milestone_id,
    )


def _create_plan_task(task: str, steps: list[str]) -> MilestoneTask:
    task_steps = [
        TaskStep(
            id=index + 1,
            description=step,
            type="file_edit",
            specialist="coder",
            phase=f"step-{index + 1}",
            depends_on=[index] if index else [],
        )
        for index, step in enumerate(steps)
    ]
    return create_task(user_request=task, steps=task_steps, phase="step-1")


def _plan_single_request(task: str, plan_markdown: str) -> str:
    return (
        "Execute the following approved plan in this workspace. Read the relevant files first, "
        "then make the changes with write_file/edit_file and verify with run_command when possible. "
        "Do not claim work you did not do.\n\n"
        f"## Original task\n{task}\n\n## Approved plan\n{plan_markdown}"
    )


def _plan_step_request(task: str, steps: list[str], index: int, count: int) -> str:
    """Build a step's request from a compact progress checklist instead of
    re-dumping the whole plan markdown every step (G10). ``index`` is 1-based."""
    checklist = render_progress_checklist(steps, index - 1, header="Plan steps")
    return (
        f"You are executing an approved plan, step {index} of {count}.\n\n"
        "Read any files you need first, then implement ONLY the current step by editing/creating "
        "files with write_file/edit_file. Keep earlier steps' work intact and the project runnable. "
        "Verify with run_command when possible. Do not claim work you did not do.\n\n"
        f"## Original task\n{task}\n\n"
        f"{checklist}"
    )


def _resolve_proceed(
    workspace: Path, console: Console, session_logger: SessionLogger | None
) -> bool:
    """Run the pending approved plan (the /proceed command). Returns False when
    there is no plan awaiting approval so the caller can tell the user."""
    if session_logger is None:
        return False
    pending = session_logger.get_pending_action()
    if pending.get("awaiting") not in {"plan_approval", "plan_continue"}:
        return False
    session_logger.clear_pending_action()
    ledger = start_run(workspace, "proceed", session_logger=session_logger)
    set_current_run(ledger)
    try:
        _run_request(
            _execute_pending_plan(
                pending,
                workspace,
                console,
                session_logger,
                run_all=is_long_running_enabled(workspace),
            )
        )
    except Exception as exc:
        ledger.fail(str(exc))
        clear_current_run()
        # True: there WAS a plan to proceed with - it failed, and that's been
        # reported. Returning False would make the caller claim nothing was
        # pending, which is worse than the truth.
        _report_request_error(exc, console, session_logger)
        return True
    _finish_current_run(workspace, ledger)
    clear_current_run()
    return True


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
            if (
                _should_show_context_preview()
                and preview.prompt
                and _preview_contains_context(preview.prompt)
            ):
                console.print(Panel(preview.prompt, title="Context Preview"))
            return
    result = await Coordinator(llm=llm, qa_workflow=qa_workflow).handle(request)
    if result.answer:
        title = f"Answer ({result.model_used})" if result.model_used else "Answer"
        console.print(Panel(result.answer, title=title))
        _log_assistant_message(session_logger, result.answer, workflow_id="qa")
    elif result.fallback_reason:
        console.print(f"[yellow]{result.fallback_reason}[/yellow]")
    if (
        _should_show_context_preview()
        and result.preview
        and _preview_contains_context(result.preview)
    ):
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
            "You are SHAMSU, a friendly AI coding assistant, talking with the user. "
            "Reply directly and naturally in a sentence or two, the way a helpful "
            "person would. Do NOT narrate your reasoning, do NOT describe yourself "
            "in the third person, and do NOT mention context, tools, files, or tasks "
            "unless the user asked about them. If the user greeted you or made small "
            "talk, reply warmly and briefly and offer to help. Do not claim you saw "
            "code, tests, or files that were not actually provided. "
            + NO_LIVE_TOOLS_NOTICE
            + (f"\n\n{extra_context}" if extra_context else "")
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


def _ledger_counts(workspace: Path, ledger: ActionLedger | None) -> tuple[int, int]:
    if not ledger:
        return 0, 0
    return (
        len(action_ledger_store.load_tool_calls(workspace, ledger.run_id)),
        len(action_ledger_store.load_events(workspace, ledger.run_id)),
    )


def _ledger_delta(
    workspace: Path, ledger: ActionLedger | None, before_tools: int, before_events: int
) -> tuple[list[str], set[str]]:
    """Successful tool names and event types recorded SINCE the snapshot."""
    if not ledger:
        return [], set()
    tool_records = action_ledger_store.load_tool_calls(workspace, ledger.run_id)[before_tools:]
    events = action_ledger_store.load_events(workspace, ledger.run_id)[before_events:]
    successful = [
        str(item.get("tool", ""))
        for item in tool_records
        if item.get("phase") == "finished" and item.get("ok") is True
    ]
    return successful, {str(item.get("type", "")) for item in events}


def _composite_step_prompt(
    plan: OperationPlan, step: OperationStep, done: list[dict[str, object]], agent_context: str
) -> str:
    """A focused prompt for ONE step of an ordered plan.

    Each step runs as its own agent turn (see `_run_composite_request`) so its
    outcome can be judged on what THAT step actually did, not on aggregate
    evidence that let one edit mark every step done. The step still sees the
    whole plan and what earlier steps produced, so "run it" / "show the diff"
    references resolve.
    """
    lines = [
        f"Original request: {plan.prompt}",
        "",
        "You are executing that request one step at a time. The full ordered plan:",
    ]
    for planned in plan.steps:
        marker = ">>" if planned.id == step.id else "  "
        lines.append(f"{marker} {planned.id}. [{planned.kind}] {planned.instruction}")
    if done:
        lines.append("")
        lines.append("Already completed in earlier steps:")
        for record in done:
            lines.append(f"  - step {record['id']} ({record['status']}): {record['instruction']}")
    lines.extend(
        [
            "",
            f"Do ONLY step {step.id} now: {step.instruction}.",
            "Use the registered tools to actually perform it - do not merely describe it, "
            "and do not do any other step. If a real choice is needed, call ask_user.",
        ]
    )
    if _explicitly_read_only(plan.prompt) or step.kind not in {"mutation", "git_mutate"}:
        lines.append("Do not modify any files while doing this.")
    return _append_agent_context("\n".join(lines), agent_context)


async def _run_composite_request(
    plan: OperationPlan,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    agent_context: str = "",
):
    """Execute an ordered operation plan one step at a time.

    Rewritten 2026-07-21 after dogfooding: the old version ran the WHOLE plan in
    a single agent turn, then reverse-engineered per-step success from aggregate
    ledger evidence - so a single successful edit marked EVERY mutation step
    "success". Live, "edit greet() AND update __main__" changed greet(), never
    touched __main__, and still reported "Step 2: success" while leaving the file
    broken. Now each step gets its own turn and is judged on the evidence that
    turn produced (its own confirmed writes, its own tool calls), so a step that
    did not happen is reported not_run - honestly - instead of riding on an
    earlier step's success.
    """
    ledger = get_current_run()
    if ledger:
        for step in plan.steps:
            ledger.log_event("operation_step_planned", **step.to_dict())
    emit_trace(
        console,
        session_logger,
        workspace,
        "operation.plan",
        f"Executing {len(plan.steps)} ordered operations",
        plan.to_dict(),
        level="normal",
    )

    statuses: list[dict[str, object]] = []
    last_result: Any = None
    blocked = False
    failed_step_id: int | None = None
    for step in plan.steps:
        if blocked:
            # An earlier step is waiting on the user; later steps depend on the
            # answer, so they are honestly not-run rather than attempted blind.
            record = {**step.to_dict(), "status": "not_run", "evidence": ["blocked:awaiting_input"]}
            statuses.append(record)
            _record_composite_step(step, record, ledger, session_logger)
            continue
        if failed_step_id is not None:
            # A dependency FAILED. Running the dependents blind against the
            # broken state produced cascading nonsense in the 2026-08-01
            # dogfood; they are honestly not-run instead.
            record = {
                **step.to_dict(),
                "status": "not_run",
                "evidence": [f"blocked:dependency_failed:step_{failed_step_id}"],
            }
            statuses.append(record)
            _record_composite_step(step, record, ledger, session_logger)
            continue

        before_tools, before_events = _ledger_counts(workspace, ledger)
        step_output = ""
        install_command = (
            _package_install_command(step.instruction) if step.kind == "mutation" else ""
        )
        deterministic_verify = (
            _composite_verification_command(plan, step)
            if step.kind == "verify" and _python_package_spec(plan.prompt)
            else ""
        )
        if install_command:
            install_result, step_output = _execute_package_install(
                install_command,
                workspace,
                console,
                session_logger,
                ledger,
            )
            result = AgentLoopResult(
                final=step_output,
                stopped=not bool(install_result.ok),
            )
        elif deterministic_verify:
            step_output, verify_ok = _execute_deterministic_composite_verification(
                plan,
                step,
                workspace,
                console,
                session_logger,
                ledger,
            )
            result = AgentLoopResult(final=step_output, stopped=not verify_ok)
        elif step.kind == "summarize":
            prior = [
                f"Step {item['id']} ({item['kind']}): {item['status']}"
                + (f"\n{item['output']}" if item.get("output") else "")
                for item in statuses
            ]
            result = AgentLoopResult(final="\n".join(prior) or "No earlier step results.")
        else:
            result = await _run_agent_chat(
                _composite_step_prompt(plan, step, statuses, agent_context),
                workspace,
                console,
                session_logger=session_logger,
                auto_approve=is_long_running_enabled(workspace),
                user_request=plan.prompt,
                force_read_only=step.kind not in {"mutation", "git_mutate"},
            )
        last_result = result
        step_tools, step_events = _ledger_delta(workspace, ledger, before_tools, before_events)

        # A git-inspect step the agent didn't satisfy falls back to the
        # deterministic git read, then the delta is recomputed so the fallback's
        # evidence counts for THIS step.
        if step.kind == "git_inspect" and not (
            set(step_tools) & {"git_status", "git_diff", "git_diff_staged", "git_log"}
        ):
            _execute_composite_git_inspection(workspace, console, session_logger, ledger)
            step_tools, step_events = _ledger_delta(workspace, ledger, before_tools, before_events)

        if step.kind == "verify" and "run_command" not in set(step_tools):
            step_output, _verify_ok = _execute_deterministic_composite_verification(
                plan, step, workspace, console, session_logger, ledger
            )
            step_tools, step_events = _ledger_delta(workspace, ledger, before_tools, before_events)
        if step.kind == "verify" and not step_output:
            step_output = _composite_command_output(workspace, ledger, before_tools)
        if step.kind == "verify" and "run_command" in set(step_tools):
            step_events = _ensure_composite_agent_verification_event(
                plan,
                step,
                workspace,
                ledger,
                before_tools,
                step_events,
            )

        status, evidence = _composite_step_outcome(step, result, step_tools, step_events)
        record = {**step.to_dict(), "status": status, "evidence": evidence}
        if step_output:
            record["output"] = step_output
        statuses.append(record)
        _record_composite_step(step, record, ledger, session_logger)
        if status == "needs_input" or bool(getattr(result, "awaiting_user", False)):
            blocked = True
        elif status == "failed":
            failed_step_id = step.id

    completed = [item for item in statuses if item["status"] == "success"]
    incomplete = [item for item in statuses if item["status"] != "success"]
    if not incomplete:
        composite_status = "success"
    elif any(item["status"] == "needs_input" for item in incomplete):
        composite_status = "needs_input"
    elif completed:
        composite_status = "partial"
    else:
        composite_status = "failed"
    if ledger:
        event_type = (
            "composite_completed"
            if composite_status == "success"
            else f"composite_{composite_status}"
        )
        ledger.log_event(event_type, steps=statuses)
    if composite_status != "needs_input":
        lines = []
        for item in statuses:
            line = f"Step {item['id']} ({item['kind']}): {item['status']}"
            tool_evidence = [
                str(value).removeprefix("tool:")
                for value in item.get("evidence", [])
                if str(value).startswith("tool:")
            ]
            if tool_evidence:
                line += f" via {', '.join(tool_evidence)}"
            if item.get("output"):
                line += f"\n{item['output']}"
            lines.append(line)
        detail = "\n".join(lines)
        model_final = str(getattr(last_result, "final", "") or "").strip()
        if composite_status == "success":
            corrected = f"Composite execution status: success.\n{detail}"
        else:
            corrected = (
                f"{model_final}\n\nComposite execution status: {composite_status}.\n{detail}"
            ).strip()
        console.print(Panel(Text(detail), title=f"Composite: {composite_status.title()}"))
        _log_assistant_message(session_logger, corrected, workflow_id="composite")
    return last_result


def _record_composite_step(
    step: OperationStep,
    record: dict[str, object],
    ledger: ActionLedger | None,
    session_logger: SessionLogger | None,
) -> None:
    if ledger:
        ledger.log_event("operation_step_finished", **record)
    _log_event(
        session_logger,
        "operation.step_finished",
        record,
        f"Operation {step.id} finished: {record['status']}",
        workflow_id="composite",
    )


def _execute_composite_git_inspection(
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    ledger: ActionLedger | None,
) -> None:
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
        action_ledger=ledger,
    )
    sections: list[str] = []
    for tool_name in ("git_status", "git_diff"):
        call_id = ledger.log_tool_call(tool_name, {}) if ledger else ""
        result = registry.execute(tool_name, {})
        semantic_ok = bool(result.ok) and not (
            tool_name == "git_status" and not result.data.get("is_git_repo", True)
        )
        if ledger:
            ledger.log_tool_result(call_id, tool_name, semantic_ok, result.message, result.data)
        _log_event(
            session_logger,
            "operation.git_inspection",
            {"tool": tool_name, "ok": semantic_ok},
            f"Composite Git inspection: {tool_name}",
            workflow_id="composite",
        )
        sections.append(_format_git_read_result(tool_name, result))
    body = "\n\n".join(section for section in sections if section).strip()
    if body:
        console.print(Panel(Text(body), title="Git Follow-up"))


def _composite_verification_command(plan: OperationPlan, step: OperationStep) -> str:
    explicit = re.search(r"`([^`]+)`", step.instruction)
    if explicit and re.match(
        r"^(?:python3?|py|pytest|npm|npx|node|pnpm|yarn|cargo|go|dotnet)\b",
        explicit.group(1).strip(),
        re.IGNORECASE,
    ):
        return explicit.group(1).strip()
    target = _extract_requested_file_path(plan.prompt)
    instruction = step.instruction.lower()
    if target and any(word in instruction for word in ("run", "execute", "script")):
        suffix = Path(target).suffix.lower()
        quoted = subprocess.list2cmdline([target]) if os.name == "nt" else shlex.quote(target)
        if suffix == ".py":
            return f"python {quoted}"
        if suffix in {".js", ".mjs", ".cjs"}:
            return f"node {quoted}"
    if re.search(r"\b(?:run|rerun|re-run)\b.*\btests?\b", instruction):
        return "python -m pytest -q"
    package_spec = _python_package_spec(plan.prompt)
    if package_spec and re.search(r"\bimport(?:s|ed|ing)?\b", instruction):
        module = re.split(r"[\[<>=!~]", package_spec, maxsplit=1)[0].replace("-", "_")
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
            script = f"import {module}, sys; print(sys.executable); print({module}.__file__)"
            quoted_script = subprocess.list2cmdline([script])
            return f"python -c {quoted_script}"
    return ""


def _execute_deterministic_composite_verification(
    plan: OperationPlan,
    step: OperationStep,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None,
    ledger: ActionLedger | None,
) -> tuple[str, bool]:
    command = _composite_verification_command(plan, step)
    if not command:
        return "", False
    registry = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console),
        action_ledger=ledger,
    )
    call_id = ledger.log_tool_call("run_command", {"command": command}) if ledger else ""
    if ledger:
        verifier_id = ledger.verifier_id_for(command, "composite_fallback")
        ledger.log_verification_started(
            command,
            verifier_id=verifier_id,
            source="composite_fallback",
            required=True,
        )
    else:
        verifier_id = ""
    result = registry.execute("run_command", {"command": command})
    if ledger:
        ledger.log_tool_result(call_id, "run_command", result.ok, result.message, result.data)
        ledger.log_verification_result(
            bool(result.ok),
            result.message,
            command=command,
            verifier_id=verifier_id,
            source="composite_fallback",
            required=True,
            exit_code=result.data.get("exit_code"),
        )
    output = str(result.data.get("stdout") or result.data.get("stderr") or result.message).strip()
    rendered = f"$ {command}\n{output}".strip()
    console.print(
        Panel(
            Text(rendered),
            title="Verification",
            border_style="green" if result.ok else "red",
        )
    )
    return rendered, bool(result.ok)


def _latest_composite_command_record(
    workspace: Path,
    ledger: ActionLedger | None,
    before_tools: int,
) -> tuple[str, bool, str, dict[str, Any]] | None:
    if ledger is None:
        return None
    records = action_ledger_store.load_tool_calls(workspace, ledger.run_id)[before_tools:]
    finished = next(
        (
            record
            for record in reversed(records)
            if record.get("tool") == "run_command" and record.get("phase") == "finished"
        ),
        None,
    )
    if finished is None:
        return None
    call_id = finished.get("tool_call_id")
    called = next(
        (
            record
            for record in reversed(records)
            if record.get("tool_call_id") == call_id and record.get("phase") == "called"
        ),
        {},
    )
    arguments = called.get("arguments") if isinstance(called, dict) else {}
    command = str(arguments.get("command") or "") if isinstance(arguments, dict) else ""
    data = finished.get("data") if isinstance(finished.get("data"), dict) else {}
    return command, bool(finished.get("ok")), str(finished.get("message") or ""), data


def _composite_command_output(
    workspace: Path,
    ledger: ActionLedger | None,
    before_tools: int,
) -> str:
    """Render the latest command evidence produced during a composite step."""
    record = _latest_composite_command_record(workspace, ledger, before_tools)
    if record is None:
        return ""
    command, _ok, message, data = record
    output = str(data.get("stdout") or data.get("stderr") or message or "").strip()
    if not command and not output:
        return ""
    return f"$ {command}\n{output}".strip()


def _ensure_composite_agent_verification_event(
    plan: OperationPlan,
    step: OperationStep,
    workspace: Path,
    ledger: ActionLedger | None,
    before_tools: int,
    step_events: set[str],
) -> set[str]:
    """Promote an agent-run command in a verify step into verifier evidence."""
    if ledger is None or step_events & {"verification_passed", "verification_failed"}:
        return step_events
    record = _latest_composite_command_record(workspace, ledger, before_tools)
    if record is None:
        return step_events
    command, ok, message, data = record
    expected = _composite_verification_command(plan, step)
    passed = ok and (not expected or command.strip() == expected.strip())
    if ok and expected and command.strip() != expected.strip():
        message = f"Expected verifier `{expected}`, but agent ran `{command}`."
    verifier_command = expected or command
    verifier_id = ledger.verifier_id_for(verifier_command, "composite_agent_verify")
    ledger.log_verification_started(
        verifier_command,
        verifier_id=verifier_id,
        source="composite_agent_verify",
        required=True,
    )
    ledger.log_verification_result(
        passed,
        message,
        command=verifier_command,
        verifier_id=verifier_id,
        source="composite_agent_verify",
        required=True,
        exit_code=data.get("exit_code"),
        actual_command=command,
        expected_command=expected,
    )
    updated = set(step_events)
    updated.add("verification_passed" if passed else "verification_failed")
    return updated


def _composite_step_outcome(
    step: OperationStep,
    result: Any,
    successful_tools: list[str],
    event_types: set[str],
) -> tuple[str, list[str]]:
    tools = set(successful_tools)
    tool_leaves = [_tool_leaf(name) for name in successful_tools]
    changed_files = list(getattr(result, "changed_files", ()) or ())
    stopped = bool(getattr(result, "stopped", False))
    awaiting_user = bool(getattr(result, "awaiting_user", False))
    evidence: list[str] = []
    matched = False
    if "verification_failed" in event_types:
        return "failed", ["event:verification_failed"]
    if step.kind == "mutation":
        file_mutation = bool(
            set(tool_leaves)
            & {"write_file", "edit_file", "move_file", "delete_file", "create_directory"}
        ) or bool(changed_files)
        package_install = bool(_package_install_command(str(getattr(step, "instruction", ""))))
        matched = file_mutation or (package_install and "run_command" in tools)
        evidence = [f"changed:{path}" for path in changed_files]
    elif step.kind == "verify":
        matched = "verification_passed" in event_types or any(
            _mcp_tool_provides_read_evidence(name) for name in successful_tools
        )
        evidence = ["event:verification_passed"] if "verification_passed" in event_types else []
    elif step.kind == "git_inspect":
        matched = bool(tools & {"git_status", "git_diff", "git_diff_staged", "git_log"})
    elif step.kind == "git_mutate":
        matched = bool(tools & {"git_add", "git_add_all", "git_commit", "git_push", "git_pull"})
    elif step.kind == "web":
        matched = bool(tools & {"web_search", "fetch_url"})
    elif step.kind == "read":
        matched = any(_tool_provides_read_evidence(name) for name in successful_tools)
    elif step.kind == "compare":
        matched = (
            sum(_tool_provides_read_evidence(name) for name in successful_tools) >= 2
            and not stopped
        )
    elif step.kind == "launch":
        matched = "run_command" in tools and not stopped
    else:
        matched = bool(str(getattr(result, "final", "") or "").strip()) and not stopped
    if matched:
        evidence.extend(f"tool:{name}" for name in successful_tools)
        return "success", evidence
    if awaiting_user:
        return "needs_input", evidence
    if stopped:
        return "failed", evidence
    return "not_run", evidence


def _tool_leaf(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return parts[2]
    return name


_READ_EVIDENCE_TOOLS = frozenset(
    {
        "read_file",
        "read_text_file",
        "read_multiple_files",
        "list_directory",
        "list_directory_with_sizes",
        "directory_tree",
        "search_files",
        "get_file_info",
        "list_allowed_directories",
    }
)


def _mcp_tool_provides_read_evidence(name: str) -> bool:
    return name.startswith("mcp__") and _tool_leaf(name) in _READ_EVIDENCE_TOOLS


def _tool_provides_read_evidence(name: str) -> bool:
    return _tool_leaf(name) in _READ_EVIDENCE_TOOLS


def _configure_agent_request_safety(
    tools: AgentToolRegistry,
    safety_input: str,
    *,
    workspace: Path | None = None,
    force_read_only: bool = False,
    allowed_write_paths: tuple[str, ...] | None = None,
    allowed_read_paths: tuple[str, ...] | None = None,
) -> bool:
    """Apply permissions from the clean current request, never model context."""
    tools.set_user_request(safety_input)
    request_is_read_only = force_read_only or _explicitly_read_only(safety_input)
    if request_is_read_only:
        tools.set_read_only(True)
    elif read_only.is_scoped(safety_input):
        allowed = contract.requested_paths(safety_input, workspace)
        if allowed:
            tools.set_allowed_write_paths(allowed)
    if allowed_write_paths:
        tools.set_allowed_write_paths(allowed_write_paths)
    if allowed_read_paths:
        tools.set_allowed_read_paths(allowed_read_paths)
    return request_is_read_only


async def _run_agent_chat(
    user_input: str,
    workspace: Path,
    console: Console,
    session_logger: SessionLogger | None = None,
    force_long_running: bool = False,
    auto_approve: bool = False,
    allowed_write_paths: tuple[str, ...] | None = None,
    allowed_read_paths: tuple[str, ...] | None = None,
    allowed_tools: tuple[str, ...] | None = None,
    use_long_term_memory: bool = True,
    use_planner: bool = True,
    user_request: str | None = None,
    force_read_only: bool = False,
    required_tool_prefix: str = "",
    hydrate_history: bool = True,
    verify_changes: bool = True,
    use_model_compaction: bool = True,
    runtime_task_id: str | None = None,
    task_contract: TaskContract | None = None,
    task_contract_persist: Callable[[TaskContract], None] | None = None,
) -> "AgentLoopResult | None":
    # auto_approve is used for an explicitly user-consented PRD build: the user
    # already approved building the whole product, so the agent's file writes
    # and verification commands during that build run without further prompts
    # (this also sidesteps the fragile mid-flow input() approval on Windows).
    approval_func = (lambda _request: True) if auto_approve else ask_approval
    action_ledger = get_current_run()
    tools = AgentToolRegistry(
        workspace,
        session_logger=session_logger,
        approval_manager=_make_approval_manager(workspace, session_logger, console, approval_func),
        action_ledger=action_ledger,
    )
    # ``user_input`` may include session memory, task handoffs, or focused
    # composite instructions. Those are useful model context but must never
    # silently change the permissions granted by the current user request.
    safety_input = user_request if user_request is not None else user_input
    # "Do not change files" outranks auto_approve. Approval mode answers "may I
    # act without asking?"; it never licenses ignoring an explicit instruction.
    request_is_read_only = _configure_agent_request_safety(
        tools,
        safety_input,
        workspace=workspace,
        force_read_only=force_read_only,
        allowed_write_paths=allowed_write_paths,
        allowed_read_paths=allowed_read_paths,
    )
    if task_contract is not None:
        tools.set_allowed_write_paths(task_contract.allowed_write_paths)

        def _handle_scope_expansion(filepath: str, reason: str) -> ToolResult:
            nonlocal task_contract
            expanded = request_scope_expansion(task_contract, workspace, filepath, reason)
            if expanded.ok:
                task_contract = expanded.contract
                tools.set_allowed_write_paths(expanded.contract.allowed_write_paths)
                try:
                    if task_contract_persist is not None:
                        task_contract_persist(expanded.contract)
                except Exception:
                    pass
                _log_task_contract_event(
                    session_logger,
                    "task_contract.scope_expansion_approved",
                    {
                        "task_id": expanded.contract.task_id,
                        "filepath": expanded.path,
                        "reason": reason,
                        "allowed_write_paths": expanded.contract.allowed_write_paths,
                    },
                    expanded.message,
                )
                return ToolResult(
                    True,
                    expanded.message,
                    {
                        "scope_expansion": True,
                        "approved": True,
                        "filepath": expanded.path,
                        "allowed": expanded.contract.allowed_write_paths,
                    },
                )
            _log_task_contract_event(
                session_logger,
                "task_contract.scope_expansion_rejected",
                {"task_id": task_contract.task_id, "filepath": filepath, "reason": reason},
                expanded.message,
            )
            return ToolResult(
                False,
                expanded.message,
                {"scope_expansion": True, "approved": False, "filepath": expanded.path or filepath},
            )

        tools.set_scope_expansion_handler(_handle_scope_expansion)
    if allowed_tools is not None:
        tools.set_allowed_tools(allowed_tools)
    if not hydrate_history and not use_long_term_memory and not use_planner:
        use_model_compaction = False
    if required_tool_prefix:
        tools.require_tool_prefix(required_tool_prefix)
        user_input = (
            f"{user_input}\n\nRequired first action: call `{required_tool_prefix}` directly. "
            "The harness has already supplied the necessary evidence. Do not substitute a "
            "read, edit, append, search, or command tool."
        )
    if _looks_like_mcp_request(safety_input):
        use_long_term_memory = False
        use_planner = False
        tools.require_tool_prefix("mcp__")
        mcp_names = [
            str((schema.get("function") or {}).get("name") or "")
            for schema in tools.tool_schemas()
            if str((schema.get("function") or {}).get("name") or "").startswith("mcp__")
        ]
        if mcp_names:
            mutation_request = bool(
                contract.requested_paths(safety_input, workspace)
                and re.search(
                    r"\b(create|write|save|edit|update|modify|change|delete|remove)\b",
                    read_only.strip(safety_input),
                    re.IGNORECASE,
                )
            )
            preferred = [
                name
                for name in mcp_names
                if mutation_request
                and re.search(r"__(?:write|create|edit|update|delete|remove)[a-z0-9_-]*$", name)
            ]
            preference = (
                " This is an explicitly authorized mutation. Call "
                + (preferred[0] if preferred else "the matching MCP mutation tool")
                + " directly; do not probe the missing destination with a read tool and do not ask for confirmation."
                if mutation_request
                else ""
            )
            user_input = (
                f"{user_input}\n\nMCP tool requirement: call one of these registered tools directly: "
                + ", ".join(mcp_names)
                + ". Do not run an `mcp` shell command and do not substitute a local tool."
                + preference
            )
    if _looks_like_docs_ingest_request(safety_input):
        use_long_term_memory = False
        use_planner = False
        tools.require_tool_prefix("ingest_docs")
        user_input = (
            f"{user_input}\n\nDocumentation ingestion requirement: call `ingest_docs` directly "
            "with the named workspace path or URL and the library name if the user supplied one. "
            "Do not reproduce the documentation with write_file and do not claim it was retained "
            "unless ingest_docs succeeds."
        )
    elif _looks_like_docs_query_request(safety_input):
        use_long_term_memory = False
        use_planner = False
        required_docs_tool = _required_docs_tool(safety_input)
        tools.require_tool_prefix(required_docs_tool)
        user_input = (
            f"{user_input}\n\nRegistered-document requirement: call `{required_docs_tool}` "
            "directly with the user's question and document name when supplied. Use only its "
            "citation-backed evidence, preserve citations in the answer, and say when the "
            "registered documents do not contain enough evidence."
        )
    # A dry run is the opposite instruction: keep going, change nothing. Writes
    # report a synthetic success and are recorded as planned actions instead.
    # The `--dry-run` flag sets the context recorder; prose ("dry run only:
    # create X") activates the same mode with a local recorder so the intent is
    # honored whether it arrived as a flag or as words.
    active_recorder = dry_run.get_recorder()
    local_dry_run = active_recorder is None and read_only.is_dry_run(safety_input)
    if local_dry_run:
        active_recorder = dry_run.DryRunRecorder()
    tools.set_dry_run(active_recorder)
    if active_recorder is not None:
        # A preview should be anchored only to the current request. Retrieved
        # memories and a speculative planner can introduce stale output paths.
        use_long_term_memory = False
        use_planner = False
    long_running = force_long_running or is_long_running_enabled(workspace)
    activities: list[str] = []
    trace_mode = _trace_mode(workspace)
    progress = ProgressReporter(
        None if trace_mode == "quiet" else console,
        session_logger,
        title="Agent",
        # Agent progress emits model calls, tool starts, and tool results. A
        # generic max here pretends those are the same unit; the chat loop prints
        # the real model-call denominator as "choosing action X/Y".
        max_steps=None,
        verbose=trace_mode == "verbose",
    )
    progress.step("Waiting for model response")

    def on_activity(msg: str) -> None:
        activities.append(msg)
        progress.step(msg)

    chat_kwargs: dict[str, Any] = {
        "session_logger": session_logger,
        "tools": tools,
        "long_running": long_running,
        "on_activity": on_activity,
        "progress": progress,
        "action_ledger": action_ledger,
    }
    # Guard optional kwargs so test doubles that patch AgentChatLoop with a
    # narrower signature keep working (same pattern as budget_manager).
    if _call_accepts_keyword(AgentChatLoop, "on_trace"):
        chat_kwargs["on_trace"] = _make_trace_emitter(console, workspace, session_logger)
    if _call_accepts_keyword(AgentChatLoop, "budget_manager"):
        chat_kwargs["budget_manager"] = _get_budget_manager(workspace, console)
    if _call_accepts_keyword(AgentChatLoop, "read_only"):
        chat_kwargs["read_only"] = request_is_read_only
    if _call_accepts_keyword(AgentChatLoop, "use_long_term_memory"):
        chat_kwargs["use_long_term_memory"] = use_long_term_memory
    if _call_accepts_keyword(AgentChatLoop, "use_planner"):
        chat_kwargs["use_planner"] = use_planner
    if _call_accepts_keyword(AgentChatLoop, "hydrate_history"):
        chat_kwargs["hydrate_history"] = hydrate_history
    if _call_accepts_keyword(AgentChatLoop, "verify_changes"):
        chat_kwargs["verify_changes"] = verify_changes
    if _call_accepts_keyword(AgentChatLoop, "use_model_compaction"):
        chat_kwargs["use_model_compaction"] = use_model_compaction
    if _call_accepts_keyword(AgentChatLoop, "original_user_request"):
        # A pending ask_user must resume from the clean user request, not an
        # internal wrapper (composite step / PRD repair contract).
        chat_kwargs["original_user_request"] = safety_input
    if runtime_task_id and _call_accepts_keyword(AgentChatLoop, "runtime_task_id"):
        chat_kwargs["runtime_task_id"] = runtime_task_id
    if task_contract is not None and _call_accepts_keyword(AgentChatLoop, "task_contract"):
        chat_kwargs["task_contract"] = task_contract
    if _call_accepts_keyword(AgentChatLoop, "audit"):
        session_id = session_logger.session_id if session_logger is not None else None
        audit = SessionAuditLog(workspace, session_id)
        audit.log_route(
            "agent-chat",
            workflow="agent-chat",
            model=model_for_role("qa"),
            tier=str(getattr(active_tier(), "value", active_tier())),
        )
        chat_kwargs["audit"] = audit
    result = await AgentChatLoop(workspace, **chat_kwargs).run(user_input)
    if action_ledger and getattr(result, "timeout_category", None):
        action_ledger.log_event(
            "run_timed_out",
            phase="model",
            category=result.timeout_category,
        )
    elif action_ledger and result.stopped and not getattr(result, "awaiting_user", False):
        action_ledger.log_event("agent_stopped", reason=result.final[:500])
    # A prose-triggered dry run has no headless wrapper to report its plan, so
    # replace the agent's (synthetic-success) narration with the actual preview.
    if local_dry_run and active_recorder is not None:
        console.print(Panel(active_recorder.summary(), title="Dry Run", border_style="cyan"))
        _log_assistant_message(session_logger, active_recorder.summary(), workflow_id="agent-chat")
        return result
    body = result.final.strip() or "No response returned."
    mcp_tools_used = _successful_mcp_tools(action_ledger, workspace)
    if mcp_tools_used and not all(name in body for name in mcp_tools_used):
        body = f"{body}\n\nExternal MCP tool used: " + ", ".join(
            f"`{name}`" for name in mcp_tools_used
        )
        result = replace(result, final=body)
    if getattr(result, "awaiting_user", False):
        # SHAMSU asked the user something and stored the pending question. Print
        # it clearly and stop - the next reply is resolved against it. Do not
        # record a "task completed" memory: nothing finished yet.
        console.print(Panel(body, title="Need Input", border_style="cyan"))
        _log_assistant_message(session_logger, body, workflow_id="clarification")
        return result
    if result.stopped:
        progress.warning("Agent stopped before completing all requested work")
    else:
        progress.done("Agent finished")
    console.print(Panel(_agent_display_summary(body, activities), title="Agent"))
    # Point at the escape hatch the moment it's relevant. Every write was
    # already backed up by a transaction, but /patch rollback needed an id from
    # .shamsu/mutations - so in practice nobody undid anything (gap G2).
    if getattr(result, "changed_files", ()):
        console.print("[dim]Not what you wanted? `/undo` reverts the last change.[/dim]")
    _log_assistant_message(session_logger, body, workflow_id="agent-chat")
    # Update the session summary and durable memory only when the agent actually
    # finished the work (a stopped/looping run is not a completed task).
    if not result.stopped:
        _finalize_session_work(session_logger, "agent-chat", user_input)
    return result


def _successful_mcp_tools(
    ledger: ActionLedger | None,
    workspace: Path,
) -> list[str]:
    if ledger is None:
        return []
    return list(
        dict.fromkeys(
            str(record.get("tool", ""))
            for record in action_ledger_store.load_tool_calls(workspace, ledger.run_id)
            if record.get("phase") == "finished"
            and record.get("ok") is True
            and str(record.get("tool", "")).startswith("mcp__")
        )
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
    lines.append(
        "Full generated code is in the edited files. Detailed raw output is kept in the session log."
    )
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
    search_query = _web_search_query(user_input)
    if hasattr(web_tool, "search_and_fetch"):
        ledger = getattr(web_tool, "action_ledger", None)
        call_id = (
            ledger.log_tool_call(
                "web_search",
                {
                    "query": search_query,
                    "original_request": user_input,
                    "mode": "search_and_fetch",
                },
            )
            if ledger
            else ""
        )
        combined = web_tool.search_and_fetch(
            search_query,
            reason="SHAMSU thinks this request needs current or external information from the web.",
        )
        if ledger:
            ok = bool(
                combined.approved and not combined.error and (combined.hits or combined.pages)
            )
            ledger.log_tool_result(
                call_id,
                "web_search",
                ok,
                combined.error
                or f"Found {len(combined.hits)} result(s) and fetched {len(combined.pages)} page(s).",
                {
                    "query": combined.query,
                    "provider": combined.provider,
                    "fallback_used": combined.fallback_used,
                    "hit_count": len(combined.hits),
                    "page_count": len(combined.pages),
                    "sources": [{"title": hit.title, "url": hit.url} for hit in combined.hits[:10]],
                },
            )
        if not combined.approved:
            await _run_general_chat(
                user_input,
                console,
                llm,
                extra_context=(
                    "External web access was denied. Answer with general knowledge only and mention that current details may be stale."
                ),
            )
            return
        if combined.error:
            console.print(f"[yellow]Web search failed: {combined.error}[/yellow]")
            await _run_general_chat(
                user_input,
                console,
                llm,
                extra_context="Web lookup failed. Answer locally and mention that external lookup was unavailable.",
            )
            return
        if not combined.hits and not combined.pages:
            # Approved, no error, and zero results (dead SearXNG, rate-limited
            # fallback). Previously this asked the model to answer "from memory,
            # but say you couldn't verify" - and the 7B model IGNORED the caveat
            # and stated a fabricated fact as though confirmed (dogfood: "Python
            # 3.10.6, Oct 2023"). A confident wrong answer to a query the user
            # explicitly sent to the web is worse than an honest miss, and a
            # small model cannot be trusted to self-disclaim. So the disclaimer
            # is DETERMINISTIC and the model is not consulted for the fact.
            provider = combined.provider or "the configured provider"
            message = (
                f"I could not retrieve live web results ({provider} returned nothing), "
                "so I have not verified this against a current source. I'm not going to "
                "guess at a live fact from memory, because it could be out of date or wrong. "
                "Check that a search provider is reachable (`/doctor`), or ask me to answer "
                "from general knowledge and I'll clearly mark it as unverified."
            )
            console.print(Panel(message, title="Web Search — No Results", border_style="yellow"))
            _log_assistant_message(session_logger, message, workflow_id="web")
            return
        await _print_web_answer(
            user_input, combined, combined.pages, console, llm, session_logger=session_logger
        )
        return

    result = web_tool.search(
        search_query,
        reason="SHAMSU thinks this request needs current or external information from the web.",
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
    await _print_web_answer(
        user_input, result, fetches, console, llm, session_logger=session_logger
    )


def _web_search_query(user_input: str) -> str:
    """Turn an instruction into the concise subject sent to the search engine."""
    query = " ".join(str(user_input or "").split())
    query = re.sub(
        r"^(?:please\s+)?(?:use\s+)?(?:the\s+)?(?:web\s+search|search\s+the\s+web|"
        r"search\s+online|look\s+online)\s+(?:to\s+)?",
        "",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"^(?:find|look\s+up|check)\s+", "", query, flags=re.IGNORECASE)
    query = re.sub(r"^the\s+", "", query, flags=re.IGNORECASE)
    query = re.split(
        r"\s*(?:,|;|\band\b)\s*(?:cite|include\s+sources?|tell\s+me\s+where|"
        r"do\s+not\s+modify|don't\s+modify)",
        query,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return query.strip(" ,:;") or user_input.strip()


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
            title=f"Browser Inspection ({response.model_used})"
            if response.model_used
            else "Browser Inspection",
        )
    )


async def _print_web_answer(
    query: str,
    result: WebSearchResult | WebSearchFetchResult,
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
    if isinstance(result, WebSearchFetchResult):
        if not fetches:
            sources = "\n".join(f"- {hit.title}: {hit.url}" for hit in result.hits[:5])
            message = (
                "I found search results, but I could not fetch readable page evidence. "
                "I cannot verify the answer from snippets alone.\n\n"
                f"Sources searched:\n{sources}"
            )
            console.print(Panel(message, title="Web Answer"))
            _log_assistant_message(session_logger, message, workflow_id="web")
            return
        prompt = build_evidence_answer_prompt(query, result)
        pack = ContextPack(
            task_id="web-qa",
            step_id=1,
            specialist="qa",
            user_request=query,
            prd_context=prompt,
        )
        response = await llm.run_specialist("qa", pack)
        body = response.raw.strip()
    elif fetches:
        context = "\n\n".join(
            f"Source: {item.url}\nTitle: {item.title}\n{item.text[:2500]}" for item in fetches
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
        body = (
            response.raw.strip()
            or "I found sources, but could not synthesize an answer from the snippets."
        )
    body = re.split(
        r"\n\s*(?:#{1,4}\s*)?(?:sources?\s+used|sources?\s+searched(?:/fetched)?|sources?)\s*:?[ \t]*\n",
        body,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip()
    grounded = _grounded_release_answer(query, fetches)
    if grounded:
        body = grounded
    source_items: list[tuple[str, str]] = []
    for page in fetches:
        item = (page.title or page.url, getattr(page, "final_url", "") or page.url)
        if item[1] and item[1] not in {url for _title, url in source_items}:
            source_items.append(item)
    for hit in result.hits:
        item = (hit.title, hit.url)
        if item[1] and item[1] not in {url for _title, url in source_items}:
            source_items.append(item)
    official_domains = _official_domains_for_query(query) if "official" in query.lower() else ()
    if official_domains:
        official_items = [
            item
            for item in source_items
            if any(
                (urlparse(item[1]).hostname or "") == domain
                or (urlparse(item[1]).hostname or "").endswith("." + domain)
                for domain in official_domains
            )
        ]
        if official_items:
            source_items = official_items
    sources = "\n".join(f"- {title}: {url}" for title, url in source_items[:5])
    provider_note = ""
    if getattr(result, "fallback_used", False):
        provider_note = "\n\nNote: SearXNG was unavailable, so SHAMSU fell back to DuckDuckGo."
    message = f"{body}{provider_note}\n\nSources:\n{sources}"
    console.print(Panel(message, title="Web Answer"))
    _log_assistant_message(session_logger, message, workflow_id="web")


def _official_domains_for_query(query: str) -> tuple[str, ...]:
    text = query.lower()
    if "python" in text:
        return ("python.org",)
    if "node.js" in text or "nodejs" in text:
        return ("nodejs.org",)
    if "django" in text:
        return ("djangoproject.com",)
    if "react" in text:
        return ("react.dev",)
    return ()


def _grounded_release_answer(query: str, fetches: list[WebFetchResult]) -> str:
    """Resolve simple latest-version facts directly from first-party evidence."""
    text = query.lower()
    if not re.search(r"\b(current|latest|stable)\b", text) or not re.search(
        r"\b(version|release)\b", text
    ):
        return ""
    domains = _official_domains_for_query(query)
    if domains != ("python.org",):
        return ""
    candidates: set[tuple[int, int, int]] = set()
    for page in fetches:
        host = urlparse(getattr(page, "final_url", "") or page.url).hostname or ""
        if not any(host == domain or host.endswith("." + domain) for domain in domains):
            continue
        for match in re.finditer(r"\bPython\s+(\d+)\.(\d+)\.(\d+)\b", page.text):
            candidates.add(tuple(int(part) for part in match.groups()))
    if not candidates:
        return ""
    version = ".".join(str(part) for part in max(candidates))
    return f"The current stable Python release is **Python {version}**, according to Python.org."


def _is_useful_web_fetch(fetch: WebFetchResult) -> bool:
    text = fetch.text.strip()
    if len(text) < 120:
        return False
    lowered = text.lower()
    navigation_markers = sum(
        marker in lowered for marker in ("cookie", "subscribe", "sign in", "menu", "advertisement")
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
    if getattr(result, "console_errors", ()):
        body = f"{body}\n\nConsole/page errors:\n" + "\n".join(
            f"- {item}" for item in result.console_errors
        )
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
        # Acknowledgements: a bare "thanks" must never reach the tool loop,
        # where a small model will try to find something to do with it.
        # Deliberately NOT "ok"/"okay" - those are affirmative-continue signals
        # with their own routing.
        "thanks",
        "thank you",
        "thx",
        "ty",
    }


# Broadened small-talk detector. `_is_casual_prompt` only matches a bare single
# greeting ("hey"), so multi-word small talk ("hey how are you") slipped past it
# into the task router and came back as a "QA task" with a fabricated plan. A
# greeting token is stripped so "hey how are you" reduces to "how are you"; the
# REMAINDER must itself be small talk, so "hey, fix the login bug" is NOT caught.
_GREETING_TOKENS = frozenset(
    {"hi", "hello", "hey", "yo", "sup", "hiya", "heya", "howdy", "greetings", "hola"}
)

_SOCIAL_SMALL_TALK = frozenset(
    {
        "how are you",
        "how are you doing",
        "how are you doing today",
        "how are you today",
        "how r you",
        "how r u",
        "how are u",
        "how you doing",
        "how ya doing",
        "how is it going",
        "hows it going",
        "hows it going today",
        "how are things",
        "hows things",
        "how is everything",
        "hows everything",
        "hows life",
        "how is your day",
        "hows your day",
        "hows your day going",
        "whats up",
        "what is up",
        "whats good",
        "whats new",
        "how do you do",
        "how have you been",
        "you good",
        "are you ok",
        "are you okay",
        "good morning",
        "good afternoon",
        "good evening",
        "good day",
        "there",  # trailing token for "hey there"
    }
)


def _is_conversational_prompt(user_input: str) -> bool:
    """True for pure small talk - a greeting, acknowledgement, or social
    "how are you" opener that is the ENTIRE message. A greeting followed by an
    actual request ("hey, fix the login bug") is NOT conversational: only the
    leading greeting token is stripped and what remains must itself be small
    talk. Deliberately tighter than `_is_general_chat_prompt` (which is a broad
    "no project markers" test that also matches real questions like "explain
    the caching") so it can safely short-circuit straight to general chat."""
    if _is_casual_prompt(user_input):
        return True
    text = re.sub(r"[^\w\s]", "", user_input.lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return False
    if text in _SOCIAL_SMALL_TALK:
        return True
    tokens = text.split()
    if tokens and tokens[0] in _GREETING_TOKENS:
        rest = " ".join(tokens[1:]).strip()
        return rest == "" or rest in _SOCIAL_SMALL_TALK
    return False


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
            action_ledger=get_current_run(),
        )
    result = await CodeEditWorkflow(workspace, search=search, llm=llm, **kwargs).run(
        _strip_forced_prefix(user_input, "edit")
    )
    if getattr(result, "needs_input", False) and getattr(result, "question", ""):
        # The planner stopped the edit on a decision that is the user's (J1):
        # ask, store the question cross-turn, and let the answer re-dispatch
        # the request. Previously the planner's verdict was computed on this
        # path and silently ignored - only the chat loop ever acted on it.
        pending = {
            "question": result.question,
            "options": list(getattr(result, "options", []) or []),
            "allow_free_text": True,
            "source": "code_edit_upfront",
            "created_from_prompt": user_input,
        }
        body = format_question(pending)
        console.print(Panel(body, title="Need Input", border_style="cyan"))
        if session_logger is not None:
            try:
                session_logger.set_pending_question(pending)
            except Exception as exc:
                swallowed.record("repl.code_edit_pending_question", exc)
        _log_assistant_message(session_logger, body, workflow_id="code_edit")
        return
    if getattr(result, "used_full_rewrite", False):
        console.print(
            "[dim]The diff didn't parse cleanly, so I rewrote the file(s) in full instead.[/dim]"
        )
    message = _print_patch_result(
        "Code Edit", result.applied, result.changed_files, result.error, console
    )
    _log_assistant_message(session_logger, message, workflow_id="code_edit")


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
            action_ledger=get_current_run(),
        )
    result = await BugFixWorkflow(workspace, search=search, llm=llm, **kwargs).run(
        _strip_forced_prefix(user_input, "fix")
    )
    remapped = getattr(result, "remapped_paths", []) or []
    if remapped:
        lines = "\n".join(f"- {reported} -> {resolved}" for reported, resolved in remapped)
        console.print(
            f"[dim]The reported path(s) didn't exist; I edited the real workspace file(s):\n{lines}[/dim]"
        )
    if getattr(result, "used_full_rewrite", False):
        console.print(
            "[dim]The diff didn't parse cleanly, so I rewrote the file(s) in full instead.[/dim]"
        )
    message = _print_patch_result(
        "Bug Fix",
        result.applied,
        result.changed_files,
        result.error,
        console,
        verification_status=getattr(result, "verification_status", ""),
    )
    _log_assistant_message(session_logger, message, workflow_id="bug_fix")


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
        action_ledger = get_current_run()
        kwargs["patch_engine"] = PatchEngine(
            workspace,
            session_logger=session_logger,
            approval_manager=approval_manager,
            action_ledger=action_ledger,
        )
        kwargs["command_runner"] = CommandRunner(
            workspace,
            session_logger=session_logger,
            approval_manager=approval_manager,
            action_ledger=action_ledger,
        )
    result = await TestGenerationWorkflow(workspace, search=search, llm=llm, **kwargs).run(
        _strip_forced_prefix(user_input, "test-gen")
    )
    message = _print_patch_result(
        "Test Generation",
        result.applied,
        result.changed_files,
        result.error,
        console,
    )
    _log_assistant_message(session_logger, message, workflow_id="test_gen")


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
                    action_ledger=get_current_run(),
                )
            }
            if session_logger
            else {}
        ),
    ).apply_readme_update(request=_strip_forced_prefix(user_input, "docs"))
    message = _print_patch_result(
        "Documentation",
        result.applied,
        result.changed_files,
        result.error,
        console,
    )
    _log_assistant_message(session_logger, message, workflow_id="doc_gen")


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
            action_ledger=get_current_run(),
        ),
        session_logger=session_logger,
        long_running=is_long_running_enabled(workspace),
    ).run(project_dir)
    _print_django_test_result(result.final_result, console)
    if result.success:
        console.print(
            f"[green]Django tests passed after {len(result.iterations)} fix attempt(s).[/green]"
        )
        return
    console.print(
        Panel(
            result.error or "Django tests still failing.",
            title="Fix Loop Stopped",
            border_style="red",
        )
    )


def _print_patch_result(
    title: str,
    applied: bool,
    changed_files: list[str],
    error: str,
    console: Console,
    verification_status: str = "",
) -> str:
    if applied:
        files = "\n".join(f"- {path}" for path in changed_files) or "No files reported."
        body = files
        if verification_status:
            body = f"Applied changes:\n{files}\n\nVerified results:\n{verification_status}"
        console.print(Panel(body, title=f"{title} Applied", border_style="green"))
        return f"{title} applied.\n{body}"
    message = error or "No changes applied."
    console.print(Panel(message, title=f"{title} Not Applied", border_style="yellow"))
    return f"{title} not applied.\n{message}"


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
        return normalized[len(prefix) :].strip()
    return user_input


def _normalize_command_input(user_input: str) -> str:
    stripped = user_input.strip()
    if stripped.startswith("/"):
        return stripped[1:].strip()
    return stripped


def _thinking_status_for_input(user_input: str) -> str:
    # These labels describe LEGACY routing steps - "checking workspace PRD
    # files", "checking whether web lookup is needed" - and simple mode does
    # none of them. Live 2026-08-18 a prompt that merely contained the word
    # "prd" sat under "Checking workspace PRD files..." for ten minutes while
    # the model was in fact answering an ordinary chat turn, and the user
    # reasonably read the label as the thing that was stuck.
    if not _legacy_routing_enabled():
        return "[dim]Working...[/dim]"
    normalized = _normalize_command_input(user_input).lower()
    if _looks_like_capabilities_question(user_input):
        return "[dim]Checking which tools I actually have...[/dim]"
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
    from shamsu import __version__

    model = model_for_role("qa")
    tier = active_tier().value
    autonomy = "on" if is_long_running_enabled(workspace) else "off"
    runtime = status_text(collect_status())
    body = Text()
    body.append(f"SHAMSU v{__version__}", style="bold")
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


def _bottom_toolbar(workspace: Path, plan_mode: bool = False) -> str:
    autonomy = "on" if is_long_running_enabled(workspace) else "off"
    model = model_for_role("qa")
    # Plan mode changes what the NEXT prompt does (plan instead of act), so it
    # has to be visible - an invisible mode is a trap.
    mode = "  |  PLAN MODE (next prompt gets planned, not built)" if plan_mode else ""
    return f" {workspace}  |  model: {model}  |  autonomy: {autonomy}{mode}  |  /help  /exit "


class CachedBottomToolbar:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.plan_mode = False
        self.value = _bottom_toolbar(workspace)

    def refresh(self) -> None:
        self.value = _bottom_toolbar(self.workspace, self.plan_mode)

    def set_plan_mode(self, enabled: bool) -> None:
        self.plan_mode = enabled
        self.refresh()

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


def _make_prompt_session(
    workspace: Path, bottom_toolbar: Callable[[], str] | None = None
) -> PromptSession | None:
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


def _handle_compact(user_input: str, session_logger: Any, console: Any) -> None:
    """`/compact` shows the running summary; `/compact clear` forgets it.

    Compaction is otherwise invisible: it decides what the model still knows
    about earlier work, and until now there was no way to read it or correct it.
    """
    argument = user_input.split(None, 1)[1].strip().lower() if " " in user_input else ""
    try:
        summary, upto = session_logger.load_summary()
    except Exception as exc:  # noqa: BLE001 - a broken read is worth showing
        console.print(f"[red]Could not read the summary: {exc}[/red]")
        return

    if argument == "clear":
        try:
            session_logger.save_summary("", 1)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Could not clear it: {exc}[/red]")
            return
        console.print("[dim]Summary cleared. Earlier turns still exist in the transcript.[/dim]")
        return

    if not summary.strip():
        console.print(
            "[dim]Nothing compacted yet - the whole conversation still fits in the "
            "window. This fills in once older turns stop fitting.[/dim]"
        )
        return

    console.print(f"[dim]What the model is told about earlier work (through message {upto}):[/dim]")
    console.print(summary)
    console.print("[dim]`/compact clear` to forget it and rebuild from the transcript.[/dim]")


def _session_prompt_label(session_logger: Any, max_chars: int = 24) -> str:
    """`shamsu (asteroids)> ` - which thread you are talking to.

    Falls back to a bare `shamsu> ` if anything is missing: a decorative label
    must never be able to stop the REPL from accepting input.
    """
    try:
        title = str(getattr(getattr(session_logger, "metadata", None), "title", "") or "").strip()
    except Exception:
        title = ""
    if not title or title.lower() in {"shamsu session", "session", "untitled"}:
        return "shamsu> "
    if len(title) > max_chars:
        title = title[:max_chars].rstrip() + "..."
    return f"shamsu ({title})> "


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
    if args.command == "run":
        from shamsu.cli.noninteractive import run_cli

        exit_code = run_cli(args)
        if exit_code:
            raise SystemExit(exit_code)
        return
    console = Console()
    _install_console_status_tracker(console)

    try:
        workspace = resolve_workspace(args.workspace)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]", soft_wrap=True)
        sys.exit(2)

    from shamsu.runtime.state_upgrade import upgrade_workspace_state

    upgrade_report = upgrade_workspace_state(workspace)
    for warning in upgrade_report.warnings:
        console.print(f"[yellow]State upgrade warning: {warning}[/yellow]")

    # Resolve the active model tier (env var > persisted workspace choice >
    # default) before anything reads model_for_role(), including the banner.
    initialize_model_tier(workspace)
    # First run in this workspace: ask which tier, then download it here (with
    # progress) rather than at install time or silently mid-conversation.
    _maybe_prompt_first_run_tier(workspace, console)

    # Track this session so the last one to exit *can* free SHAMSU's Ollama
    # footprint. By default we now KEEP Ollama and its loaded models warm across
    # exits - repeatedly unloading/reloading an 8B model is what made the next
    # session stall for a long time on first use. Opt back into shutdown with
    # SHAMSU_SHUTDOWN_OLLAMA_ON_EXIT=1.
    session_pid = register_session()
    atexit.register(flush_memory_queues)
    if _os_env_flag("SHAMSU_SHUTDOWN_OLLAMA_ON_EXIT"):
        atexit.register(shutdown_if_last_session, session_pid)

    _print_startup_banner(workspace, console)
    _ensure_graphiti_ready_at_startup(workspace, console)
    _ensure_code_memory_ready_at_startup(workspace, console)
    if upgrade_report.initialized:
        from shamsu.runtime.doctor import run_first_run_checks, write_first_run_report

        first_run_report = run_first_run_checks(workspace)
        report_path = write_first_run_report(workspace, first_run_report)
        ready_count = sum(check.ok for check in first_run_report.checks)
        console.print(
            f"[dim]First-run checks: {ready_count}/{len(first_run_report.checks)} ready; "
            f"report: {report_path}[/dim]"
        )
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
    console.print(f"[dim]Trace: {read_trace_mode(workspace)}[/dim]")
    console.print("[dim]Type a prompt, or `/help` for commands.[/dim]\n")
    web_tool = WebTool(
        workspace=workspace,
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
    # One event loop for the whole session, so a cancelled request returns to
    # this prompt instead of ending the process (see _RequestRunner).
    global _REQUEST_RUNNER
    _REQUEST_RUNNER = _RequestRunner(console)
    # Plan mode: `/plan` with no task arms this, and the NEXT natural prompt is
    # planned instead of executed. Deliberately per-session (not persisted): a
    # mode that silently survives a restart would plan when you meant to build.
    plan_mode = False

    while True:
        try:
            # Name the thread in the prompt. `/sessions current` existed, but
            # nobody runs it every turn, so which conversation you were in was
            # invisible - and after a restart silently forked a new session it
            # was the difference between "resumed" and "everything is gone".
            prompt_label = _session_prompt_label(session_logger)
            if session is None:
                raw_input_text = input(prompt_label)
            else:
                raw_input_text = session.prompt([("class:prompt", prompt_label)])
        except EOFError:
            print("\nGoodbye.")
            break
        except KeyboardInterrupt:
            # Ctrl+C at an idle prompt clears the line; it does not quit. Ctrl+D
            # (EOF) and /exit are how you leave, which is the convention every
            # other shell the user is already in follows.
            console.print("[dim]Use /exit or Ctrl+D to leave.[/dim]")
            continue

        # Strip a stray leading BOM that piped stdin can prepend; it is
        # not whitespace, so .strip() alone leaves it and it would break slash
        # commands and pollute previews.
        user_input = raw_input_text.lstrip("\ufeff").strip()
        if not user_input:
            continue
        previous_user_prompt = session_logger.metadata.last_user_prompt
        logged_user_input = redact_remote_control_command(user_input)
        session_logger.log(
            "user.prompt",
            {"prompt": logged_user_input},
            "User submitted prompt",
            workflow_id="repl",
        )
        # Auto-name a still-placeholder session from its first meaningful,
        # natural-language prompt (slash commands never name a session).
        if not user_input.startswith("/"):
            session_manager.maybe_auto_title(session_logger, user_input)
        # A pending clarification question takes priority: interpret this reply
        # as its answer instead of routing it as a brand-new prompt. A slash
        # command clearly changes topic, so we let it clear the question below.
        if not user_input.startswith("/"):
            pending_question = session_logger.get_pending_question()
            if pending_question.get("question"):
                rewritten = _resolve_pending_question(
                    pending_question, user_input, workspace, console, session_logger
                )
                if rewritten is None:
                    # Declined/cancelled: a plan paused on this question must not
                    # linger and silently resume on some later, unrelated prompt.
                    _take_paused_plan(session_logger)
                    continue
                # If a plan stopped on that question, the answer resumes the plan
                # rather than starting an unrelated new request (gap J5).
                paused_plan = _take_paused_plan(session_logger)
                if paused_plan is not None:
                    ledger = start_run(workspace, user_input, session_logger=session_logger)
                    set_current_run(ledger)
                    try:
                        _run_request(
                            _resume_paused_plan(
                                paused_plan, rewritten, workspace, console, session_logger
                            )
                        )
                    except Exception as exc:
                        ledger.fail(str(exc))
                        clear_current_run()
                        _report_request_error(exc, console, session_logger)
                        continue
                    _finish_current_run(workspace, ledger)
                    clear_current_run()
                    continue
                user_input = rewritten
            else:
                continuation_message = _continuation_clarification(user_input, previous_user_prompt)
                if continuation_message is not None:
                    console.print(f"[yellow]{continuation_message}[/yellow]")
                    _log_assistant_message(
                        session_logger, continuation_message, workflow_id="clarification"
                    )
                    continue
        elif session_logger.get_pending_question().get("question"):
            # Topic change via slash command: drop the stale pending question,
            # and any plan paused on it - otherwise the plan would sit armed and
            # resume off some later, unrelated answer.
            session_logger.clear_pending_question()
            _take_paused_plan(session_logger)
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
            # Degraded, not hard-blocking: when Graphiti isn't set up for this
            # workspace, fall back to the local SQLite memory store so editing,
            # QA, and PRD builds still work instead of every prompt hitting a
            # "run /memory setup" wall. The startup banner already warned once;
            # only a hard rejection (e.g. a non-local URI) still stops here.
            memory_gate = MemoryService(workspace).ensure_ready_degraded()
            if not memory_gate.allowed:
                console.print(
                    Panel(
                        memory_gate.reason or REQUIRED_MEMORY_MESSAGE,
                        title="Graphiti Memory Required",
                        border_style="red",
                    )
                )
                continue
        if lowered_input in {"exit", "quit"}:
            print("Goodbye.")
            break
        if lowered_input == "help":
            _print_help(console)
            continue
        if lowered_input == "remote_control" or lowered_input.startswith("remote_control "):
            handle_remote_control_command(normalized_input, workspace, console)
            continue
        if lowered_input == "doctor":
            _handle_doctor(workspace, console)
            continue
        if lowered_input.startswith("memory"):
            _handle_memory(normalized_input, workspace, console, session_logger=session_logger)
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
        if lowered_input == "proceed":
            if not _resolve_proceed(workspace, console, session_logger):
                console.print(
                    "[yellow]Nothing to proceed - make a plan first with `plan <task>`.[/yellow]"
                )
            continue
        if lowered_input in _PLAN_MODE_OFF_COMMANDS:
            if plan_mode:
                plan_mode = False
                bottom_toolbar.set_plan_mode(False)
                console.print("[dim]Plan mode off.[/dim]")
            else:
                console.print("[dim]Plan mode is already off.[/dim]")
            continue
        if plan_mode and not user_input.startswith("/"):
            # Armed by a bare `/plan`: this prompt is the task to plan, whatever
            # it looks like. Slash commands still run normally (checked above),
            # so /help, /exit and friends keep working inside plan mode.
            # One plan per arming: a plan is now pending `proceed`, so drop back
            # to normal mode rather than planning every subsequent prompt.
            plan_mode = False
            bottom_toolbar.set_plan_mode(False)
            _run_plan_with_ledger(normalized_input, user_input, workspace, console, session_logger)
            continue
        if lowered_input == "plan" or lowered_input.startswith("plan "):
            # `/plan <task>` plans that task right away. Bare `/plan` turns plan
            # MODE on instead, so the task can be typed as the next prompt -
            # bare `/plan` used to just print a usage error.
            _, _, plan_task = normalized_input.partition(" ")
            if not plan_task.strip():
                plan_mode = True
                bottom_toolbar.set_plan_mode(True)
                _print_plan_mode_banner(console)
                continue
            plan_mode = False
            bottom_toolbar.set_plan_mode(False)
            _run_plan_with_ledger(plan_task, user_input, workspace, console, session_logger)
            continue
        if _looks_like_plan_request(lowered_input):
            # "make me a plan to add auth" in ordinary chat. The whole sentence
            # is the task: the planner reads it better than a stripped fragment.
            plan_mode = False
            bottom_toolbar.set_plan_mode(False)
            _run_plan_with_ledger(normalized_input, user_input, workspace, console, session_logger)
            continue
        if lowered_input.startswith("generate-django "):
            _handle_generate_django(
                normalized_input, workspace, console, session_logger=session_logger
            )
            continue
        if lowered_input.startswith("generate-prd "):
            _run_request(_handle_generate_prd(normalized_input, workspace, console, session_logger))
            continue
        if lowered_input.startswith("models"):
            _handle_models(normalized_input, console, workspace)
            continue
        if lowered_input.startswith("web "):
            _handle_web(
                normalized_input,
                console,
                web_tool,
                _make_llm_manager(session_logger, console, workspace),
            )
            continue
        if lowered_input.startswith("browse "):
            _handle_browse(normalized_input, console, browser_tool)
            continue
        if lowered_input.startswith("django"):
            if lowered_input.startswith("django fix-tests"):
                _run_request(
                    _handle_django_fix_tests(normalized_input, workspace, console, session_logger)
                )
            else:
                _handle_django(normalized_input, workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("compact"):
            _handle_compact(normalized_input, session_logger, console)
            continue
        if lowered_input.startswith("sessions"):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                session_logger = _handle_sessions(
                    normalized_input, session_manager, session_logger, console
                )
                web_tool.session_logger = session_logger
                browser_tool.session_logger = session_logger
            continue
        if lowered_input.startswith("permissions"):
            _handle_permissions(normalized_input, workspace, console)
            continue
        if lowered_input == "skills" or lowered_input.startswith("skills "):
            handle_skills_command(normalized_input, workspace, console)
            continue
        if lowered_input == "mcp" or lowered_input.startswith("mcp "):
            from shamsu.mcp.cli import handle_mcp_command

            handle_mcp_command(normalized_input, workspace, console, session_logger)
            continue
        if lowered_input.startswith("milestones"):
            _handle_milestones(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("taskmaster"):
            _handle_taskmaster(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("prd "):
            # Parsing/reparsing calls Taskmaster and the local model, so it
            # gets its own ActionLedger run - same reasoning as /tasks
            # execute|continue below (Taskmaster.md section 12).
            ledger = start_run(workspace, user_input, session_logger=session_logger)
            set_current_run(ledger)
            try:
                _handle_prd_command(normalized_input, workspace, console)
            except Exception as exc:
                ledger.fail(str(exc))
                clear_current_run()
                _report_request_error(exc, console, session_logger)
                continue
            _finish_current_run(workspace, ledger)
            clear_current_run()
            continue
        if lowered_input.startswith("context"):
            _handle_context(normalized_input, workspace, console)
            continue
        if lowered_input == "tasks" or lowered_input.startswith("tasks "):
            _tasks_tokens = lowered_input.split(maxsplit=2)
            if len(_tasks_tokens) > 1 and _tasks_tokens[1] in {"execute", "continue"}:
                search, _ = _build_search_agent(workspace, session_logger)
                llm = _make_llm_manager(session_logger, console, workspace)
                # Task execution goes through model calls, Codebase-Memory
                # queries, and PatchEngine mutations just like the natural-
                # language fallback path below - it needs the same active
                # ActionLedger run so those get recorded (see Taskmaster.md
                # section 12), not just its own `.shamsu/taskmaster/` bookkeeping.
                ledger = start_run(workspace, user_input, session_logger=session_logger)
                set_current_run(ledger)
                try:
                    _run_request(
                        _handle_tasks_execute(
                            normalized_input,
                            workspace,
                            console,
                            search,
                            llm,
                            session_logger=session_logger,
                        )
                    )
                except Exception as exc:
                    ledger.fail(str(exc))
                    clear_current_run()
                    _report_request_error(exc, console, session_logger)
                    continue
                _finish_current_run(workspace, ledger)
                clear_current_run()
            else:
                _handle_tasks(normalized_input, workspace, console)
            continue
        if lowered_input.startswith("autonomy"):
            _handle_autonomy(normalized_input, workspace, console)
            bottom_toolbar.refresh()
            continue
        if lowered_input.startswith("trace"):
            _handle_trace(normalized_input, workspace, console)
            continue
        if lowered_input == "debug" or lowered_input.startswith("debug "):
            _handle_debug(normalized_input, workspace, console)
            continue
        if lowered_input == "audit-log" or lowered_input.startswith("audit-log "):
            _handle_audit_log(normalized_input, workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("diagnostics"):
            _handle_diagnostics(normalized_input, workspace, console)
            continue
        if lowered_input == "undo":
            _handle_undo(workspace, console, session_logger=session_logger)
            continue
        if lowered_input.startswith("patch"):
            _handle_patch(normalized_input, workspace, console, session_logger=session_logger)
            continue
        # Checked before "log": /logs is the signpost to the log files on disk,
        # /log tails this session's event stream. Two different things.
        if lowered_input == "logs" or lowered_input.startswith("logs "):
            _handle_logs(normalized_input, workspace, console)
            continue
        if lowered_input == "log" or lowered_input.startswith("log "):
            with console.status(_thinking_status_for_input(user_input), spinner="dots"):
                _handle_log(normalized_input, session_logger, console)
            continue
        if lowered_input == "runs" or lowered_input.startswith("runs "):
            _handle_runs(normalized_input, workspace, console)
            continue
        _run_tokens = lowered_input.split(maxsplit=2)
        if lowered_input == "run" or (
            lowered_input.startswith("run ")
            and len(_run_tokens) > 1
            and _run_tokens[1] in _RUN_SUBCOMMANDS
        ):
            # "run" is also an ordinary English verb ("run the tests"), so only
            # claim it here when the next token is a recognized /run subcommand -
            # everything else falls through to the normal agent request path.
            _handle_run(normalized_input, workspace, console)
            continue

        # Follow-up resolution against a stored pending action: a bare
        # "yes"/"no"/"do it" resolves the prior action instead of entering the
        # model/tool loop as a fresh, context-free prompt. Dispatching a
        # confirmed action still passes through the normal approval gates, so
        # this never bypasses safety.
        dispatch_input = user_input
        pending_action = session_logger.get_pending_action()
        if pending_action.get("awaiting") == "prd_plan_selection":
            if is_negative(user_input):
                session_logger.clear_pending_action()
                console.print("[yellow]Cancelled the pending PRD plan execution.[/yellow]")
                continue
            if _looks_like_prd_slice_execution_reply(user_input):
                session_logger.clear_pending_action()
                ledger = start_run(workspace, user_input, session_logger=session_logger)
                set_current_run(ledger)
                try:
                    _run_request(
                        _execute_pending_prd_plan(
                            pending_action,
                            user_input,
                            workspace,
                            console,
                            session_logger,
                        )
                    )
                except Exception as exc:
                    ledger.fail(str(exc))
                    clear_current_run()
                    _report_request_error(exc, console, session_logger)
                    continue
                _finish_current_run(workspace, ledger)
                clear_current_run()
                continue
            # The user may be asking a side question while a PRD plan is pending.
            # Leave it armed unless they explicitly start, continue, run all, or cancel.
        if pending_action.get("awaiting") == "plan_continue":
            if is_negative(user_input):
                session_logger.clear_pending_action()
                console.print("[yellow]Cancelled the remaining plan steps.[/yellow]")
                continue
            if is_affirmative(user_input) or _looks_like_follow_plan(user_input) or _plan_autonomous_execution_requested(user_input):
                session_logger.clear_pending_action()
                ledger = start_run(workspace, user_input, session_logger=session_logger)
                set_current_run(ledger)
                try:
                    _run_request(
                        _execute_pending_plan(
                            pending_action,
                            workspace,
                            console,
                            session_logger,
                            run_all=(
                                is_long_running_enabled(workspace)
                                or _plan_autonomous_execution_requested(user_input)
                            ),
                        )
                    )
                except Exception as exc:
                    ledger.fail(str(exc))
                    clear_current_run()
                    _report_request_error(exc, console, session_logger)
                    continue
                _finish_current_run(workspace, ledger)
                clear_current_run()
                continue
            # The user may be asking a side question while plan steps remain.
        if pending_action.get("awaiting") == "plan_approval":
            # A plan is awaiting the user's go-ahead: "proceed"/"follow the plan"
            # executes it step by step; "no" discards it (the file is kept).
            if is_negative(user_input):
                session_logger.clear_pending_action()
                console.print(
                    "[yellow]Discarded the pending plan. The plan file is kept under .shamsu/plans/.[/yellow]"
                )
                continue
            if is_affirmative(user_input) or _looks_like_follow_plan(user_input) or _plan_autonomous_execution_requested(user_input):
                session_logger.clear_pending_action()
                ledger = start_run(workspace, user_input, session_logger=session_logger)
                set_current_run(ledger)
                try:
                    _run_request(
                        _execute_pending_plan(
                            pending_action,
                            workspace,
                            console,
                            session_logger,
                            run_all=(
                                is_long_running_enabled(workspace)
                                or _plan_autonomous_execution_requested(user_input)
                            ),
                        )
                    )
                except Exception as exc:
                    ledger.fail(str(exc))
                    clear_current_run()
                    _report_request_error(exc, console, session_logger)
                    continue
                _finish_current_run(workspace, ledger)
                clear_current_run()
                continue
            # Neither approval nor rejection: leave the plan pending and answer the
            # message normally (the user may be asking something else meanwhile).
        if pending_action:
            if is_negative(user_input):
                session_logger.clear_pending_action()
                console.print("[yellow]Cancelled the pending action.[/yellow]")
                continue
            if is_affirmative(user_input) and pending_action.get("awaiting") == "confirmation":
                origin = str(pending_action.get("created_from_prompt", "")).strip()
                session_logger.clear_pending_action()
                if origin:
                    dispatch_input = f"{origin}\n\n[User confirmed: proceed with this request.]"
                console.print("[dim]Resolving your confirmation against the pending action.[/dim]")

        ledger = start_run(workspace, dispatch_input, session_logger=session_logger)
        set_current_run(ledger)
        try:
            with console.status(_thinking_status_for_input(user_input), spinner="dots") as thinking:
                completed = _run_request(
                    _handle_request(
                        dispatch_input,
                        workspace,
                        console,
                        web_tool,
                        browser_tool,
                        previous_user_prompt=previous_user_prompt,
                        session_logger=session_logger,
                        thinking_status=thinking,
                    )
                )
        except Exception as exc:
            ledger.fail(str(exc))
            clear_current_run()
            _report_request_error(exc, console, session_logger)
            continue
        if not completed:
            # Record the interrupt on the run itself, so a cancelled turn reads
            # as cancelled rather than as whatever partial evidence it left.
            with contextlib.suppress(Exception):
                ledger.log_run_cancelled()
        _finish_current_run(workspace, ledger)
        clear_current_run()

    flush_memory_queues()
    browser_tool.close()
    if _REQUEST_RUNNER is not None:
        _REQUEST_RUNNER.close()


if __name__ == "__main__":
    main()
