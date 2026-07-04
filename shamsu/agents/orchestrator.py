"""Thin agent orchestration layer before model routing."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from shamsu.indexer.walker import ensure_index
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
    def __init__(self, workspace_root: Path, session_logger: SessionLogger | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.session_logger = session_logger
        self.workspace_tool = WorkspaceTool(self.workspace_root)
        self.mention_resolver = MentionResolver(self.workspace_root)

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
        ensure_index(self.workspace_root, self.session_logger)
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
        f"Index exists: {(workspace / '.shamsu' / 'index.db').exists()}",
        "Available tools: workspace files, indexed search, @file context, web search with approval, browser with approval, patch preview/apply with approval.",
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
