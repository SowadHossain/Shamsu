"""Thin agent orchestration layer before model routing."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from shamsu.abstract.service import AbstractService
from shamsu.action_ledger.context import get_current_run
from shamsu.memory.service import MemoryService, REQUIRED_MEMORY_MESSAGE
from shamsu.session.manager import SessionLogger
from shamsu.session.memory import ConversationMemory
from shamsu.tools.workspace import MentionContext, MentionResolver, WorkspaceTool, render_mention_context


@dataclass(frozen=True)
class AgentResult:
    handled: bool = False
    title: str = ""
    message: str = ""
    effective_input: str = ""
    context: str = ""
    mentions: list[MentionContext] = field(default_factory=list)
    action: str = ""


class AgentOrchestrator:
    def __init__(
        self,
        workspace_root: Path,
        session_logger: SessionLogger | None = None,
        abstract_service: AbstractService | None = None,
        memory_service: MemoryService | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.session_logger = session_logger
        self.workspace_tool = WorkspaceTool(self.workspace_root)
        self.mention_resolver = MentionResolver(self.workspace_root)
        self.abstract_service = abstract_service or AbstractService(self.workspace_root)
        self.memory_service = memory_service or MemoryService(self.workspace_root)

    def run(self, user_input: str) -> AgentResult:
        memory = ConversationMemory.from_session(self.session_logger)
        effective_input = memory.resolve_followup(user_input)
        mentions = self.mention_resolver.resolve_all(effective_input)
        context = render_mention_context(mentions)
        self._log_resolution(user_input, effective_input, mentions, context)

        if _asks_workspace_location(effective_input):
            return AgentResult(
                handled=True,
                title="Current Workspace",
                message=f"I am working in:\n{self.workspace_root}",
                effective_input=effective_input,
                action="workspace.location",
            )
        if _asks_workspace_files(effective_input):
            return AgentResult(
                handled=True,
                title="Workspace Files",
                message=self.workspace_tool.list_files().render(),
                effective_input=effective_input,
                action="workspace.files",
            )
        if _asks_capabilities(effective_input):
            # Answer from the live tool registry, deterministically, before the
            # memory gate and model routing. Otherwise this reaches the tool-less
            # QA brain, which invents tools/files from stale session context.
            return AgentResult(
                handled=True,
                title="SHAMSU Tools",
                message=_render_capabilities(self.workspace_root),
                effective_input=effective_input,
                action="capabilities",
            )
        if _asks_prd_files(effective_input):
            prds = self.workspace_tool.find_prds()
            if not prds:
                message = (
                    "I couldn't find a PRD file in this workspace. "
                    "Add a `.md`, `.txt`, or `.pdf` PRD (e.g. named `*prd*` or "
                    "`Product Requirements*`), then ask again."
                )
            else:
                body = "\n".join(f"- {path.as_posix()}" for path in prds)
                message = f"I found these PRD-like files:\n{body}"
            return AgentResult(
                handled=True,
                title="Workspace PRD Files",
                message=message,
                effective_input=effective_input,
                action="workspace.prds",
            )
        if mentions and _should_show_mentions(effective_input, mentions):
            return AgentResult(
                handled=True,
                title="Mention Context",
                message=context or "No readable @file context found.",
                effective_input=effective_input,
                context=context,
                mentions=mentions,
                action="mentions.read",
            )
        if _asks_weather_without_location(effective_input):
            return AgentResult(
                handled=True,
                title="Location Needed",
                message="Which location should I check the weather for?",
                effective_input=effective_input,
                action="web.needs_location",
            )
        # Degraded, not hard-blocking: a workspace that has never run
        # `/memory setup` (a fresh checkout, a PRD-build target directory, ...)
        # must still be able to edit code and answer questions. When Graphiti
        # is unavailable, MemoryService transparently falls back to the local
        # SQLite store, so agent work proceeds instead of bricking the whole
        # session behind a "run /memory setup" wall. Only a hard rejection
        # (e.g. a rejected non-local URI) still blocks.
        memory_gate = self.memory_service.ensure_ready_degraded()
        ledger = get_current_run()
        if ledger:
            ledger.log_memory_status_checked(memory_gate.allowed, memory_gate.reason or "")
        if not memory_gate.allowed:
            return AgentResult(
                handled=True,
                title="Graphiti Memory Required",
                message=memory_gate.reason or REQUIRED_MEMORY_MESSAGE,
                effective_input=effective_input,
                context=context,
                mentions=mentions,
                action="memory.blocked",
            )
        gate = self.abstract_service.ensure_ready()
        if not gate.allowed:
            return AgentResult(
                handled=True,
                title="Codebase-Memory MCP Required",
                message=gate.reason,
                effective_input=effective_input,
                context=context,
                mentions=mentions,
                action="abstract.blocked",
            )
        return AgentResult(
            handled=False,
            effective_input=effective_input,
            context=_agent_context(self.workspace_root, self.workspace_tool, memory, context),
            mentions=mentions,
        )

    def _log_resolution(
        self,
        user_input: str,
        effective_input: str,
        mentions: list[MentionContext],
        context: str,
    ) -> None:
        if self.session_logger:
            self.session_logger.log(
                "agent.context",
                {
                    "prompt": user_input,
                    "effective_prompt": effective_input,
                    "mentions": [item.mention for item in mentions],
                    "has_context": bool(context),
                },
                "Agent resolved prompt context",
                workflow_id="agent",
            )


def _agent_context(
    workspace: Path,
    workspace_tool: WorkspaceTool,
    memory: ConversationMemory,
    mention_context: str,
) -> str:
    listing = workspace_tool.list_files(limit=12).render(limit=12)
    parts = [
        f"Workspace root: {workspace}",
        "Available tools: workspace files, Codebase-Memory MCP search, @file context, web search with approval, browser with approval, patch preview/apply with approval.",
        "Top-level workspace files:",
        listing,
        "Recent conversation:",
        memory.build_context(max_turns=8),
    ]
    if mention_context:
        parts.extend(["Mentioned file context:", mention_context])
    return "\n\n".join(parts)


def _asks_workspace_location(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
        for phrase in (
            "what folder are you in",
            "where are you right now",
            "where are you rn",
            "what directory are you in",
            "what workspace are you in",
            "current folder",
            "current directory",
            "current workspace",
        )
    )


def _asks_workspace_files(text: str) -> bool:
    lowered = text.lower()
    return any(
        phrase in lowered
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
            "show the folder",
            "list this repo",
            "what files do i have",
            "what files are in this workspace",
        )
    )


_CAPABILITY_PHRASES = (
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


def _asks_capabilities(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in _CAPABILITY_PHRASES)


def _render_capabilities(workspace_root: Path) -> str:
    """Human-readable capability answer built from the real tool registry so the
    list always matches what SHAMSU can actually call."""
    from shamsu.tools.agent_tools import AgentToolRegistry

    schemas = AgentToolRegistry(workspace_root).tool_schemas()
    lines = ["Tools I can call directly:"]
    for schema in schemas:
        fn = schema.get("function", {})
        name = str(fn.get("name", ""))
        # First sentence only - some descriptions carry multi-sentence usage notes.
        description = str(fn.get("description", "")).split(". ", 1)[0].strip().rstrip(".")
        lines.append(f"- {name}: {description}")
    lines.append("")
    lines.append("Higher-level workflows:")
    for item in (
        "Answer questions about this workspace's indexed code",
        "Edit code and apply reviewed diffs (with your approval)",
        "Fix bugs from a traceback, failing command, or error message",
        "Generate tests, write docs, and audit the project",
        "Parse a PRD and generate a Django project",
        "Search the web and inspect local apps in a browser (with approval)",
    ):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("Run /help for the full command list.")
    return "\n".join(lines)


def _asks_prd_files(text: str) -> bool:
    lowered = text.lower()
    mentions_prd = "prd" in lowered or "product requirements" in lowered
    return mentions_prd and any(
        phrase in lowered
        for phrase in (
            "what prds",
            "which prds",
            "find prd",
            "find the prd",
            "prd files",
            "what product requirements",
            "which product requirements",
            "find product requirements",
        )
    )


def _should_show_mentions(text: str, mentions: list[MentionContext]) -> bool:
    lowered = text.lower()
    if any(item.kind == "ambiguous" or item.error for item in mentions):
        return True
    return any(
        phrase in lowered
        for phrase in (
            "read @",
            "show @",
            "open @",
            "what is in @",
            "summarize @",
            "check @",
        )
    )


def _asks_weather_without_location(text: str) -> bool:
    lowered = text.lower()
    if not any(word in lowered for word in ("weather", "forecast", "temperature")):
        return False
    return not re.search(
        r"\b(in|for|at)\s+[a-z][a-z\s,.-]{2,}(?:\s+(today|now|tomorrow))?\??$",
        lowered,
    )


