"""Tool registry exposed to the local ReAct chat loop."""
from __future__ import annotations

import difflib
import json
import re
import traceback
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from collections.abc import Callable, Iterable
from typing import Any

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.indexer.policy import walk_workspace_files
from shamsu.mcp.manager import MCPManager, get_shared_mcp_manager, summarize_mcp_result
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.retriever.documents import (
    DOCUMENT_SOURCE_SUFFIXES,
    DocumentError,
    DocumentStore,
    PreparedDocument,
)
from shamsu.retriever.search import SearchAgent
from shamsu.tools.policy import (
    ExecutionPhase,
    evaluate_phase_tool_policy,
    normalize_phase,
    phase_allowed_tools,
)
from shamsu.runtime.advanced_capabilities import AdvancedCapability, normalize_advanced_capabilities
from shamsu.runtime.task_state import current_task_context
from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.commands import command_may_write_workspace, redact
from shamsu.safety.dry_run import DryRunRecorder
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.session.manager import SessionLogger
from shamsu.skills.ingest import (
    MAX_REFERENCE_SOURCE_CHARS,
    ReferenceIngestError,
    is_web_reference,
    prepare_reference,
    validate_local_reference_path,
)
from shamsu.skills.loader import discover_skills
from shamsu.tools.executor import CommandRunner
from shamsu.tools.git import GitCommandResult, GitTool
from shamsu.tools.logical import LogicalToolLayer, all_logical_tool_names, expand_tool_aliases
from shamsu.tools.path_resolve import (
    _find_files_by_query,
    _find_path_candidates,
    _format_path_candidates,
    _normalize_workspace_path,
    _path_exists_case_insensitive,
    _strong_path_candidates,
    _walk_workspace_files,
)
from shamsu.tools.workspace import (
    DOCUMENT_EXTENSIONS,
    TEXT_EXTENSIONS,
    WorkspaceTool,
    extract_document_text,
    is_readable_text_file,
)
from shamsu.types import ApprovalRequest

# Extensions the read tools will return as text (a superset of WorkspaceTool's
# TEXT_EXTENSIONS so common source files like .jsx/.vue/.go are readable).
_READABLE_TEXT_EXTENSIONS = frozenset(
    TEXT_EXTENSIONS
    | {
        ".jsx",
        ".mjs",
        ".cjs",
        ".vue",
        ".svelte",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".rb",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cc",
        ".cs",
        ".php",
        ".sql",
        ".xml",
        ".svg",
        ".gradle",
        ".graphql",
        ".proto",
    }
)
_READABLE_FILENAMES = frozenset({"Dockerfile", "Makefile", ".gitignore", ".env", ".dockerignore"})

# Max characters returned for a whole-file read before truncation kicks in.
MAX_READ_CHARS = 24000

# Total characters one serialized tool result may spend on its string fields.
# Raised from an effective 6000 because the window is now 32k with a ~24k prompt
# budget - a source file the model must EDIT is worth ~6k tokens of it, and the
# old ceiling silently cut ordinary files. `_share_budget` decides who gets what.
COMPACT_VALUE_LIMIT = 24000

# No field is squeezed below this, so metadata stays readable.
MIN_FIELD_CHARS = 500

_MCP_PATH_ARGUMENTS = frozenset(
    {
        "path",
        "filepath",
        "file",
        "source",
        "destination",
        "target",
        "directory",
        "dir",
    }
)


def _mcp_mutation_paths(arguments: dict[str, Any]) -> list[str]:
    """Extract explicit paths from a mutating MCP call for scope enforcement."""
    paths: list[str] = []
    for key, value in arguments.items():
        normalized_key = re.sub(r"[_-]", "", str(key)).lower()
        if normalized_key not in _MCP_PATH_ARGUMENTS:
            continue
        values = value if isinstance(value, list) else [value]
        paths.extend(str(item) for item in values if isinstance(item, (str, Path)) and str(item))
    return paths


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    message: str
    data: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(
            {"ok": self.ok, "message": self.message, "data": _compact_value(self.data)},
            ensure_ascii=True,
        )


class AgentToolRegistry:
    def __init__(
        self,
        workspace_root: Path,
        approval_func=ask_approval,
        session_logger: SessionLogger | None = None,
        approval_manager: ApprovalManager | None = None,
        action_ledger: ActionLedger | None = None,
        web_tool: Any | None = None,
        mcp_manager: MCPManager | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.workspace_tool = WorkspaceTool(self.workspace_root)
        self.transactions = TransactionWorkspace(self.workspace_root)
        self.document_store = DocumentStore(self.workspace_root)
        self.action_ledger = action_ledger
        self.command_runner = CommandRunner(
            self.workspace_root,
            approval_func=approval_func,
            session_logger=session_logger,
            approval_manager=approval_manager,
            action_ledger=action_ledger,
        )
        self.approval_func = approval_func
        self.session_logger = session_logger
        self.approval_manager = approval_manager or ApprovalManager(
            approval_func,
            session_logger,
            action_ledger=action_ledger,
        )
        # Injected for tests; lazily constructed on first use otherwise (gap D1:
        # the loop had no way to look anything up mid-task - web was a separate
        # pre-routed path decided before the agent ever started).
        self._web_tool = web_tool
        self._mcp = mcp_manager or get_shared_mcp_manager(
            self.workspace_root, session_logger=session_logger
        )

        # Git commands run through the same CommandRunner so command safety,
        # approval, logging, timeout handling, and diagnostics still apply.
        self.git_tool = GitTool(self.workspace_root, command_runner=self.command_runner)

        # Set when the user's prompt explicitly forbids file changes. This is a
        # HARD deny on every mutating tool, deliberately independent of the
        # approval layer: "--approval allow" answers "may SHAMSU write without
        # asking me?", not "may SHAMSU ignore what I just told it?". A dogfood
        # run under broad approval overwrote a script the prompt had told it not
        # to touch, because read-only intent had no enforcement point at all.
        self._read_only = False

        # Set for a dry run. Mutating tools then report a synthetic success and
        # record what they WOULD have done, so the agent keeps planning instead
        # of stopping at a denial - see shamsu/safety/dry_run.py for why a
        # denying approver cannot produce a preview.
        self._dry_run: DryRunRecorder | None = None

        # Set for a SCOPED read-only request ("create X, do not modify any
        # other files"). A blanket refusal would fail the request from the
        # other direction, so instead the named targets stay writable and
        # everything else is denied - which is exactly what the user said.
        self._allowed_write_paths: set[str] | None = None
        # Long-running project builds need a narrower evidence boundary than
        # the workspace itself. Without it, a model can discover similarly
        # named files from old builds and repair the wrong project.
        self._allowed_read_paths: set[str] | None = None
        self._user_request = ""
        self._required_tool_prefix = ""
        self._allowed_tool_names: set[str] | None = None
        self._scope_expansion_handler: Callable[[str, str], ToolResult] | None = None
        self._current_phase: ExecutionPhase | None = None
        self._task_risk = "medium"
        self._enabled_advanced_capabilities: frozenset[AdvancedCapability] = frozenset()
        self._logical_tools_enabled = False
        self._logical_tools = LogicalToolLayer(
            self,
            lambda ok, message, data: ToolResult(ok, message, data),
            phase_getter=self._effective_phase,
            risk_getter=lambda: self._task_risk,
            enabled_advanced_getter=lambda: self._enabled_advanced_capabilities,
        )
        self._resolved_read_aliases: dict[str, str] = {}

    def set_read_only(self, read_only: bool) -> None:
        self._read_only = bool(read_only)

    def set_dry_run(self, recorder: DryRunRecorder | None) -> None:
        self._dry_run = recorder

    def set_allowed_write_paths(self, paths: Iterable[str] | None) -> None:
        """Restrict mutations to `paths`. None removes the restriction."""
        if paths is None:
            self._allowed_write_paths = None
            return
        self._allowed_write_paths = {_normalize_workspace_path(p).lower() for p in paths if p}

    def sole_allowed_write_path(self) -> str:
        """Return the exact orchestrator-owned mutation target, when unique."""
        allowed = self._allowed_write_paths
        return next(iter(allowed)) if allowed is not None and len(allowed) == 1 else ""

    def allowed_write_paths(self) -> list[str]:
        """Return the orchestrator-owned mutation scope for repair handoffs."""
        return sorted(self._allowed_write_paths or ())

    def set_scope_expansion_handler(
        self,
        handler: Callable[[str, str], ToolResult] | None,
    ) -> None:
        self._scope_expansion_handler = handler

    def set_allowed_read_paths(self, paths: Iterable[str] | None) -> None:
        """Restrict local discovery and reads to project paths."""
        if paths is None:
            self._allowed_read_paths = None
            return
        self._allowed_read_paths = {
            _normalize_workspace_path(p).lower() for p in paths if _normalize_workspace_path(p)
        }

    def has_scoped_reads(self) -> bool:
        return self._allowed_read_paths is not None

    def _inside_allowed_read_scope(self, path: str) -> bool:
        allowed = self._allowed_read_paths
        if allowed is None:
            return True
        clean = _normalize_workspace_path(path) or path
        try:
            normalized = self.sandbox.validate(clean).relative_to(self.workspace_root).as_posix().lower()
        except (SecurityError, ValueError):
            normalized = clean.lower()
        return any(
            normalized == item or normalized.startswith(item.rstrip("/") + "/")
            for item in allowed
        )

    def _scoped_read_path(self, path: str) -> str:
        """Resolve a short model path inside the sole active project root."""
        normalized = _normalize_workspace_path(path) or path
        allowed = self._allowed_read_paths
        if allowed is None or self._inside_allowed_read_scope(normalized):
            return normalized
        if len(allowed) == 1 and normalized not in {"", "."}:
            root = next(iter(allowed)).rstrip("/")
            return f"{root}/{normalized.lstrip('/')}"
        return normalized

    def _scoped_write_path(self, path: str) -> str:
        """Resolve a short write path under an explicitly scoped project root."""
        normalized = _normalize_workspace_path(path) or path
        allowed = self._allowed_write_paths
        if allowed is None:
            return normalized
        if any(
            normalized.lower() == item
            or normalized.lower().startswith(item.rstrip("/") + "/")
            for item in allowed
        ):
            return normalized
        # Only project runs set the same single root for both reads and writes.
        # A normal scoped edit such as allowed_write_paths={"notes.md"} must
        # still reject an unrelated short path instead of nesting it below a
        # filename.
        if len(allowed) == 1 and allowed == self._allowed_read_paths:
            root = next(iter(allowed)).rstrip("/")
            return f"{root}/{normalized.lstrip('/')}"
        if len(allowed) == 1:
            target = next(iter(allowed)).rstrip("/")
            short = normalized.lower().lstrip("/")
            if target == short or target.endswith("/" + short):
                return target
        return normalized

    def _scoped_candidates(self, candidates: Iterable[str]) -> list[str]:
        return [candidate for candidate in candidates if self._inside_allowed_read_scope(candidate)]

    def _resolved_read_path(self, path: str) -> str:
        """Reuse a path that a prior read safely resolved from the same alias."""
        normalized = _normalize_workspace_path(path) or path
        try:
            requested = self.sandbox.validate(normalized)
        except SecurityError:
            return normalized
        if requested.exists():
            return normalized
        resolved = self._resolved_read_aliases.get(normalized.lower(), "")
        if not resolved or not self._inside_allowed_read_scope(resolved):
            return normalized
        try:
            target = self.sandbox.validate(resolved)
        except SecurityError:
            return normalized
        return resolved if target.is_file() else normalized

    def _allowed_by_unique_basename_scope(self, normalized: str, allowed: set[str]) -> bool:
        """Allow a resolved path when the scope named its unique basename."""
        name = PurePosixPath(normalized).name.lower()
        if not name:
            return False
        basename_scopes = {
            item
            for item in allowed
            if "/" not in item.strip("/") and PurePosixPath(item).name.lower() == name
        }
        if not basename_scopes:
            return False

        matches: set[str] = set()
        for relative in _walk_workspace_files(self.workspace_root):
            rel_path = _normalize_workspace_path(str(relative)).lower()
            if PurePosixPath(rel_path).name != name:
                continue
            if self._inside_allowed_read_scope(rel_path):
                matches.add(rel_path)
        return len(matches) == 1 and normalized.lower() in matches

    def set_user_request(self, request: str) -> None:
        self._user_request = str(request or "")

    def require_tool_prefix(self, prefix: str | None) -> None:
        """Prevent an explicit tool request from being replaced by a shell guess."""
        self._required_tool_prefix = str(prefix or "")

    def set_allowed_tools(self, names: Iterable[str] | None) -> None:
        """Restrict model-visible and executable tools for an orchestrated step.

        The list is canonicalized across both tool vocabularies here, at the one
        boundary every caller passes through, rather than in each caller's
        constant: an allowlist naming ``write_file`` must also admit the
        ``file.patch`` it is executed as, or the step runs with no usable tools
        at all. See ``expand_tool_aliases``.
        """
        if names is None:
            self._allowed_tool_names = None
            return
        self._allowed_tool_names = expand_tool_aliases(names)

    def use_logical_tools(self, enabled: bool = True) -> None:
        """Expose compact logical tools to the model while keeping low-level internals."""
        self._logical_tools_enabled = bool(enabled)

    def model_tool_names(self) -> set[str]:
        if self._logical_tools_enabled:
            return all_logical_tool_names() | {
                "list_files",
                "file_info",
                "read_file",
                "find_file",
                "grep_files",
                "search_index",
                "write_file",
                "edit_file",
                "append_file",
                "run_command",
                "request_scope_expansion",
            }
        return {
            str((schema.get("function") or {}).get("name") or "")
            for schema in self.tool_schemas()
        }

    def set_phase(self, phase: str | ExecutionPhase | None, *, task_risk: str | None = None) -> None:
        """Set the runtime phase used by deterministic tool policy."""
        self._current_phase = normalize_phase(phase)
        if task_risk is not None:
            self._task_risk = str(task_risk or "medium")

    def set_enabled_advanced_capabilities(self, names: Iterable[str] | None) -> None:
        """Enable advanced capability gates after benchmark readiness is proven."""
        self._enabled_advanced_capabilities = normalize_advanced_capabilities(
            {str(name) for name in names or []}
        )

    def clear_phase(self) -> None:
        self._current_phase = None
        self._task_risk = "medium"

    def _effective_phase(self) -> ExecutionPhase:
        if self._current_phase is not None:
            return self._current_phase
        context = current_task_context()
        if context is not None:
            try:
                state = context.store.load_task(context.task_id)
                if state is not None:
                    return normalize_phase(state.current_phase)
            except Exception:
                pass
        return ExecutionPhase.AUTHOR

    def _phase_context_active(self) -> bool:
        return self._current_phase is not None or current_task_context() is not None

    def _tool_is_allowed(self, name: str) -> bool:
        allowed = self._allowed_tool_names
        if allowed is None:
            return True
        return name in allowed or any(
            pattern.endswith("*") and name.startswith(pattern[:-1])
            for pattern in allowed
        )

    def is_tool_allowed(self, name: str) -> bool:
        """Public capability check for harness-owned fallback paths."""
        return self._tool_is_allowed(name) and self._tool_allowed_by_phase(name)

    def _logical_tool_allowed(self, name: str) -> bool:
        if self._allowed_tool_names is None:
            return True
        return name in self._allowed_tool_names

    def _tool_allowed_by_phase(self, name: str) -> bool:
        if not self._phase_context_active():
            return True
        phase_tools = set(
            phase_allowed_tools(
                self._effective_phase(),
                enabled_advanced_capabilities=self._enabled_advanced_capabilities,
            )
        )
        return name in phase_tools or any(
            pattern.endswith("*") and name.startswith(pattern[:-1])
            for pattern in phase_tools
        )

    def _phase_policy_denial(self, name: str, arguments: dict[str, Any]) -> ToolResult | None:
        if not self._phase_context_active():
            return None
        decision = evaluate_phase_tool_policy(
            name,
            arguments,
            phase=self._effective_phase(),
            task_risk=self._task_risk,
            enabled_advanced_capabilities=self._enabled_advanced_capabilities,
        )
        if decision.allowed:
            return None
        return ToolResult(
            False,
            f"Tool {name} denied by phase contract: {decision.reason}",
            decision.denial_payload(),
        )

    def _outside_allowed_scope(self, path: str) -> ToolResult | None:
        allowed = self._allowed_write_paths
        if allowed is None:
            return None
        clean = _normalize_workspace_path(path) or path
        try:
            normalized = self.sandbox.validate(clean).relative_to(self.workspace_root).as_posix().lower()
        except (SecurityError, ValueError):
            normalized = clean.lower()
        if any(
            normalized == item or normalized.startswith(item.rstrip("/") + "/")
            for item in allowed
        ):
            return None
        if self._allowed_by_unique_basename_scope(normalized, allowed):
            return None
        if self.action_ledger:
            try:
                self.action_ledger.log_event(
                    "out_of_contract_write_rejected",
                    filepath=path,
                    allowed=sorted(allowed),
                )
            except Exception:
                pass
        return ToolResult(
            False,
            f"Write rejected: {path} is outside this task's locked write scope. "
            "This request allowed changes only to "
            + ", ".join(sorted(allowed))
            + ". No file was modified.",
            {
                "scoped_read_only": True,
                "out_of_contract_write": True,
                "filepath": path,
                "allowed": sorted(allowed),
            },
        )

    def _planned(self, action: str, path: str, detail: str, size_bytes: int = 0) -> ToolResult:
        """Record an intended mutation and report it as done, without doing it."""
        assert self._dry_run is not None
        self._dry_run.record(action, path, detail=detail, size_bytes=size_bytes)
        return ToolResult(
            True,
            f"[dry run] Would {action} {path}. Nothing was written. "
            "Continue as though this succeeded.",
            {
                "dry_run": True,
                "planned_action": action,
                "filepath": path,
                "resolved_filepath": path,
                "detail": detail,
                "size_bytes": size_bytes,
            },
        )

    def _read_only_refusal(self, action: str, target: str) -> ToolResult:
        return ToolResult(
            False,
            f"Refused to {action} {target}: this request said not to change files. "
            "No file was modified. Ask again without that restriction if you want the change.",
            {"read_only": True, "blocked_action": action, "filepath": target},
        )

    def tool_schemas(self) -> list[dict[str, Any]]:
        if self._logical_tools_enabled:
            # MCP tools are appended, not replaced. Returning only the logical
            # set made every registered MCP tool invisible the moment logical
            # tools were switched on - the same "registered but unreachable"
            # failure as an allowlist naming the wrong vocabulary.
            return self._logical_tools.schemas(allowed_names=self._allowed_tool_names) + [
                schema
                for schema in self._mcp.tool_schemas()
                if self._tool_is_allowed(str((schema.get("function") or {}).get("name") or ""))
            ]
        local_schemas = [
            _tool_schema(
                "list_files",
                "List files and folders inside the workspace.",
                {
                    "path": {
                        "type": "string",
                        "description": "Relative folder path inside the workspace.",
                        "default": ".",
                    }
                },
            ),
            _tool_schema(
                "read_file",
                "Read a text file inside the workspace. If the path is wrong it returns "
                "'candidates' (close matches) instead of failing blindly; when exactly one "
                "candidate matches it reads that file and reports resolved_filepath. Pass "
                "start_line/end_line to read only part of a large file.",
                {
                    "filepath": {"type": "string", "description": "Relative file path."},
                    "start_line": {
                        "type": "string",
                        "description": "Optional 1-based first line to read.",
                        "default": "",
                    },
                    "end_line": {
                        "type": "string",
                        "description": "Optional 1-based last line to read.",
                        "default": "",
                    },
                },
                required=["filepath"],
            ),
            _tool_schema(
                "file_info",
                "Check whether a path is a file, directory, or missing before acting on it. "
                "Returns kind, size, and candidates for a missing path. Use before editing an "
                "uncertain path.",
                {"filepath": {"type": "string", "description": "Relative file path."}},
                required=["filepath"],
            ),
            _tool_schema(
                "find_file",
                "Find files by name or partial path when an expected path is missing. Use right "
                "after read_file says 'Not a file'. Example: find_file query=App.tsx.",
                {
                    "query": {"type": "string", "description": "File name or partial path to search for."},
                    "limit": {
                        "type": "string",
                        "description": "Max results. Default 20, max 100.",
                        "default": "20",
                    },
                },
                required=["query"],
            ),
            _tool_schema(
                "grep_files",
                "Search file contents for a symbol or text when you know what to look for but not "
                "which file. Returns filepath, line number, and the matching line.",
                {
                    "query": {"type": "string", "description": "Exact text/symbol to search for."},
                    "path": {
                        "type": "string",
                        "description": "Relative folder to search under. Default '.'.",
                        "default": ".",
                    },
                    "extensions": {
                        "type": "string",
                        "description": "Optional comma-separated extension filter, e.g. '.ts,.tsx,.js'.",
                        "default": "",
                    },
                    "limit": {
                        "type": "string",
                        "description": "Max matches. Default 50, max 200.",
                        "default": "50",
                    },
                },
                required=["query"],
            ),
            _tool_schema(
                "edit_file",
                "Safely replace exact text in an EXISTING file (with approval + rollback backup). "
                "Prefer this for small, targeted changes. old_string must match exactly once "
                "unless replace_all=true. Use append_file to add content at the end of an existing "
                "file, and write_file only for new files or full rewrites.",
                {
                    "filepath": {"type": "string", "description": "Relative file path (must exist)."},
                    "old_string": {"type": "string", "description": "Exact text to replace."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                    "replace_all": {
                        "type": "string",
                        "description": "Use true to replace every occurrence. Default false.",
                        "default": "false",
                    },
                },
                required=["filepath", "old_string", "new_string"],
            ),
            _tool_schema(
                "append_file",
                "Append content to the END of an EXISTING text file (with approval + rollback "
                "backup). Use this when adding a new function, test, section, or block without "
                "replacing existing text. A separating newline is added when needed. This never "
                "creates a missing file; use write_file to create one.",
                {
                    "filepath": {"type": "string", "description": "Relative file path (must exist)."},
                    "content": {"type": "string", "description": "Content to append."},
                },
                required=["filepath", "content"],
            ),
            _tool_schema(
                "write_file",
                "Create a new file or fully rewrite an existing one. Always pass the COMPLETE new "
                "file content (it overwrites). For a small change to an existing file, prefer "
                "edit_file or append_file. If a matching file already exists at a different path "
                "this refuses and returns candidates rather than creating a duplicate. Pass content "
                "as plain text; SHAMSU also accepts a raw '# write_file: <path>' fenced block, so "
                "you never have to escape code.",
                {
                    "filepath": {"type": "string", "description": "Relative file path."},
                    "content": {"type": "string", "description": "Complete file content."},
                    "overwrite": {
                        "type": "string",
                        "description": (
                            "Use true to replace an existing file. Default true for model calls; "
                            "new files are always created."
                        ),
                        "default": "true",
                    },
                },
                required=["filepath", "content"],
            ),
            _tool_schema(
                "move_file",
                "Move or rename a file inside the workspace. Use this instead of writing a new "
                "copy and leaving the old one behind, and instead of shell mv/move. Backed up, so "
                "it can be undone. Refuses if the destination already exists.",
                {
                    "source": {"type": "string", "description": "Existing relative file path."},
                    "destination": {"type": "string", "description": "New relative file path."},
                },
                required=["source", "destination"],
            ),
            _tool_schema(
                "delete_file",
                "Delete a workspace file. The file is backed up first, so a deletion can be undone. "
                "Only delete when the task clearly calls for it; if several files could be the "
                "intended target, call ask_user instead of guessing.",
                {"filepath": {"type": "string", "description": "Relative file path to delete."}},
                required=["filepath"],
            ),
            _tool_schema(
                "run_command",
                "Run a workspace-bound command through SHAMSU's command runner.",
                {
                    "command": {"type": "string", "description": "Command to run."},
                    "cwd": {
                        "type": "string",
                        "description": "Relative working directory inside the workspace.",
                        "default": ".",
                    },
                },
                required=["command"],
            ),
            _tool_schema(
                "search_index",
                "Search SHAMSU's workspace index.",
                {"query": {"type": "string", "description": "Search query."}},
                required=["query"],
            ),
            _tool_schema(
                "web_search",
                "Search the web for CURRENT or EXTERNAL information the workspace cannot answer: "
                "library APIs, error messages from third-party tools, versions, documentation. "
                "Requires user approval. Never use it for anything about this workspace's own "
                "code - use search_index/grep_files for that.",
                {"query": {"type": "string", "description": "Search query."}},
                required=["query"],
            ),
            _tool_schema(
                "fetch_url",
                "Fetch one web page's readable text (e.g. a documentation page found via "
                "web_search). Requires user approval.",
                {"url": {"type": "string", "description": "Absolute http(s) URL."}},
                required=["url"],
            ),
            _tool_schema(
                "ingest_docs",
                "Register local Markdown, text, or PDF documentation, or one public documentation "
                "URL. Small references become workspace skills; large sources and PDFs become "
                "chunked, cited documents under .shamsu/documents. Future coding prompts that "
                "name a registered library receive relevant excerpts automatically. URL fetching "
                "and workspace writes require approval.",
                {
                    "source": {
                        "type": "string",
                        "description": "Workspace-relative .md/.txt/.pdf/.docx path or absolute http(s) URL.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Library/reference name used for future matching, e.g. react-query.",
                        "default": "",
                    },
                    "mode": {
                        "type": "string",
                        "description": "auto (default), reference for a small skill, or document for chunked retrieval.",
                        "enum": ["auto", "reference", "document"],
                        "default": "auto",
                    },
                },
                required=["source"],
            ),
            _tool_schema(
                "search_docs",
                "Search registered documentation by meaning and keywords. Returns bounded excerpts "
                "with document, page or line, and section citations. Use document to narrow the "
                "search to a named manual/library. This tool is read-only.",
                {
                    "query": {"type": "string", "description": "Question or search terms."},
                    "document": {
                        "type": "string",
                        "description": "Optional registered document name or id.",
                        "default": "",
                    },
                    "top_k": {
                        "type": "string",
                        "description": "Maximum excerpts. Default 5, max 20.",
                        "default": "5",
                    },
                },
                required=["query"],
            ),
            _tool_schema(
                "ask_docs",
                "Retrieve citation-backed evidence from registered documentation for a question. "
                "Answer only from the returned excerpts and cite each factual claim. This tool "
                "is read-only and falls back to keyword/paged retrieval if embeddings are absent.",
                {
                    "question": {"type": "string", "description": "Question to answer from the docs."},
                    "document": {
                        "type": "string",
                        "description": "Optional registered document name or id.",
                        "default": "",
                    },
                    "top_k": {
                        "type": "string",
                        "description": "Maximum evidence excerpts. Default 6, max 20.",
                        "default": "6",
                    },
                },
                required=["question"],
            ),
            _tool_schema(
                "summarize_docs",
                "Create a bounded extractive map-reduce summary of one registered document. "
                "The summary samples the full chunk set and retains page/section citations. "
                "This tool is read-only.",
                {
                    "document": {
                        "type": "string",
                        "description": "Registered document name or id.",
                    },
                    "max_tokens": {
                        "type": "string",
                        "description": "Approximate output budget. Default 1200, max 3000.",
                        "default": "1200",
                    },
                },
                required=["document"],
            ),

            # -----------------------------------------------------------------
            # Git read tools
            # -----------------------------------------------------------------
            _tool_schema(
                "git_status",
                "Show short git status for the workspace. Use before committing or pushing.",
                {},
            ),
            _tool_schema(
                "git_status_full",
                "Show full git status for the workspace.",
                {},
            ),
            _tool_schema(
                "git_diff",
                "Show unstaged git diff for the workspace.",
                {},
            ),
            _tool_schema(
                "git_diff_staged",
                "Show staged git diff for the workspace.",
                {},
            ),
            _tool_schema(
                "git_diff_file",
                "Show git diff for one file.",
                {"filepath": {"type": "string", "description": "Relative file path."}},
                required=["filepath"],
            ),
            _tool_schema(
                "git_branch",
                "Show the current git branch.",
                {},
            ),
            _tool_schema(
                "git_branches",
                "List git branches.",
                {
                    "all_branches": {
                        "type": "string",
                        "description": "Use true to include remote branches.",
                        "default": "false",
                    }
                },
            ),
            _tool_schema(
                "git_remote",
                "Show configured git remotes.",
                {},
            ),
            _tool_schema(
                "git_log",
                "Show recent git commits.",
                {
                    "limit": {
                        "type": "string",
                        "description": "Number of commits to show. Default 10, max 100.",
                        "default": "10",
                    }
                },
            ),
            _tool_schema(
                "git_unpushed_commits",
                "Show commits that exist locally but not on the remote branch. Use before pushing.",
                {
                    "remote": {
                        "type": "string",
                        "description": "Remote name. Default origin.",
                        "default": "origin",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name. If omitted, current branch is used.",
                        "default": "",
                    },
                    "limit": {
                        "type": "string",
                        "description": "Number of commits to show. Default 20, max 100.",
                        "default": "20",
                    },
                },
            ),

            # -----------------------------------------------------------------
            # Git mutation tools
            # -----------------------------------------------------------------
            _tool_schema(
                "git_add",
                "Stage specific files for commit. Always inspect git_status and git_diff first.",
                {
                    "paths": {
                        "type": "string",
                        "description": "Comma-separated relative file paths to stage.",
                    }
                },
                required=["paths"],
            ),
            _tool_schema(
                "git_init",
                "Initialize a new git repository in the workspace. Use when git_status reports this is not a git repository.",
                {},
            ),
            _tool_schema(
                "git_add_all",
                "Stage all workspace changes. Use only when the user clearly wants all changes staged.",
                {},
            ),
            _tool_schema(
                "git_commit",
                "Create a git commit with a message. Always inspect git_status and git_diff or git_diff_staged first.",
                {
                    "message": {
                        "type": "string",
                        "description": "Commit message.",
                    }
                },
                required=["message"],
            ),
            _tool_schema(
                "git_create_branch",
                "Create a new git branch. Optionally check it out.",
                {
                    "branch": {
                        "type": "string",
                        "description": "New branch name.",
                    },
                    "checkout": {
                        "type": "string",
                        "description": "Use true to switch to the new branch. Default true.",
                        "default": "true",
                    },
                },
                required=["branch"],
            ),
            _tool_schema(
                "git_checkout",
                "Switch to an existing git branch.",
                {
                    "branch": {
                        "type": "string",
                        "description": "Branch name.",
                    }
                },
                required=["branch"],
            ),
            _tool_schema(
                "git_restore",
                "Restore file changes. This may discard local edits. Use only when the user explicitly asks.",
                {
                    "paths": {
                        "type": "string",
                        "description": "Comma-separated relative file paths to restore.",
                    },
                    "staged": {
                        "type": "string",
                        "description": "Use true to unstage instead of restoring working tree changes.",
                        "default": "false",
                    },
                },
                required=["paths"],
            ),
            _tool_schema(
                "git_stash_push",
                "Create a git stash. Use when the user wants to save local changes temporarily.",
                {
                    "message": {
                        "type": "string",
                        "description": "Optional stash message.",
                        "default": "",
                    },
                    "include_untracked": {
                        "type": "string",
                        "description": "Use true to include untracked files.",
                        "default": "false",
                    },
                },
            ),
            _tool_schema(
                "git_stash_list",
                "List git stashes.",
                {},
            ),
            _tool_schema(
                "git_stash_pop",
                "Apply and remove the latest git stash. This can modify files.",
                {},
            ),

            # -----------------------------------------------------------------
            # Git remote/network tools
            # -----------------------------------------------------------------
            _tool_schema(
                "git_fetch",
                "Fetch from a git remote.",
                {
                    "remote": {
                        "type": "string",
                        "description": "Remote name. Default origin.",
                        "default": "origin",
                    },
                    "prune": {
                        "type": "string",
                        "description": "Use true to prune deleted remote branches.",
                        "default": "false",
                    },
                },
            ),
            _tool_schema(
                "git_pull",
                "Pull from a git remote. Use with care if the workspace has local changes.",
                {
                    "remote": {
                        "type": "string",
                        "description": "Remote name. Default origin.",
                        "default": "origin",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name. If omitted, git decides based on tracking branch.",
                        "default": "",
                    },
                },
            ),
            _tool_schema(
                "git_push",
                "Push the current or specified branch to a remote. Use only when the user explicitly asks to push.",
                {
                    "remote": {
                        "type": "string",
                        "description": "Remote name. Default origin.",
                        "default": "origin",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name. If omitted, current branch is used.",
                        "default": "",
                    },
                    "set_upstream": {
                        "type": "string",
                        "description": "Use true to push with -u and set upstream.",
                        "default": "false",
                    },
                },
            ),
            _tool_schema(
                "request_scope_expansion",
                "Request explicit permission to add one workspace path to the current task's locked write scope.",
                {
                    "filepath": {
                        "type": "string",
                        "description": "Workspace-relative file path to add.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Concise reason this task requires the path.",
                    },
                },
                required=["filepath", "reason"],
            ),
            _tool_schema(
                "ask_user",
                "Ask the user a question when the answer is THEIRS to give: choosing between "
                "valid approaches or designs, naming, scope, anything destructive or hard to "
                "undo, an ambiguous target (several matching files), or required input that "
                "tools cannot find. Asking one good question is cheap; acting on a wrong "
                "guess is expensive. Look up plain facts with find_file/grep_files/read_file "
                "yourself instead of asking. Calling this ends your turn and waits for the "
                "user's answer.",
                {
                    "question": {"type": "string", "description": "The question to ask the user."},
                    "options": {
                        "type": "array",
                        "description": "Optional list of choices, each {label, description}.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "description": {"type": "string"},
                            },
                        },
                    },
                    "allow_free_text": {
                        "type": "boolean",
                        "description": "Whether the user may answer in free text instead of a listed option.",
                        "default": True,
                    },
                },
                required=["question"],
            ),
        ]
        schemas = local_schemas + self._mcp.tool_schemas()
        return [
            schema
            for schema in schemas
            if self.is_tool_allowed(str((schema.get("function") or {}).get("name") or ""))
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if name == "request_scope_expansion":
                return self.request_scope_expansion(
                    str(arguments.get("filepath") or ""),
                    str(arguments.get("reason") or ""),
                )
            if self._logical_tools_enabled:
                alias = None
                if not self._logical_tools.is_logical_tool(name):
                    alias = self._logical_tools.alias(name, arguments)
                logical_name, logical_args = alias or (name, arguments)
                if self._logical_tools.is_logical_tool(logical_name):
                    if not self._logical_tool_allowed(logical_name):
                        return ToolResult(
                            False,
                            f"Tool {logical_name} is not allowed for the current orchestrated step.",
                            {
                                "blocked_tool": logical_name,
                                "requested_tool": name,
                                "current_phase": self._effective_phase().value,
                                "allowed_tools": sorted(self._allowed_tool_names or []),
                                "reason": "Logical tool is not in the active plan step's allowed tools.",
                            },
                        )
                    return self._logical_tools.execute(logical_name, logical_args)
            phase_denial = self._phase_policy_denial(name, arguments)
            if phase_denial is not None:
                return phase_denial
            if not self._tool_is_allowed(name):
                return ToolResult(
                    False,
                    f"Tool {name} is not allowed for the current orchestrated step.",
                    {
                        "blocked_tool": name,
                        "requested_tool": name,
                        "current_phase": self._effective_phase().value,
                        "allowed_tools": sorted(self._allowed_tool_names or []),
                        "reason": "Tool is not in the active plan step's allowed tools.",
                    },
                )
            if (
                self._required_tool_prefix
                and not name.startswith(self._required_tool_prefix)
                and name != "ask_user"
            ):
                return ToolResult(
                    False,
                    f"This request explicitly requires a {self._required_tool_prefix} tool. "
                    f"Do not substitute {name}; call one of the registered "
                    f"{self._required_tool_prefix} tools.",
                    {
                        "required_tool_prefix": self._required_tool_prefix,
                        "blocked_tool": name,
                    },
                )
            if name.startswith("mcp__"):
                return self._execute_mcp(name, arguments)
            if name == "list_files":
                return self.list_files(str(arguments.get("path") or "."))
            if name == "read_file":
                return self.read_file(
                    str(arguments.get("filepath") or ""),
                    start_line=arguments.get("start_line"),
                    end_line=arguments.get("end_line"),
                )
            if name == "file_info":
                return self.file_info(str(arguments.get("filepath") or ""))
            if name == "find_file":
                return self.find_file(
                    str(arguments.get("query") or ""),
                    limit=_as_int(arguments.get("limit"), default=20, minimum=1, maximum=100),
                )
            if name == "grep_files":
                return self.grep_files(
                    str(arguments.get("query") or ""),
                    str(arguments.get("path") or "."),
                    str(arguments.get("extensions") or ""),
                    limit=_as_int(arguments.get("limit"), default=50, minimum=1, maximum=200),
                )
            if name == "edit_file":
                return self.edit_file(
                    str(arguments.get("filepath") or ""),
                    str(arguments.get("old_string") or ""),
                    str(arguments.get("new_string") or ""),
                    replace_all=_as_bool(arguments.get("replace_all")),
                )
            if name == "append_file":
                return self.append_file(
                    str(arguments.get("filepath") or ""),
                    str(arguments.get("content") or ""),
                )
            if name == "write_file":
                # The model-facing tool always overwrites: small models forget an
                # overwrite flag, get blocked, and then hallucinate success. The
                # internal `overwrite` param stays for callers that need it.
                return self.write_file(
                    str(arguments.get("filepath") or ""),
                    str(arguments.get("content") or ""),
                    overwrite=_as_bool(arguments.get("overwrite"), default=True),
                )
            if name == "move_file":
                return self.move_file(
                    str(arguments.get("source") or ""),
                    str(arguments.get("destination") or ""),
                )
            if name == "delete_file":
                return self.delete_file(str(arguments.get("filepath") or ""))
            if name == "run_command":
                return self.run_command(
                    str(arguments.get("command") or ""),
                    str(arguments.get("cwd") or "."),
                )
            if name == "request_scope_expansion":
                return self.request_scope_expansion(
                    str(arguments.get("filepath") or ""),
                    str(arguments.get("reason") or ""),
                )
            if name == "search_index":
                return self.search_index(str(arguments.get("query") or ""))
            if name == "web_search":
                return self.web_search(str(arguments.get("query") or ""))
            if name == "fetch_url":
                return self.fetch_url(str(arguments.get("url") or ""))
            if name == "ingest_docs":
                return self.ingest_docs(
                    str(arguments.get("source") or ""),
                    str(arguments.get("name") or ""),
                    str(arguments.get("mode") or "auto"),
                )
            if name == "search_docs":
                return self.search_docs(
                    str(arguments.get("query") or ""),
                    str(arguments.get("document") or ""),
                    top_k=_as_int(arguments.get("top_k"), default=5, minimum=1, maximum=20),
                )
            if name == "ask_docs":
                return self.ask_docs(
                    str(arguments.get("question") or ""),
                    str(arguments.get("document") or ""),
                    top_k=_as_int(arguments.get("top_k"), default=6, minimum=1, maximum=20),
                )
            if name == "summarize_docs":
                return self.summarize_docs(
                    str(arguments.get("document") or ""),
                    max_tokens=_as_int(
                        arguments.get("max_tokens"),
                        default=1200,
                        minimum=100,
                        maximum=3000,
                    ),
                )

            # -----------------------------------------------------------------
            # Git read tools
            # -----------------------------------------------------------------
            if name == "git_status":
                status = self.git_tool.status()
                return ToolResult(
                    True,
                    "Read git status.",
                    {
                        "is_git_repo": status.is_git_repo,
                        "is_dirty": status.is_dirty,
                        "changed_files": status.changed_files,
                        "raw_output": status.raw_output,
                        "error": status.error,
                    },
                )

            if name == "git_status_full":
                return _git_tool_result(self.git_tool.status_full())

            if name == "git_diff":
                return _git_tool_result(self.git_tool.diff_result())

            if name == "git_diff_staged":
                return _git_tool_result(self.git_tool.diff_staged())

            if name == "git_diff_file":
                return _git_tool_result(self.git_tool.diff_file(str(arguments.get("filepath") or "")))

            if name == "git_branch":
                return _git_tool_result(self.git_tool.branch())

            if name == "git_branches":
                return _git_tool_result(
                    self.git_tool.branches(
                        all_branches=_as_bool(arguments.get("all_branches")),
                    )
                )

            if name == "git_remote":
                return _git_tool_result(self.git_tool.remote())

            if name == "git_log":
                return _git_tool_result(
                    self.git_tool.log(
                        limit=_as_int(arguments.get("limit"), default=10, minimum=1, maximum=100),
                    )
                )

            if name == "git_unpushed_commits":
                return _git_tool_result(
                    self.git_tool.unpushed_commits(
                        remote=str(arguments.get("remote") or "origin"),
                        branch=str(arguments.get("branch") or ""),
                        limit=_as_int(arguments.get("limit"), default=20, minimum=1, maximum=100),
                    )
                )

            # -----------------------------------------------------------------
            # Git mutation tools
            # -----------------------------------------------------------------
            if name == "git_add":
                return _git_tool_result(
                    self.git_tool.add(
                        _split_csv(arguments.get("paths")),
                    )
                )

            if name == "git_init":
                return _git_tool_result(self.git_tool.init())

            if name == "git_add_all":
                return _git_tool_result(self.git_tool.add_all())

            if name == "git_commit":
                return _git_tool_result(self.git_tool.commit(str(arguments.get("message") or "")))

            if name == "git_create_branch":
                return _git_tool_result(
                    self.git_tool.create_branch(
                        branch=str(arguments.get("branch") or ""),
                        checkout=_as_bool(arguments.get("checkout"), default=True),
                    )
                )

            if name == "git_checkout":
                return _git_tool_result(self.git_tool.checkout(str(arguments.get("branch") or "")))

            if name == "git_restore":
                return _git_tool_result(
                    self.git_tool.restore(
                        paths=_split_csv(arguments.get("paths")),
                        staged=_as_bool(arguments.get("staged")),
                    )
                )

            if name == "git_stash_push":
                return _git_tool_result(
                    self.git_tool.stash_push(
                        message=str(arguments.get("message") or ""),
                        include_untracked=_as_bool(arguments.get("include_untracked")),
                    )
                )

            if name == "git_stash_list":
                return _git_tool_result(self.git_tool.stash_list())

            if name == "git_stash_pop":
                return _git_tool_result(self.git_tool.stash_pop())

            # -----------------------------------------------------------------
            # Git remote/network tools
            # -----------------------------------------------------------------
            if name == "git_fetch":
                return _git_tool_result(
                    self.git_tool.fetch(
                        remote=str(arguments.get("remote") or "origin"),
                        prune=_as_bool(arguments.get("prune")),
                    )
                )

            if name == "git_pull":
                return _git_tool_result(
                    self.git_tool.pull(
                        remote=str(arguments.get("remote") or "origin"),
                        branch=str(arguments.get("branch") or ""),
                    )
                )

            if name == "git_push":
                return _git_tool_result(
                    self.git_tool.push(
                        remote=str(arguments.get("remote") or "origin"),
                        branch=str(arguments.get("branch") or ""),
                        set_upstream=_as_bool(arguments.get("set_upstream")),
                    )
                )

            if name == "find_file":
                return self.find_file(str(arguments.get("query") or ""))
            if name == "grep_files":
                return self.grep_files(
                    str(arguments.get("query") or ""),
                    str(arguments.get("path") or "."),
                )
            if name == "ask_user":
                return self.ask_user(
                    str(arguments.get("question") or ""),
                    arguments.get("options"),
                    bool(arguments.get("allow_free_text", True)),
                )
            return ToolResult(False, f"Unknown tool: {name}", {"tool": name})
        except Exception as exc:
            traceback_path = ""
            if self.action_ledger:
                traceback_path = self.action_ledger.record_exception(
                    "tool_execution",
                    name,
                    traceback.format_exc(),
                )
            return ToolResult(
                False,
                str(exc),
                {
                    "tool": name,
                    "exception_class": type(exc).__name__,
                    "exception_message": str(exc),
                    "phase": "tool_execution",
                    "traceback_path": traceback_path,
                },
            )

    def request_scope_expansion(self, filepath: str, reason: str) -> ToolResult:
        if self._scope_expansion_handler is None:
            return ToolResult(
                False,
                "Scope expansion is not available for this task.",
                {"filepath": filepath, "reason": reason, "scope_expansion": False},
            )
        return self._scope_expansion_handler(filepath, reason)

    def _execute_mcp(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        tool = self._mcp.get_tool(name)
        if tool is None:
            return ToolResult(False, f"Unknown MCP tool: {name}", {"tool": name})
        arguments = dict(arguments)
        # Small models commonly express "the workspace root" as POSIX `/`.
        # On Windows the filesystem MCP resolves that to the drive root, which
        # is outside the advertised MCP Root. Keep the call inside SHAMSU's
        # sandbox by resolving only this unambiguous shorthand.
        if str(arguments.get("path", "")).strip() in {"/", "\\"}:
            arguments["path"] = str(self.workspace_root)
        config = self._mcp.config.servers[tool.server]
        effective_read_only = tool.name in config.read_only_tools or (
            config.trust_tool_annotations and tool.read_only
        )
        permission = config.tool_permissions.get(
            tool.name, config.tool_permissions.get(name, "ask")
        )
        metadata = {
            "mcp_server": tool.server,
            "mcp_tool": tool.name,
            "transport": config.transport,
            "read_only": effective_read_only,
            "server_read_only_hint": tool.read_only,
            "destructive": tool.destructive,
            "effective_arguments": arguments,
        }
        if permission == "deny":
            return ToolResult(
                False,
                f"MCP tool {tool.server}/{tool.name} is denied by configuration.",
                {**metadata, "permission": "deny"},
            )
        if self._read_only and not effective_read_only:
            return ToolResult(
                False,
                f"Refused MCP tool {tool.server}/{tool.name}: this request said not to make "
                "changes, and the tool is not configured as trusted read-only.",
                {**metadata, "read_only_request": True},
            )
        if self._allowed_write_paths is not None and not effective_read_only:
            scoped_paths = _mcp_mutation_paths(arguments)
            if not scoped_paths:
                return ToolResult(
                    False,
                    f"Refused MCP tool {tool.server}/{tool.name}: this request limits file "
                    "changes, but SHAMSU could not prove which path the external tool would touch.",
                    {
                        **metadata,
                        "scoped_read_only": True,
                        "allowed": sorted(self._allowed_write_paths),
                    },
                )
            for scoped_path in scoped_paths:
                outside = self._outside_allowed_scope(scoped_path)
                if outside is not None:
                    return ToolResult(False, outside.message, {**metadata, **outside.data})
        if self._dry_run is not None and not effective_read_only:
            self._dry_run.record(
                "call_mcp_tool",
                f"{tool.server}/{tool.name}",
                detail=redact(json.dumps(arguments, ensure_ascii=True, default=str)),
            )
            return ToolResult(
                True,
                f"[dry run] Would call MCP tool {tool.server}/{tool.name}. Nothing was sent.",
                {**metadata, "dry_run": True},
            )

        needs_approval = permission != "allow" and (
            config.approval == "always"
            or (config.approval == "writes" and not effective_read_only)
        )
        if needs_approval:
            preview = redact(json.dumps(arguments, ensure_ascii=True, default=str))
            approved = self.approval_manager.ask(
                ApprovalRequest(
                    action_type="mcp_tool",
                    description=f"Call external MCP tool {tool.server}/{tool.name}",
                    risk_level="high" if tool.destructive else "medium",
                    preview=preview[:3000],
                    reason=(
                        "This sends data to an external MCP server and may change external state."
                        if not effective_read_only
                        else "This sends a query to an external MCP server."
                    ),
                )
            )
            if not approved:
                return ToolResult(
                    False,
                    f"MCP tool call denied by user: {tool.server}/{tool.name}",
                    {**metadata, "approval": "denied"},
                )
        transaction_id = ""
        local_paths: list[str] = []
        if not effective_read_only:
            for candidate in _mcp_mutation_paths(arguments):
                try:
                    relative = (
                        self.sandbox.validate(candidate)
                        .relative_to(self.workspace_root)
                        .as_posix()
                    )
                except (SecurityError, ValueError):
                    continue
                if relative not in local_paths:
                    local_paths.append(relative)
            if local_paths:
                operations = [
                    {"op": "mcp_tool", "path": path, "tool": name}
                    for path in local_paths
                ]
                transaction_id = self.transactions.begin(
                    f"External MCP tool {tool.server}/{tool.name}",
                    operations,
                    destructive=bool(tool.destructive),
                )
                for path in local_paths:
                    self.transactions.backup_file(transaction_id, path)
                if self.action_ledger:
                    self.action_ledger.log_mutation_started(
                        transaction_id,
                        f"External MCP tool {tool.server}/{tool.name}",
                    )
        try:
            raw = self._mcp.call(name, arguments)
        except Exception as exc:
            if transaction_id:
                manifest = self.transactions.finalize(transaction_id, "failed", str(exc))
                if self.action_ledger:
                    self.action_ledger.log_mutation_finished(
                        transaction_id,
                        "failed",
                        touched_files=local_paths,
                        rollback_available=bool(manifest.get("backups")),
                        error=str(exc),
                        operations=list(manifest.get("operations", [])),
                        before_hashes=dict(manifest.get("before_hashes", {})),
                        after_hashes=dict(manifest.get("after_hashes", {})),
                        backups=dict(manifest.get("backups", {})),
                    )
            return ToolResult(
                False,
                f"MCP tool {tool.server}/{tool.name} failed: {exc}",
                {**metadata, "exception_class": exc.__class__.__name__},
            )
        message, data = summarize_mcp_result(raw)
        is_error = bool(data.get("is_error"))
        if transaction_id:
            for path in local_paths:
                self.transactions.record_after(transaction_id, path)
            status = "failed" if is_error else "applied"
            manifest = self.transactions.finalize(
                transaction_id,
                status,
                message if is_error else "",
            )
            rollback_available = bool(local_paths)
            if self.action_ledger:
                self.action_ledger.log_mutation_finished(
                    transaction_id,
                    status,
                    touched_files=local_paths,
                    rollback_available=rollback_available,
                    error=message if is_error else "",
                    operations=list(manifest.get("operations", [])),
                    before_hashes=dict(manifest.get("before_hashes", {})),
                    after_hashes=dict(manifest.get("after_hashes", {})),
                    backups=dict(manifest.get("backups", {})),
                )
            metadata.update(
                {
                    "transaction_id": transaction_id,
                    "touched_files": local_paths,
                    "rollback_available": rollback_available,
                }
            )
        return ToolResult(
            not is_error,
            message if message else f"MCP tool {tool.server}/{tool.name} completed.",
            {**metadata, **data},
        )

    def list_files(self, path: str = ".") -> ToolResult:
        normalized = _normalize_workspace_path(path) or "."
        if self._allowed_read_paths is not None and normalized == ".":
            roots = sorted(self._allowed_read_paths)
            existing = []
            for root in roots:
                target = self.sandbox.validate(root)
                if target.is_dir():
                    files = [
                        f"[file] {root}/{str(relative).replace(chr(92), '/')}"
                        for relative in _walk_workspace_files(target)
                    ]
                    if len(files) > 120:
                        hidden = len(files) - 120
                        files = [*files[:120], f"... {hidden} more file(s) not shown"]
                    existing.append(
                        f"{root}/\n" + ("\n".join(files) if files else "(empty)")
                    )
                else:
                    existing.append(f"{root}/ (not created yet)")
            return ToolResult(
                True,
                "Listed files inside the active project scope.",
                {"path": ".", "listing": "\n".join(existing), "read_scope": roots},
            )
        normalized = self._scoped_read_path(normalized)
        if not self._inside_allowed_read_scope(normalized):
            return ToolResult(
                False,
                f"Refused to list {path}: it is outside the active project read scope.",
                {"path": path, "read_scope": sorted(self._allowed_read_paths or ())},
            )
        target = self.sandbox.validate(normalized)
        if not target.is_dir():
            return ToolResult(False, f"Not a directory: {normalized}", {"path": normalized})
        listing = WorkspaceTool(target).list_files().render()
        return ToolResult(True, "Listed files.", {"path": normalized, "listing": listing})

    def read_file(
        self,
        filepath: str,
        start_line: Any = None,
        end_line: Any = None,
    ) -> ToolResult:
        normalized = _normalize_workspace_path(filepath)
        if not normalized:
            return ToolResult(False, "Missing filepath.", {"filepath": filepath, "candidates": []})
        normalized = self._scoped_read_path(normalized)
        if not self._inside_allowed_read_scope(normalized):
            return ToolResult(
                False,
                f"Refused to read {filepath}: it is outside the active project read scope.",
                {
                    "filepath": filepath,
                    "candidates": [],
                    "read_scope": sorted(self._allowed_read_paths or ()),
                },
            )

        # Documents SHAMSU extracts rather than reads as text. Keyed off the
        # shared set, not a literal: when `.docx` became a supported PRD format
        # this check still said `.pdf`, so the model was told the PRD it had
        # been pointed at was "not a supported text file" - the same failure
        # that derailed the 2026-08-01 dogfood for PDFs.
        if PurePosixPath(normalized).suffix.lower() in DOCUMENT_EXTENSIONS:
            return self._read_pdf(normalized)

        try:
            target = self.sandbox.validate(normalized)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"filepath": normalized, "candidates": []})

        if target.is_dir():
            listing = WorkspaceTool(target).list_files().render()
            return ToolResult(
                False,
                f"Not a file: {normalized} is a directory. Pass a file path inside it.",
                {"filepath": normalized, "kind": "directory", "listing": listing, "candidates": []},
            )

        if target.is_file():
            return self._read_existing_file(normalized, normalized, target, start_line, end_line)

        # Missing: try a case-insensitive resolution first, then candidates.
        ci = _path_exists_case_insensitive(self.workspace_root, normalized)
        if ci is not None and ci.is_file():
            resolved = ci.relative_to(self.workspace_root).as_posix()
            return self._read_existing_file(normalized, resolved, ci, start_line, end_line)

        candidates = self._scoped_candidates(_find_path_candidates(self.workspace_root, normalized))
        # Auto-resolve ONLY when there is exactly one candidate. This is a
        # read-only operation, so guessing is cheap and reversible; with two or
        # more matches we never silently pick one (see acceptance criteria).
        if len(candidates) == 1:
            try:
                cand_target = self.sandbox.validate(candidates[0])
            except SecurityError:
                cand_target = None
            if cand_target is not None and cand_target.is_file():
                return self._read_existing_file(normalized, candidates[0], cand_target, start_line, end_line)

        message = f"Not a file: {normalized}."
        if candidates:
            message += f" Candidates: {_format_path_candidates(candidates)}"
        else:
            message += " No similar files found. Use find_file or grep_files to locate it."
        return ToolResult(False, message, {"filepath": normalized, "candidates": candidates})

    def _read_pdf(self, normalized: str) -> ToolResult:
        try:
            target = self.sandbox.validate(normalized)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"filepath": normalized, "candidates": []})
        if not target.is_file():
            candidates = _find_path_candidates(self.workspace_root, normalized)
            message = f"Not a file: {normalized}."
            if candidates:
                message += f" Candidates: {_format_path_candidates(candidates)}"
            return ToolResult(False, message, {"filepath": normalized, "candidates": candidates})
        from shamsu.prd.input import parse_prd_file

        content = parse_prd_file(target).raw_text
        if len(content) > MAX_READ_CHARS:
            content = f"{content[:MAX_READ_CHARS]}\n... [truncated {len(content) - MAX_READ_CHARS} chars]"
        return ToolResult(
            True,
            "Read file.",
            {"filepath": normalized, "resolved_filepath": normalized, "content": content, "candidates": []},
        )

    def _read_existing_file(
        self,
        asked: str,
        resolved: str,
        target: Path,
        start_line: Any,
        end_line: Any,
    ) -> ToolResult:
        if not _is_readable_text(target):
            return ToolResult(
                False,
                f"Not a supported text file: {resolved}",
                {"filepath": asked, "resolved_filepath": resolved, "candidates": []},
            )
        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult(
                False,
                f"Could not read {resolved}: {exc}",
                {"filepath": asked, "resolved_filepath": resolved, "candidates": []},
            )

        lines = text.splitlines()
        total_lines = len(lines)
        data: dict[str, Any] = {
            "filepath": asked,
            "resolved_filepath": resolved,
            "total_lines": total_lines,
            "candidates": [],
        }

        start = _coerce_optional_int(start_line)
        end = _coerce_optional_int(end_line)
        if start is not None or end is not None:
            start = max(1, start if start is not None else 1)
            end = end if end is not None else total_lines
            end = min(total_lines, max(end, start))
            selected = lines[start - 1 : end]
            content = "\n".join(selected)
            if len(content) > MAX_READ_CHARS:
                content = f"{content[:MAX_READ_CHARS]}\n... [truncated {len(content) - MAX_READ_CHARS} chars]"
            data["start_line"] = start
            data["end_line"] = end
            data["truncated"] = start > 1 or end < total_lines
        else:
            content = text
            if len(content) > MAX_READ_CHARS:
                content = f"{content[:MAX_READ_CHARS]}\n... [truncated {len(content) - MAX_READ_CHARS} chars]"
                data["truncated"] = True
                data["hint"] = (
                    "File is large; pass start_line/end_line to read a specific range. "
                    "To change it, use patch_file - rewriting a file this size with "
                    "write_file is slow and may not fit in one reply."
                )
            else:
                data["truncated"] = False
        data["content"] = content
        self._resolved_read_aliases[asked.lower()] = resolved

        if resolved != asked:
            message = f"Read file (resolved '{asked}' -> '{resolved}')."
        else:
            message = "Read file."
        return ToolResult(True, message, data)

    def file_info(self, filepath: str) -> ToolResult:
        normalized = _normalize_workspace_path(filepath)
        if not normalized:
            return ToolResult(False, "Missing filepath.", {"filepath": filepath, "candidates": []})
        normalized = self._scoped_read_path(normalized)
        if not self._inside_allowed_read_scope(normalized):
            return ToolResult(
                False,
                f"Refused to inspect {filepath}: it is outside the active project read scope.",
                {"filepath": filepath, "candidates": []},
            )
        try:
            target = self.sandbox.validate(normalized)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"filepath": normalized, "candidates": []})

        if target.is_file():
            stat = target.stat()
            return ToolResult(
                True,
                f"{normalized} is a file ({stat.st_size} bytes).",
                {
                    "exists": True,
                    "kind": "file",
                    "filepath": normalized,
                    "resolved_filepath": normalized,
                    "size_bytes": stat.st_size,
                    "extension": target.suffix,
                    "candidates": [],
                },
            )
        if target.is_dir():
            return ToolResult(
                True,
                f"{normalized} is a directory.",
                {
                    "exists": True,
                    "kind": "directory",
                    "filepath": normalized,
                    "resolved_filepath": normalized,
                    "extension": "",
                    "candidates": [],
                },
            )

        ci = _path_exists_case_insensitive(self.workspace_root, normalized)
        resolved = ci.relative_to(self.workspace_root).as_posix() if ci is not None else ""
        candidates = self._scoped_candidates(_find_path_candidates(self.workspace_root, normalized))
        message = f"No file or directory at {normalized}."
        if resolved:
            message += f" A case-insensitive match exists: {resolved}."
        elif candidates:
            message += f" Candidates: {_format_path_candidates(candidates)}"
        return ToolResult(
            True,
            message,
            {
                "exists": False,
                "kind": "missing",
                "filepath": normalized,
                "resolved_filepath": resolved,
                "extension": PurePosixPath(normalized).suffix,
                "candidates": candidates,
            },
        )

    def find_file(self, query: str, limit: int = 20) -> ToolResult:
        if _is_placeholder_query(query):
            return ToolResult(
                False,
                f'Missing or placeholder query "{str(query).strip()}". Pass a real file name to find_file.',
                {"query": query, "matches": []},
            )
        normalized = _normalize_workspace_path(query)
        if not normalized:
            return ToolResult(False, "Missing query.", {"query": query, "matches": []})
        matches = self._scoped_candidates(
            _find_files_by_query(self.workspace_root, normalized, limit)
        )
        message = (
            f"Found {len(matches)} file(s) matching '{normalized}'."
            if matches
            else f"No files matched '{normalized}'. Try a shorter query or grep_files."
        )
        # "candidates" is the key the chat loop's read-failure recovery reads to
        # suggest the real path; "matches" is kept for backward compatibility.
        return ToolResult(
            True,
            message,
            {"query": normalized, "candidates": matches, "matches": matches, "count": len(matches)},
        )

    def grep_files(
        self,
        query: str,
        path: str = ".",
        extensions: str = "",
        limit: int = 50,
    ) -> ToolResult:
        if _is_placeholder_query(query):
            return ToolResult(
                False,
                f'Missing or placeholder query "{str(query).strip()}". Pass a concrete symbol or text string to grep_files.',
                {"query": query, "matches": []},
            )
        normalized_path = _normalize_workspace_path(path) or "."
        if self._allowed_read_paths is not None and normalized_path == ".":
            roots = sorted(self._allowed_read_paths)
            if len(roots) == 1:
                normalized_path = roots[0]
            else:
                return ToolResult(
                    False,
                    "Pass one active project root as the grep path.",
                    {"query": query, "matches": [], "read_scope": roots},
                )
        normalized_path = self._scoped_read_path(normalized_path)
        if not self._inside_allowed_read_scope(normalized_path):
            return ToolResult(
                False,
                f"Refused to search {path}: it is outside the active project read scope.",
                {"query": query, "matches": []},
            )
        try:
            base = self.sandbox.validate(normalized_path)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"query": query, "matches": []})
        if not base.exists():
            return ToolResult(False, f"Search path not found: {path}", {"query": query, "matches": []})
        base = base if base.is_dir() else base.parent

        ext_filter = _parse_extensions(extensions)
        matches: list[dict[str, Any]] = []
        files_scanned = 0
        truncated = False
        for rel in _walk_workspace_files(base):
            full = base / rel
            if ext_filter and full.suffix.lower() not in ext_filter:
                continue
            if not _is_readable_text(full):
                continue
            files_scanned += 1
            try:
                with full.open("r", encoding="utf-8", errors="ignore") as handle:
                    for lineno, line in enumerate(handle, start=1):
                        if query in line:
                            rel_path = full.relative_to(self.workspace_root).as_posix()
                            matches.append(
                                {
                                    "file": rel_path,
                                    "filepath": rel_path,
                                    "line": lineno,
                                    "text": line.rstrip("\n")[:200],
                                }
                            )
                            if len(matches) >= limit:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break

        message = f"Found {len(matches)} match(es) for '{query}' across {files_scanned} file(s)."
        if truncated:
            message += f" Output capped at {limit} matches; narrow the query or set extensions."
        return ToolResult(
            True,
            message,
            {"query": query, "matches": matches, "count": len(matches), "truncated": truncated},
        )

    def edit_file(
        self,
        filepath: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> ToolResult:
        if self._read_only:
            return self._read_only_refusal("edit", filepath or "the file")
        filepath = self._resolved_read_path(self._scoped_write_path(filepath))
        scoped = self._outside_allowed_scope(filepath)
        if scoped is not None:
            return scoped
        if self._dry_run is not None:
            return self._planned(
                "edit",
                _normalize_workspace_path(filepath) or filepath,
                f"replace {old_string[:40]!r}" if old_string else "in-place edit",
            )
        normalized = _normalize_workspace_path(filepath)
        if not normalized:
            return ToolResult(False, "Missing filepath.", {"filepath": filepath, "candidates": []})
        if old_string == "":
            return ToolResult(
                False,
                "Missing old_string. Provide the exact text to replace (use write_file to create a new file).",
                {"filepath": normalized, "candidates": []},
            )
        try:
            target = self.sandbox.validate(normalized)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"filepath": normalized, "candidates": []})

        # Edits never auto-resolve to a different file: show candidates, fail safe.
        if not target.is_file():
            candidates = self._scoped_candidates(
                _find_path_candidates(self.workspace_root, normalized)
            )
            message = f"Cannot edit: file does not exist: {normalized}."
            if candidates:
                message += (
                    f" Candidates: {_format_path_candidates(candidates)}. "
                    "Confirm the real path, then call edit_file on it."
                )
            else:
                message += " Use find_file to locate it, or write_file to create it."
            return ToolResult(False, message, {"filepath": normalized, "candidates": candidates})

        try:
            content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(False, f"Could not read {normalized}: {exc}", {"filepath": normalized})

        count = content.count(old_string)
        unescaped_escapes = False
        if count == 0:
            # The model wrote a LITERAL backslash-n where a newline belongs.
            # That text is in no file, so it can never match: live 2026-08-19
            # this was 24 patch attempts and 0 successes in one session, then 29
            # more in the next, with the same payload sent nine times. The
            # harness's error was accurate and useless because it compared the
            # mangled string as given.
            #
            # Tried before the whitespace fuzz because it is the more specific
            # failure, and only ever after an exact match has already missed -
            # nothing that already worked changes path. `new_string` is
            # unescaped with it or the replacement would put the literal
            # backslash INTO the file, trading one corruption for another.
            decoded = _decode_literal_escapes(old_string)
            if decoded != old_string and content.count(decoded) > 0:
                old_string = decoded
                new_string = _decode_literal_escapes(new_string)
                count = content.count(old_string)
                unescaped_escapes = True
        if count == 0:
            # Exact match missed - retry tolerating trailing-whitespace / line-
            # ending drift (the most common reason a local model's edit block
            # doesn't match byte-for-byte) before giving up. On a unique fuzzy
            # hit, adopt the file's own text as old_string so the replacement
            # keeps real whitespace; ambiguous/no match still fails safe below.
            fuzzy = _fuzzy_match_block(content, old_string)
            if fuzzy is not None and fuzzy != new_string:
                old_string = fuzzy
                count = content.count(old_string)
        if count == 0:
            hint = _nearby_edit_hint(content, old_string)
            if _mentions_literal_escapes(old_string):
                # Unescaping was tried above and still did not match, so the
                # text is wrong for a second reason too. Name the format
                # mistake anyway - the model cannot see that its own newlines
                # went out as two characters, and it repeated the same payload
                # nine times without ever being told.
                hint = (
                    "Your old_string contains a literal backslash-n where a newline "
                    "belongs. Send real line breaks, not the two characters. " + hint
                )
            return ToolResult(
                False,
                f"old_string not found in {normalized}. The file was NOT changed. {hint}",
                {
                    "filepath": normalized,
                    "matches": 0,
                    "current_excerpt": _edit_recovery_excerpt(content, old_string),
                },
            )
        auto_disambiguated = False
        if count > 1 and not replace_all:
            candidate_contexts = _edit_context_candidates(content, old_string)
            selected = _select_edit_context(
                candidate_contexts, self._user_request, old_string
            )
            if selected is not None:
                contextual_old = str(selected.get("text", ""))
                contextual_new = contextual_old.replace(old_string, new_string, 1)
                if contextual_old and content.count(contextual_old) == 1:
                    old_string = contextual_old
                    new_string = contextual_new
                    count = 1
                    auto_disambiguated = True
        if count > 1 and not replace_all:
            return ToolResult(
                False,
                f"old_string appears {count} times in {normalized}. Add more surrounding context to "
                "make it unique, or set replace_all=true. The file was NOT changed.",
                {
                    "filepath": normalized,
                    "matches": count,
                    "candidate_contexts": candidate_contexts,
                },
            )
        if old_string == new_string:
            return ToolResult(
                False,
                "old_string and new_string are identical; nothing to change.",
                {"filepath": normalized, "matches": count},
            )

        if not replace_all:
            new_string = _indent_multiline_replacement(content, old_string, new_string)
        if replace_all:
            new_content = content.replace(old_string, new_string)
            replacements = count
        else:
            new_content = content.replace(old_string, new_string, 1)
            replacements = 1

        request = ApprovalRequest(
            action_type="file_edit",
            description=f"Edit file: {normalized} ({replacements} replacement(s))",
            risk_level="medium",
            preview=_edit_preview(normalized, old_string, new_string),
            working_dir=str(self.workspace_root),
            reason="The agent requested a targeted file edit.",
            target_paths=[normalized],
        )
        broken = _breaks_working_python(target, new_content)
        if broken:
            return ToolResult(
                False,
                f"Refusing to edit {normalized}: {broken}, but the file on disk currently does. "
                "The replacement text is truncated or malformed - the working file has been kept.",
                {"filepath": normalized, "syntax_regression": True},
            )
        if not self.approval_manager.ask(request):
            return ToolResult(False, "File edit denied by user.", {"filepath": normalized})

        transaction_id = self.transactions.begin(
            reason=f"Agent edit_file: {normalized}",
            operations=[{"op": "edit_file", "path": normalized, "dest_path": "", "reason": ""}],
            destructive=False,
        )
        if self.action_ledger:
            self.action_ledger.log_mutation_started(
                transaction_id,
                f"Agent edit_file: {normalized}",
            )
        self.transactions.backup_file(transaction_id, normalized)
        target.write_text(new_content, encoding="utf-8")
        self.transactions.record_after(transaction_id, normalized)
        diff_text = "".join(
            difflib.unified_diff(
                content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{normalized}",
                tofile=f"b/{normalized}",
            )
        )
        self.transactions.save_patch(transaction_id, diff_text)
        manifest = self.transactions.finalize(transaction_id, "applied")
        if self.action_ledger:
            self.action_ledger.log_mutation_finished(
                transaction_id,
                "applied",
                manifest.get("touched_files", []),
                rollback_available=True,
                operations=manifest.get("operations", []),
                before_hashes=manifest.get("before_hashes", {}),
                after_hashes=manifest.get("after_hashes", {}),
                backups=manifest.get("backups", {}),
                patch_path=f".shamsu/mutations/{transaction_id}/patch.diff",
                verification=manifest.get("verification"),
                abstract_index_state="stale",
            )
        _mark_code_memory_stale(self.workspace_root)
        added, removed = _line_change_counts(old_string, new_string)
        first_index = content.find(old_string)
        start_line = content.count("\n", 0, first_index) + 1
        end_line = start_line + old_string.count("\n")
        message = (
            f"Edited {normalized}: +{added} -{removed} lines "
            f"(lines {start_line}-{end_line}, {replacements} replacement(s))."
        )
        if unescaped_escapes:
            # Say it even on success. A salvage the model never hears about is
            # one it makes again every turn, and the next one may not be
            # salvageable.
            message += (
                " Note: your old_string had a literal backslash-n where a newline "
                "belongs; it was decoded to match. Send real line breaks next time."
            )
        return ToolResult(
            True,
            message,
            {
                "filepath": normalized,
                "resolved_filepath": normalized,
                "replacements": replacements,
                "bytes_written": len(new_content.encode("utf-8")),
                "transaction_id": transaction_id,
                "lines_added": added,
                "lines_removed": removed,
                "start_line": start_line,
                "end_line": end_line,
                "auto_disambiguated": auto_disambiguated,
                "unescaped_literal_newlines": unescaped_escapes,
            },
        )

    def append_file(self, filepath: str, content: str) -> ToolResult:
        if self._read_only:
            return self._read_only_refusal("append to", filepath or "the file")
        filepath = self._resolved_read_path(self._scoped_write_path(filepath))
        scoped = self._outside_allowed_scope(filepath)
        if scoped is not None:
            return scoped
        normalized = _normalize_workspace_path(filepath)
        if not normalized:
            return ToolResult(False, "Missing filepath.", {"filepath": filepath, "candidates": []})
        if not content:
            return ToolResult(
                False,
                "Missing content. Pass the text to append; the file was NOT changed.",
                {"filepath": normalized, "candidates": []},
            )
        if self._dry_run is not None:
            return self._planned(
                "append",
                normalized,
                f"{len(content.splitlines())} line(s)",
                len(content.encode("utf-8")),
            )
        try:
            target = self.sandbox.validate(normalized)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"filepath": normalized, "candidates": []})
        if not target.is_file():
            candidates = self._scoped_candidates(
                _find_path_candidates(self.workspace_root, normalized)
            )
            message = f"Cannot append: file does not exist: {normalized}."
            if candidates:
                message += (
                    f" Candidates: {_format_path_candidates(candidates)}. "
                    "Confirm the real path, then call append_file on it."
                )
            else:
                message += " Use write_file to create it."
            return ToolResult(False, message, {"filepath": normalized, "candidates": candidates})
        try:
            old_content = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ToolResult(False, f"Could not read {normalized}: {exc}", {"filepath": normalized})

        separator = "\n" if old_content and not old_content.endswith(("\n", "\r")) and not content.startswith(("\n", "\r")) else ""
        appended_content = f"{separator}{content}"
        new_content = f"{old_content}{appended_content}"
        broken = _breaks_working_python(target, new_content)
        if broken:
            return ToolResult(
                False,
                f"Refusing to append to {normalized}: {broken}, but the file on disk currently "
                "does. The appended text is truncated or malformed - the working file has been "
                "kept.",
                {"filepath": normalized, "syntax_regression": True},
            )
        request = ApprovalRequest(
            action_type="file_edit",
            description=f"Append to file: {normalized}",
            risk_level="medium",
            preview=appended_content[:4000],
            working_dir=str(self.workspace_root),
            reason="The agent requested content be appended to an existing workspace file.",
            target_paths=[normalized],
        )
        if not self.approval_manager.ask(request):
            return ToolResult(False, "File append denied by user.", {"filepath": normalized})

        transaction_id = self.transactions.begin(
            reason=f"Agent append_file: {normalized}",
            operations=[{"op": "edit_file", "path": normalized, "dest_path": "", "reason": "append"}],
            destructive=False,
        )
        if self.action_ledger:
            self.action_ledger.log_mutation_started(
                transaction_id,
                f"Agent append_file: {normalized}",
            )
        self.transactions.backup_file(transaction_id, normalized)
        target.write_text(new_content, encoding="utf-8")
        self.transactions.record_after(transaction_id, normalized)
        diff_text = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"a/{normalized}",
                tofile=f"b/{normalized}",
            )
        )
        self.transactions.save_patch(transaction_id, diff_text)
        manifest = self.transactions.finalize(transaction_id, "applied")
        if self.action_ledger:
            self.action_ledger.log_mutation_finished(
                transaction_id,
                "applied",
                manifest.get("touched_files", []),
                rollback_available=True,
                operations=manifest.get("operations", []),
                before_hashes=manifest.get("before_hashes", {}),
                after_hashes=manifest.get("after_hashes", {}),
                backups=manifest.get("backups", {}),
                patch_path=f".shamsu/mutations/{transaction_id}/patch.diff",
                verification=manifest.get("verification"),
                abstract_index_state="stale",
            )
        _mark_code_memory_stale(self.workspace_root)
        added, removed = _line_change_counts(old_content, new_content)
        start_line = len(old_content.splitlines()) + 1
        return ToolResult(
            True,
            f"Appended to {normalized}: +{added} -{removed} lines (starting at line {start_line}).",
            {
                "filepath": normalized,
                "resolved_filepath": normalized,
                "bytes_written": len(appended_content.encode("utf-8")),
                "transaction_id": transaction_id,
                "lines_added": added,
                "lines_removed": removed,
                "start_line": start_line,
                "separator_added": bool(separator),
            },
        )

    def _get_web_tool(self):
        """The shared WebTool, built on first use. Local import: tools/web pulls
        in provider config this module otherwise never needs."""
        if self._web_tool is None:
            from shamsu.tools.web import WebTool

            self._web_tool = WebTool(
                approval_func=self.approval_func,
                session_logger=self.session_logger,
                approval_manager=self.approval_manager,
                workspace=self.workspace_root,
                action_ledger=self.action_ledger,
            )
        return self._web_tool

    def web_search(self, query: str) -> ToolResult:
        """Search the web mid-task (gap D1). WebTool carries its own approval
        gate, enable flag (SHAMSU_WEB_ENABLED) and provider fallback - this is
        a thin adapter to the ToolResult shape the loop budget expects."""
        query = (query or "").strip()
        if not query:
            return ToolResult(False, "web_search needs a non-empty query.", {})
        try:
            result = self._get_web_tool().search(query, reason="The agent needs external information mid-task.")
        except Exception as exc:
            return ToolResult(False, f"Web search failed: {exc}", {"query": query})
        if not result.approved:
            return ToolResult(False, result.error or "Web search denied.", {"query": query})
        if result.error:
            return ToolResult(False, f"Web search failed: {result.error}", {"query": query})
        hits = [
            {"title": hit.title, "url": hit.url, "snippet": getattr(hit, "snippet", "")}
            for hit in result.hits[:5]
        ]
        return ToolResult(
            True,
            f"Found {len(hits)} result(s) for {query!r}.",
            {"query": query, "provider": result.provider, "results": hits},
        )

    def fetch_url(self, url: str) -> ToolResult:
        """Fetch one page's readable text mid-task (approval-gated in WebTool).
        The per-tool-result token budget already caps what enters history."""
        url = (url or "").strip()
        if not url:
            return ToolResult(False, "fetch_url needs a URL.", {})
        try:
            result = self._get_web_tool().fetch(url, reason="The agent needs this page's content mid-task.")
        except Exception as exc:
            return ToolResult(False, f"Fetch failed: {exc}", {"url": url})
        if not result.approved:
            return ToolResult(False, result.error or "Fetch denied.", {"url": url})
        if result.error:
            return ToolResult(False, f"Fetch failed: {result.error}", {"url": url})
        return ToolResult(
            True,
            f"Fetched {result.final_url or url} ({result.title or 'untitled'}).",
            {
                "url": result.final_url or url,
                "title": result.title,
                "text": (result.text or result.excerpt or "")[:12000],
            },
        )

    def ingest_docs(self, source: str, name: str = "", mode: str = "auto") -> ToolResult:
        """Persist documentation as a small skill or a chunked registered document."""
        source = (source or "").strip()
        name = (name or "").strip()
        mode = (mode or "auto").strip().lower()
        if not source:
            return ToolResult(False, "ingest_docs needs a source path or URL.", {})
        if mode not in {"auto", "reference", "document"}:
            return ToolResult(
                False,
                "ingest_docs mode must be auto, reference, or document.",
                {"source": source, "mode": mode},
            )
        if self._read_only:
            return self._read_only_refusal("ingest documentation from", source)

        source_kind = "url" if is_web_reference(source) else "local"
        source_label = source
        title = ""
        source_path: Path | None = None
        if source_kind == "url":
            try:
                fetched = self._get_web_tool().fetch(
                    source,
                    reason="The user asked SHAMSU to ingest this documentation as a reusable reference.",
                )
            except Exception as exc:
                return ToolResult(False, f"Documentation fetch failed: {exc}", {"source": source})
            if not fetched.approved:
                return ToolResult(
                    False,
                    fetched.error or "Documentation fetch denied.",
                    {"source": source, "source_kind": source_kind, "approval": "denied"},
                )
            if fetched.error:
                return ToolResult(
                    False,
                    f"Documentation fetch failed: {fetched.error}",
                    {"source": source, "source_kind": source_kind},
                )
            text = fetched.text or fetched.excerpt or ""
            source_label = fetched.final_url or source
            title = fetched.title or ""
        else:
            normalized_source = _normalize_workspace_path(source)
            if not normalized_source:
                return ToolResult(False, "Invalid documentation source path.", {"source": source})
            try:
                source_path = self.sandbox.validate(normalized_source)
                if not source_path.is_file():
                    raise ReferenceIngestError(
                        f"Documentation source is not a file: {source_path.name}"
                    )
                if source_path.suffix.lower() not in DOCUMENT_SOURCE_SUFFIXES:
                    supported = ", ".join(sorted(DOCUMENT_SOURCE_SUFFIXES))
                    raise ReferenceIngestError(
                        f"Documentation ingestion supports {supported} files."
                    )
                suffix = source_path.suffix.lower()
                if suffix == ".pdf":
                    # Deferred: the PDF path prepares a page-indexed document
                    # below rather than a flat string.
                    text = ""
                elif suffix in DOCUMENT_EXTENSIONS:
                    # Other extractable documents (.docx) have no page model,
                    # so they ingest as text and take the ordinary size-based
                    # document/reference decision. `read_text` on one would
                    # raise UnicodeDecodeError - it is a zip archive.
                    text = extract_document_text(source_path)
                else:
                    text = source_path.read_text(encoding="utf-8")
            except (SecurityError, ReferenceIngestError, OSError, UnicodeDecodeError) as exc:
                return ToolResult(
                    False,
                    f"Could not ingest local documentation: {exc}",
                    {"source": normalized_source, "source_kind": source_kind},
                )
            source_label = normalized_source
            title = source_path.stem

        use_document = mode == "document" or (
            mode == "auto"
            and (
                (source_path is not None and source_path.suffix.lower() == ".pdf")
                or len(text) > MAX_REFERENCE_SOURCE_CHARS
            )
        )
        if mode == "reference" and source_path is not None and source_path.suffix.lower() == ".pdf":
            return ToolResult(
                False,
                "PDF sources require mode=document or mode=auto.",
                {"source": source_label, "source_kind": "pdf"},
            )
        if use_document:
            try:
                if source_path is not None and source_path.suffix.lower() == ".pdf":
                    prepared_document = self.document_store.prepare_pdf(
                        source_path,
                        source=source_label,
                        name=name,
                        title=title,
                    )
                else:
                    prepared_document = self.document_store.prepare_text(
                        text,
                        source=source_label,
                        source_kind=source_kind,
                        name=name,
                        title=title,
                    )
            except DocumentError as exc:
                return ToolResult(
                    False,
                    str(exc),
                    {"source": source_label, "source_kind": source_kind, "mode": "document"},
                )
            return self._persist_document(prepared_document)

        try:
            if source_path is not None:
                validate_local_reference_path(source_path)
            prepared = prepare_reference(
                text,
                source=source_label,
                source_kind=source_kind,
                name=name,
                title=title,
            )
        except ReferenceIngestError as exc:
            return ToolResult(
                False,
                str(exc),
                {"source": source_label, "source_kind": source_kind},
            )

        target_path = prepared.relative_path
        scoped = self._outside_allowed_scope(target_path)
        if scoped is not None:
            return scoped
        if self._dry_run is not None:
            return self._planned(
                "ingest documentation into",
                target_path,
                f"{prepared.display_name} from {prepared.source}",
                len(prepared.skill_content.encode("utf-8")),
            )

        existing_skill = discover_skills(self.workspace_root).skills.get(prepared.skill_name)
        if (
            existing_skill is not None
            and str(existing_skill.metadata.get("reference_content_hash") or "")
            == prepared.content_hash
            and existing_skill.source == "workspace"
        ):
            return ToolResult(
                True,
                f"Reference {prepared.display_name} is already current.",
                {
                    "skill_name": prepared.skill_name,
                    "skill_path": target_path,
                    "source": prepared.source,
                    "source_kind": prepared.source_kind,
                    "content_hash": prepared.content_hash,
                    "unchanged": True,
                    "triggers": list(prepared.triggers),
                },
            )

        target = self.sandbox.validate(target_path)
        exists = target.is_file()
        old_content = ""
        if exists:
            try:
                old_content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                return ToolResult(
                    False,
                    f"Could not read existing reference skill: {exc}",
                    {"skill_path": target_path},
                )

        request = ApprovalRequest(
            action_type="file_edit" if exists else "file_write",
            description=(
                f"{'Update' if exists else 'Create'} workspace documentation reference: "
                f"{prepared.display_name}"
            ),
            risk_level="medium",
            preview=(
                f"Source: {prepared.source}\nTarget: {target_path}\n\n"
                f"{prepared.skill_content[:3500]}"
            ),
            working_dir=str(self.workspace_root),
            reason="The user asked SHAMSU to retain documentation for future coding tasks.",
            target_paths=[target_path],
        )
        if not self.approval_manager.ask(request):
            return ToolResult(
                False,
                "Documentation ingestion write denied by user.",
                {
                    "source": prepared.source,
                    "source_kind": prepared.source_kind,
                    "skill_path": target_path,
                    "approval": "denied",
                },
            )

        transaction_id = self.transactions.begin(
            reason=f"Agent ingest_docs: {prepared.display_name}",
            operations=[
                {
                    "op": "edit_file" if exists else "create_file",
                    "path": target_path,
                    "dest_path": "",
                    "reason": "workspace documentation reference",
                }
            ],
            destructive=False,
        )
        if self.action_ledger:
            self.action_ledger.log_mutation_started(
                transaction_id,
                f"Agent ingest_docs: {prepared.display_name}",
            )
        try:
            self.transactions.backup_file(transaction_id, target_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(prepared.skill_content, encoding="utf-8")
            self.transactions.record_after(transaction_id, target_path)
            diff_text = "".join(
                difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    prepared.skill_content.splitlines(keepends=True),
                    fromfile=f"a/{target_path}" if exists else "/dev/null",
                    tofile=f"b/{target_path}",
                )
            )
            self.transactions.save_patch(transaction_id, diff_text)
            manifest = self.transactions.finalize(transaction_id, "applied")
        except Exception as exc:
            self.transactions.finalize(transaction_id, "failed", str(exc))
            return ToolResult(
                False,
                f"Documentation ingestion failed: {exc}",
                {
                    "source": prepared.source,
                    "skill_path": target_path,
                    "transaction_id": transaction_id,
                },
            )

        if self.action_ledger:
            self.action_ledger.log_mutation_finished(
                transaction_id,
                "applied",
                manifest.get("touched_files", []),
                rollback_available=True,
                operations=manifest.get("operations", []),
                before_hashes=manifest.get("before_hashes", {}),
                after_hashes=manifest.get("after_hashes", {}),
                backups=manifest.get("backups", {}),
                patch_path=f".shamsu/mutations/{transaction_id}/patch.diff" if diff_text else "",
                verification=manifest.get("verification"),
                abstract_index_state="fresh",
            )
            self.action_ledger.log_event(
                "docs_ingested",
                skill_name=prepared.skill_name,
                skill_path=target_path,
                source=prepared.source,
                source_kind=prepared.source_kind,
                content_hash=prepared.content_hash,
                source_chars=prepared.source_chars,
                triggers=list(prepared.triggers),
            )
        if self.session_logger:
            self.session_logger.log(
                "docs.ingested",
                {
                    "skill_name": prepared.skill_name,
                    "skill_path": target_path,
                    "source": prepared.source,
                    "source_kind": prepared.source_kind,
                    "content_hash": prepared.content_hash,
                    "source_chars": prepared.source_chars,
                    "triggers": list(prepared.triggers),
                    "transaction_id": transaction_id,
                },
                f"Ingested documentation reference: {prepared.display_name}",
                workflow_id="docs-ingest",
            )
        return ToolResult(
            True,
            f"Ingested {prepared.display_name} documentation as workspace skill "
            f"{prepared.skill_name}. Future tasks that name it can inject this reference.",
            {
                "skill_name": prepared.skill_name,
                "display_name": prepared.display_name,
                "skill_path": target_path,
                "source": prepared.source,
                "source_kind": prepared.source_kind,
                "content_hash": prepared.content_hash,
                "source_chars": prepared.source_chars,
                "triggers": list(prepared.triggers),
                "transaction_id": transaction_id,
                "created": not exists,
                "overwrote": exists,
            },
        )

    def _persist_document(self, prepared: PreparedDocument) -> ToolResult:
        record = prepared.record
        target_path = prepared.relative_path
        scoped = self._outside_allowed_scope(target_path)
        if scoped is not None:
            return scoped
        if self._dry_run is not None:
            return self._planned(
                "register chunked documentation in",
                target_path,
                f"{record.name} from {record.source} ({len(record.chunks)} chunks)",
                len(prepared.json_content.encode("utf-8")),
            )

        target = self.sandbox.validate(target_path)
        exists = target.is_file()
        old_content = ""
        if exists:
            try:
                old_content = target.read_text(encoding="utf-8")
                old_payload = json.loads(old_content)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                return ToolResult(
                    False,
                    f"Could not read existing registered document: {exc}",
                    {"document_path": target_path},
                )
            if str(old_payload.get("content_hash") or "") == record.content_hash:
                return ToolResult(
                    True,
                    f"Registered document {record.name} is already current.",
                    {
                        "mode": "document",
                        "document_id": record.document_id,
                        "document_name": record.name,
                        "document_path": target_path,
                        "source": record.source,
                        "source_kind": record.source_kind,
                        "content_hash": record.content_hash,
                        "chunks": len(record.chunks),
                        "unchanged": True,
                    },
                )

        preview_chunks = "\n\n".join(
            f"[{chunk.citation}]\n{chunk.text[:800]}" for chunk in record.chunks[:3]
        )
        request = ApprovalRequest(
            action_type="file_edit" if exists else "file_write",
            description=(
                f"{'Update' if exists else 'Register'} chunked document: {record.name}"
            ),
            risk_level="medium",
            preview=(
                f"Source: {record.source}\nTarget: {target_path}\n"
                f"Chunks: {len(record.chunks)}\n\n{preview_chunks[:3500]}"
            ),
            working_dir=str(self.workspace_root),
            reason="The user asked SHAMSU to retain documentation for cited retrieval.",
            target_paths=[target_path],
        )
        if not self.approval_manager.ask(request):
            return ToolResult(
                False,
                "Document registration write denied by user.",
                {
                    "mode": "document",
                    "source": record.source,
                    "document_path": target_path,
                    "approval": "denied",
                },
            )

        transaction_id = self.transactions.begin(
            reason=f"Agent ingest_docs document: {record.name}",
            operations=[
                {
                    "op": "edit_file" if exists else "create_file",
                    "path": target_path,
                    "dest_path": "",
                    "reason": "registered chunked documentation",
                }
            ],
            destructive=False,
        )
        if self.action_ledger:
            self.action_ledger.log_mutation_started(
                transaction_id,
                f"Agent ingest_docs document: {record.name}",
            )
        try:
            self.transactions.backup_file(transaction_id, target_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(prepared.json_content, encoding="utf-8")
            self.transactions.record_after(transaction_id, target_path)
            diff_text = "".join(
                difflib.unified_diff(
                    old_content.splitlines(keepends=True),
                    prepared.json_content.splitlines(keepends=True),
                    fromfile=f"a/{target_path}" if exists else "/dev/null",
                    tofile=f"b/{target_path}",
                )
            )
            self.transactions.save_patch(transaction_id, diff_text)
            manifest = self.transactions.finalize(transaction_id, "applied")
        except Exception as exc:
            self.transactions.finalize(transaction_id, "failed", str(exc))
            return ToolResult(
                False,
                f"Document registration failed: {exc}",
                {
                    "mode": "document",
                    "source": record.source,
                    "document_path": target_path,
                    "transaction_id": transaction_id,
                },
            )

        event_payload = {
            "mode": "document",
            "document_id": record.document_id,
            "document_name": record.name,
            "document_path": target_path,
            "source": record.source,
            "source_kind": record.source_kind,
            "content_hash": record.content_hash,
            "source_chars": record.source_chars,
            "chunks": len(record.chunks),
            "warnings": list(record.warnings),
            "transaction_id": transaction_id,
        }
        if self.action_ledger:
            self.action_ledger.log_mutation_finished(
                transaction_id,
                "applied",
                manifest.get("touched_files", []),
                rollback_available=True,
                operations=manifest.get("operations", []),
                before_hashes=manifest.get("before_hashes", {}),
                after_hashes=manifest.get("after_hashes", {}),
                backups=manifest.get("backups", {}),
                patch_path=f".shamsu/mutations/{transaction_id}/patch.diff" if diff_text else "",
                verification=manifest.get("verification"),
                abstract_index_state="fresh",
            )
            self.action_ledger.log_event("docs_ingested", **event_payload)
        if self.session_logger:
            self.session_logger.log(
                "docs.ingested",
                event_payload,
                f"Registered chunked documentation: {record.name}",
                workflow_id="docs-ingest",
            )
        return ToolResult(
            True,
            f"Registered {record.name} as {len(record.chunks)} cited document chunks. "
            "Use search_docs, ask_docs, or summarize_docs; named coding tasks receive "
            "relevant excerpts automatically.",
            {
                **event_payload,
                "created": not exists,
                "overwrote": exists,
            },
        )

    def search_docs(self, query: str, document: str = "", top_k: int = 5) -> ToolResult:
        query = (query or "").strip()
        document = (document or "").strip()
        if not query:
            return ToolResult(False, "search_docs needs a query.", {})
        result = self.document_store.search(
            query,
            document=document,
            top_k=top_k,
            semantic=True,
        )
        hits = [hit.to_dict() for hit in result.hits]
        if self.action_ledger:
            self.action_ledger.log_event(
                "docs_searched",
                query=query[:500],
                document=document,
                matched_documents=list(result.matched_documents),
                result_count=len(hits),
                semantic_used=result.semantic_used,
                semantic_error=result.semantic_error,
                citations=[hit["citation"] for hit in hits],
            )
        if not result.matched_documents:
            available = [record.name for record in self.document_store.load_all()]
            return ToolResult(
                False,
                "No registered documents matched the requested document.",
                {"document": document, "available_documents": available},
            )
        if not hits:
            return ToolResult(
                True,
                f"No relevant excerpts found for {query!r}.",
                {
                    "query": query,
                    "document": document,
                    "matched_documents": list(result.matched_documents),
                    "results": [],
                    "semantic_used": result.semantic_used,
                    "semantic_error": result.semantic_error,
                },
            )
        fallback = (
            " Semantic retrieval was unavailable, so keyword/paged retrieval was used."
            if result.semantic_error
            else ""
        )
        return ToolResult(
            True,
            f"Found {len(hits)} cited document excerpt(s).{fallback}",
            {
                "query": query,
                "document": document,
                "matched_documents": list(result.matched_documents),
                "results": hits,
                "semantic_used": result.semantic_used,
                "semantic_error": result.semantic_error,
            },
        )

    def ask_docs(self, question: str, document: str = "", top_k: int = 6) -> ToolResult:
        result = self.search_docs(question, document, top_k=top_k)
        if not result.ok:
            return result
        evidence = result.data.get("results", [])
        result.data["answer_instruction"] = (
            "Answer the user's question only from these excerpts. Cite the supplied citation "
            "after every factual claim. If the excerpts do not contain the answer, say so."
        )
        result.data["question"] = question
        return ToolResult(
            True,
            (
                f"Retrieved {len(evidence)} citation-backed excerpt(s) for the question. "
                "Synthesize the answer now using only this evidence."
                if evidence
                else "The registered documents did not contain evidence for this question."
            ),
            result.data,
        )

    def summarize_docs(self, document: str, max_tokens: int = 1200) -> ToolResult:
        document = (document or "").strip()
        if not document:
            return ToolResult(False, "summarize_docs needs a document name or id.", {})
        try:
            summary = self.document_store.summarize(document, max_tokens=max_tokens)
        except DocumentError as exc:
            return ToolResult(
                False,
                str(exc),
                {
                    "document": document,
                    "available_documents": [
                        record.name for record in self.document_store.load_all()
                    ],
                },
            )
        if self.action_ledger:
            self.action_ledger.log_event(
                "docs_summarized",
                document_name=summary.document_name,
                covered_chunks=summary.covered_chunks,
                total_chunks=summary.total_chunks,
                citations=list(summary.citations),
            )
        return ToolResult(
            True,
            f"Summarized {summary.document_name} with cited coverage from "
            f"{summary.covered_chunks} of {summary.total_chunks} chunks.",
            {
                "document_name": summary.document_name,
                "summary": summary.text,
                "covered_chunks": summary.covered_chunks,
                "total_chunks": summary.total_chunks,
                "citations": list(summary.citations),
                "summary_kind": "bounded-extractive-map-reduce",
            },
        )

    def move_file(self, source: str, destination: str) -> ToolResult:
        """Move/rename a file inside the workspace, transactionally.

        Without this, any refactor that relocates a file forced the model into
        `run_command` shell hacks (`mv`/`move` - allowlist-dependent and
        POSIX/Windows-divergent) or into write-new-and-leave-the-old, which
        litters dead files that then pollute search_index results and future
        context packs (gap D2).
        """
        if self._read_only:
            return self._read_only_refusal("move", source or "the file")
        source = self._scoped_write_path(source)
        destination = self._scoped_write_path(destination)
        scoped = self._outside_allowed_scope(source)
        if scoped is not None:
            return scoped
        scoped = self._outside_allowed_scope(destination)
        if scoped is not None:
            return scoped
        if self._dry_run is not None:
            return self._planned(
                "move",
                _normalize_workspace_path(source) or source,
                f"to {_normalize_workspace_path(destination) or destination}",
            )
        from_path = _normalize_workspace_path(source)
        to_path = _normalize_workspace_path(destination)
        if not from_path or not to_path:
            return ToolResult(False, "move_file needs both source and destination.", {})
        if from_path == to_path:
            return ToolResult(False, "Source and destination are the same file.", {"filepath": from_path})
        try:
            src = self.sandbox.validate(from_path)
            dest = self.sandbox.validate(to_path)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"filepath": from_path})
        if not src.is_file():
            return ToolResult(
                False,
                f"Cannot move {from_path}: it is not a file in this workspace.",
                {"filepath": from_path},
            )
        if dest.exists():
            return ToolResult(
                False,
                f"Refusing to move {from_path} onto {to_path}: the destination already exists. "
                "Pick a different destination, or edit/delete that file explicitly first.",
                {"filepath": to_path},
            )

        request = ApprovalRequest(
            action_type="file_edit",
            description=f"Move {from_path} -> {to_path}",
            risk_level="medium",
            working_dir=str(self.workspace_root),
            reason="The agent requested a workspace file move/rename.",
            target_paths=[from_path, to_path],
        )
        if not self.approval_manager.ask(request):
            return ToolResult(False, "Move denied by user.", {"filepath": from_path})

        # Back the source up before moving so /undo can restore it, exactly like
        # every other model-driven mutation.
        transaction_id = self.transactions.begin(
            reason=f"Agent move_file: {from_path} -> {to_path}",
            operations=[{"op": "move_file", "path": from_path, "dest_path": to_path, "reason": ""}],
            destructive=True,
        )
        self.transactions.backup_file(transaction_id, from_path)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        except OSError as exc:
            self.transactions.finalize(transaction_id, "failed")
            return ToolResult(False, f"Move failed: {exc}", {"filepath": from_path})
        self.transactions.record_after(transaction_id, to_path)
        self.transactions.finalize(transaction_id, "applied")
        _mark_code_memory_stale(self.workspace_root)
        return ToolResult(
            True,
            f"Moved {from_path} -> {to_path}.",
            {"filepath": to_path, "source": from_path, "transaction_id": transaction_id},
        )

    def delete_file(self, filepath: str) -> ToolResult:
        """Delete a workspace file, recoverably.

        The file goes to the transaction trash rather than being unlinked, so
        `/undo` (and `/patch rollback`) can bring it back - a model deleting the
        wrong file must never be unrecoverable.
        """
        if self._read_only:
            return self._read_only_refusal("delete", filepath or "the file")
        filepath = self._scoped_write_path(filepath)
        scoped = self._outside_allowed_scope(filepath)
        if scoped is not None:
            return scoped
        if self._dry_run is not None:
            return self._planned("delete", _normalize_workspace_path(filepath) or filepath, "")
        normalized = _normalize_workspace_path(filepath)
        if not normalized:
            return ToolResult(False, "Missing filepath.", {})
        try:
            target = self.sandbox.validate(normalized)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"filepath": normalized})
        if not target.is_file():
            return ToolResult(
                False,
                f"Cannot delete {normalized}: it is not a file in this workspace.",
                {"filepath": normalized},
            )

        request = ApprovalRequest(
            action_type="file_delete",
            description=f"Delete file: {normalized}",
            risk_level="high",
            working_dir=str(self.workspace_root),
            reason="The agent requested a workspace file deletion.",
            target_paths=[normalized],
        )
        if not self.approval_manager.ask(request):
            return ToolResult(False, "Delete denied by user.", {"filepath": normalized})

        transaction_id = self.transactions.begin(
            reason=f"Agent delete_file: {normalized}",
            operations=[{"op": "delete_file", "path": normalized, "dest_path": "", "reason": ""}],
            destructive=True,
        )
        self.transactions.backup_file(transaction_id, normalized)
        try:
            target.unlink()
        except OSError as exc:
            self.transactions.finalize(transaction_id, "failed")
            return ToolResult(False, f"Delete failed: {exc}", {"filepath": normalized})
        self.transactions.finalize(transaction_id, "applied")
        _mark_code_memory_stale(self.workspace_root)
        return ToolResult(
            True,
            f"Deleted {normalized}. Recoverable with /undo.",
            {"filepath": normalized, "transaction_id": transaction_id},
        )

    def write_file(self, filepath: str, content: str, overwrite: bool = False) -> ToolResult:
        if self._read_only:
            return self._read_only_refusal("write", filepath or "the file")
        filepath = self._resolved_read_path(self._scoped_write_path(filepath))
        scoped = self._outside_allowed_scope(filepath)
        if scoped is not None:
            return scoped
        if self._dry_run is not None:
            target = _normalize_workspace_path(filepath) or filepath
            verb = "overwrite" if (self.workspace_root / target).is_file() else "create"
            return self._planned(
                verb, target, f"{len(content.splitlines())} line(s)", len(content.encode("utf-8"))
            )
        normalized = _normalize_workspace_path(filepath)
        if not normalized:
            return ToolResult(False, "Missing filepath.", {})
        if not str(content or "").strip() and PurePosixPath(normalized).name != "__init__.py":
            return ToolResult(
                False,
                f"Refusing an empty write to {normalized}. The content argument is missing or "
                "blank, so this would erase/create a hollow source file. Provide the complete "
                "file content. Empty writes are allowed only for __init__.py package markers.",
                {"filepath": normalized, "content_missing": True},
            )
        unwrapped = _unwrap_serialized_tool_call(str(content), "write_file")
        if unwrapped is not None:
            content = unwrapped
        broken = _breaks_working_python(self.workspace_root / normalized, str(content))
        if broken:
            return ToolResult(
                False,
                f"Refusing to overwrite {normalized}: {broken}, but the file on disk currently "
                "does. This is a truncated or malformed generation - the working file has been "
                "kept. Send the COMPLETE file content.",
                {"filepath": normalized, "syntax_regression": True},
            )
        gutted = _gutting_overwrite(self.workspace_root / normalized, str(content))
        if gutted:
            return ToolResult(
                False,
                f"Refusing to overwrite {normalized}: {gutted} This looks like a truncated "
                "generation rather than the file you meant to write. Read the file, then send "
                "its COMPLETE new content - or use edit_file/append_file to change one part of "
                "it.",
                {"filepath": normalized, "gutting_overwrite": True},
            )
        try:
            target = self.sandbox.validate(normalized)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"filepath": normalized})

        exists = target.exists()
        if exists and target.is_dir():
            return ToolResult(
                False,
                f"{normalized} is a directory, not a file.",
                {"filepath": normalized, "candidates": []},
            )

        # Guard against creating a wrong-path duplicate: if the requested file
        # does not exist but the SAME relative file exists at a different root
        # (e.g. write src/App.tsx while client/src/App.tsx exists), refuse and
        # surface candidates instead of silently scattering a second copy.
        if not exists:
            strong = self._scoped_candidates(
                _strong_path_candidates(self.workspace_root, normalized)
            )
            if strong:
                return ToolResult(
                    False,
                    f"Refusing to create {normalized}: a matching file already exists at a different "
                    f"path. Candidates: {_format_path_candidates(strong)}. Edit the existing file "
                    "(edit_file/write_file) or pass the correct path.",
                    {"filepath": normalized, "candidates": strong},
                )

        if exists and not overwrite:
            return ToolResult(
                False,
                "File already exists. Set overwrite=true if overwriting is intended.",
                {"filepath": normalized, "candidates": []},
            )

        # Capture the prior content BEFORE writing so we can report +added/-removed
        # lines (a bare "Wrote X" hides how much actually changed on overwrite).
        old_content = ""
        if exists:
            try:
                old_content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                old_content = ""

        request = ApprovalRequest(
            action_type="file_edit" if exists else "file_write",
            description=f"{'Overwrite' if exists else 'Create'} file: {normalized}",
            risk_level="medium",
            preview=content[:4000],
            working_dir=str(self.workspace_root),
            reason="The agent requested a workspace file write.",
            target_paths=[normalized],
        )
        if not self.approval_manager.ask(request):
            return ToolResult(False, "File write denied by user.", {"filepath": normalized})

        # Every model-driven write goes through a transaction (backup + hash)
        # even for this simple full-overwrite path, so it can be rolled back
        # via /patch rollback like any other mutation - the model never gets
        # to overwrite a file with no safety net.
        transaction_id = self.transactions.begin(
            reason=f"Agent write_file: {normalized}",
            operations=[
                {
                    "op": "edit_file" if exists else "create_file",
                    "path": normalized,
                    "dest_path": "",
                    "reason": "",
                }
            ],
            destructive=False,
        )
        if self.action_ledger:
            self.action_ledger.log_mutation_started(
                transaction_id,
                f"Agent write_file: {normalized}",
            )
        self.transactions.backup_file(transaction_id, normalized)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.transactions.record_after(transaction_id, normalized)
        diff_text = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{normalized}" if exists else "/dev/null",
                tofile=f"b/{normalized}",
            )
        )
        self.transactions.save_patch(transaction_id, diff_text)
        manifest = self.transactions.finalize(transaction_id, "applied")
        if self.action_ledger:
            self.action_ledger.log_mutation_finished(
                transaction_id,
                "applied",
                manifest.get("touched_files", []),
                rollback_available=True,
                operations=manifest.get("operations", []),
                before_hashes=manifest.get("before_hashes", {}),
                after_hashes=manifest.get("after_hashes", {}),
                backups=manifest.get("backups", {}),
                patch_path=f".shamsu/mutations/{transaction_id}/patch.diff" if diff_text else "",
                verification=manifest.get("verification"),
                abstract_index_state="stale",
            )
        _mark_code_memory_stale(self.workspace_root)
        added, removed = _line_change_counts(old_content, content)
        line_count = len(content.splitlines())
        if exists:
            change = f"+{added} -{removed} lines"
        else:
            change = f"+{added} lines"
        return ToolResult(
            True,
            f"{'Overwrote' if exists else 'Created'} {normalized} ({change}, {line_count} total).",
            {
                "filepath": normalized,
                "resolved_filepath": normalized,
                "bytes_written": len(content.encode("utf-8")),
                "transaction_id": transaction_id,
                "created": not exists,
                "overwrote": exists,
                "lines_added": added,
                "lines_removed": removed,
                "line_count": line_count,
            },
        )

    def run_command(self, command: str, cwd: str = ".") -> ToolResult:
        if not command.strip():
            return ToolResult(False, "Missing command.", {})
        if self._read_only and command_may_write_workspace(command):
            return ToolResult(
                False,
                "Refused to run a workspace-writing command: this request said not to "
                "change files. Run the command without redirection or file-writing shell syntax.",
                {
                    "read_only": True,
                    "blocked_action": "run_command",
                    "command": command,
                    "outcome_classification": "policy_decision",
                    "actionable": False,
                },
            )
        effective_cwd = self._scoped_command_cwd(command, cwd)
        before_files = _workspace_file_snapshot(self.workspace_root)
        code, stdout, stderr = self.command_runner.run(
            command, self.sandbox.validate(effective_cwd)
        )
        after_files = _workspace_file_snapshot(self.workspace_root)
        touched_files = sorted(
            path
            for path in set(before_files) | set(after_files)
            if before_files.get(path) != after_files.get(path)
        )
        data: dict[str, Any] = {
            "exit_code": code,
            "stdout": stdout,
            "stderr": stderr,
            "cwd": effective_cwd,
        }
        if code == 124:
            data["timeout"] = True
            data["timeout_category"] = "TOOL_TIMEOUT"
            data["timeout_seconds"] = getattr(self.command_runner, "timeout_seconds", 0)
        if touched_files:
            data["touched_files"] = touched_files
            data["deleted_files"] = [path for path in touched_files if path not in after_files]
        resolution = getattr(self.command_runner, "last_command_resolution", None)
        if resolution is not None:
            data["resolved_command"] = resolution.command
            data["project_environment"] = {
                "kind": resolution.environment_kind,
                "project_root": resolution.project_root,
                "interpreter": resolution.interpreter,
                "bootstrapped": resolution.bootstraps_environment,
            }
        packet = getattr(self.command_runner, "last_diagnostic_packet", None)
        if packet is not None:
            data["outcome_classification"] = packet.classification
            data["actionable"] = packet.actionable
            data["diagnostics_path"] = getattr(self.command_runner, "last_diagnostics_path", "")
        elif code in {125, 126}:
            data["outcome_classification"] = "policy_decision"
            data["actionable"] = False
        elif code == 127:
            data["outcome_classification"] = "environment_condition"
            data["actionable"] = False

        # DiagnosticDigest already parsed this command's output into a compact
        # ErrorPacket (see CommandRunner._run_diagnostics) - surface that to
        # the model on failure instead of leaving it unread on the command
        # runner, per pipeline.md: "parse errors before giving logs to model."
        if code != 0 and self.command_runner.last_error_packet is not None:
            data["diagnostics"] = self.command_runner.last_error_packet.to_model_context()

        return ToolResult(code == 0, f"Command exited with {code}.", data)

    def _scoped_command_cwd(self, command: str, cwd: str) -> str:
        """Resolve cwd-less framework commands beside their unique scoped entry point."""
        normalized = _normalize_workspace_path(cwd) or "."
        if normalized != "." or not re.search(r"(?:^|\s)manage\.py(?:\s|$)", command):
            return normalized
        candidates: list[str] = []
        for path in walk_workspace_files(self.workspace_root):
            if path.name != "manage.py":
                continue
            try:
                relative = path.relative_to(self.workspace_root).as_posix()
            except ValueError:
                continue
            if self._inside_allowed_read_scope(relative):
                candidates.append(relative)
        if len(candidates) != 1:
            return normalized
        parent = Path(candidates[0]).parent.as_posix()
        return parent if parent != "." else "."

    def search_index(self, query: str) -> ToolResult:
        from shamsu.abstract.service import AbstractService

        if not AbstractService(self.workspace_root).ensure_ready().allowed:
            return ToolResult(False, "Codebase-Memory MCP is not ready. Run /abstract setup.", {"query": query})

        results = SearchAgent(self.workspace_root).search(query, top_k=5)
        return ToolResult(
            True,
            f"Found {len(results)} result(s).",
            {
                "query": query,
                "results": [
                    {
                        "file_path": item.file_path,
                        "line_start": item.line_start,
                        "line_end": item.line_end,
                        "content": item.content[:1200],
                        "score": item.score,
                    }
                    for item in results
                ],
            },
        )

    def ask_user(
        self,
        question: str,
        options: Any = None,
        allow_free_text: bool = True,
    ) -> ToolResult:
        """Signal that the agent needs input. This does not block: it returns a
        structured pending question the chat loop stores in session state and
        surfaces to the user, ending the turn."""
        if not question.strip():
            return ToolResult(False, "ask_user needs a non-empty question.", {})
        from shamsu.agents.clarification import build_pending_question

        normalized_options: list[dict[str, str]] = []
        if isinstance(options, list):
            normalized_options = [
                item if isinstance(item, dict) else {"label": str(item), "description": ""}
                for item in options
            ]
        pending = build_pending_question(
            question,
            normalized_options,
            allow_free_text=allow_free_text,
        )
        return ToolResult(
            True,
            question.strip(),
            {"ask_user": True, "pending_question": pending},
        )


def _git_tool_result(result: GitCommandResult) -> ToolResult:
    return ToolResult(
        result.ok,
        result.message or ("Git command completed." if result.ok else "Git command failed."),
        {
            "command": result.command,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )


def _split_csv(value: Any) -> list[str]:
    text = str(value or "")
    return [item.strip() for item in text.split(",") if item.strip()]


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(str(value or default))
    except ValueError:
        number = default
    return max(minimum, min(number, maximum))


# Placeholder "queries" a model emits when it has not decided what to search for
# yet (e.g. grep_files query="?"). Executing these scans the whole tree for
# nonsense and burns a loop round, so the tools reject them up front and the
# chat loop turns the rejection into a concrete correction.
_PLACEHOLDER_QUERY_TOKENS = frozenset(
    {
        "?", "??", "???", "...", "…", "*", "**",
        "<query>", "<text>", "<pattern>", "<symbol>", "<term>", "<file>", "<name>",
        "query", "search", "text", "pattern", "symbol", "term", "keyword",
        "todo", "tbd", "xxx", "n/a", "na", "none", "null", "placeholder",
        "your_query", "your query", "example", "filename", "file",
    }
)


def _line_change_counts(old: str, new: str) -> tuple[int, int]:
    """(added, removed) line counts between two texts, for tool result summaries
    like '+12 -3 lines'."""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            removed += i2 - i1
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "insert":
            added += j2 - j1
    return added, removed


def _is_placeholder_query(query: str) -> bool:
    """True for an empty or placeholder search term (see `_PLACEHOLDER_QUERY_TOKENS`)."""
    stripped = str(query or "").strip()
    if not stripped:
        return True
    return stripped.lower() in _PLACEHOLDER_QUERY_TOKENS


_GREP_IGNORED = {".git", ".shamsu", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", ".mypy_cache"}


def _mark_code_memory_stale(workspace_root: Path) -> None:
    """Best-effort: never let code-memory bookkeeping break a successful write."""
    try:
        from shamsu.abstract.service import AbstractService

        AbstractService(workspace_root).mark_stale()
    except Exception:
        pass


def _tool_schema(
    name: str,
    description: str,
    properties: dict[str, dict[str, Any]],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }


def _compact_value(value: Any, limit: int = COMPACT_VALUE_LIMIT) -> Any:
    if isinstance(value, str):
        return _truncate_text(value, limit)

    if isinstance(value, list):
        compacted = [_compact_value(item, max(limit // 4, 500)) for item in value[:20]]
        if len(value) > 20:
            compacted.append(f"... [truncated {len(value) - 20} item(s)]")
        return compacted

    if isinstance(value, dict):
        items = list(value.items())[:40]
        compacted = {
            str(key): _compact_value(item, budget)
            for (key, item), budget in zip(items, _share_budget(items, limit))
        }
        if len(value) > len(items):
            compacted["..."] = f"truncated {len(value) - len(items)} key(s)"
        return compacted

    return value


def _share_budget(items: list[tuple[Any, Any]], limit: int) -> list[int]:
    """Split *limit* across a dict's values by what each actually needs.

    An EQUAL split is what broke `read_file` live on 2026-08-18. Its result has
    six keys - filepath, resolved_filepath, total_lines, candidates, truncated,
    content - so `6000 // 6` gave the file content **1000 chars** while a bool
    and an empty list reserved 1000 each. A 4170-char file reached the model as
    24% of itself, under `"truncated": false` (honest: the READER did not
    truncate, the serializer did). The model correctly reported "the file read
    is being truncated in the response", re-read five times, and had no way out.

    Equal shares also degrade silently as keys are added: exposing start_line
    and end_line would have cut content to ~666 chars.

    So: anything that already fits keeps its full size, and only the oversized
    values divide what is left. One big field among small ones - the usual shape
    of a tool result - now gets nearly the whole budget.
    """
    sizes = [len(item) if isinstance(item, str) else 0 for _key, item in items]
    budgets = [0] * len(items)
    remaining = limit
    unsatisfied = list(range(len(items)))

    # Repeatedly hand out an equal share; anything needing less than its share
    # takes only what it needs and returns the rest to the pool.
    while unsatisfied:
        share = max(remaining // len(unsatisfied), MIN_FIELD_CHARS)
        settled = [i for i in unsatisfied if sizes[i] <= share]
        if not settled:
            for i in unsatisfied:
                budgets[i] = share
            break
        for i in settled:
            budgets[i] = max(sizes[i], MIN_FIELD_CHARS)
            remaining -= sizes[i]
            unsatisfied.remove(i)
        remaining = max(remaining, 0)
    return budgets


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated {len(text) - limit} chars]"


# ---------------------------------------------------------------------------
# Path resolution + file discovery helpers
#
# Small models routinely ask for a plausible-but-wrong path (e.g. `src/App.tsx`
# when the file lives at `client/src/App.tsx`). These helpers turn a "not a
# file" dead-end into an actionable set of candidates so the read tools can
# recover instead of stalling the agent loop.
# ---------------------------------------------------------------------------


# Top-level declarations across the languages SHAMSU writes. Used only to ask
# "did this file define things before, and does it still?"
_DEFINITION_RE = re.compile(
    # `export function foo()` and `export default class Bar` are the ordinary
    # shape in JS/TS; without the optional export/default prefix a whole module
    # of them counted as zero declarations and the gutting guard stood down.
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?"
    r"(?:def|class|function|interface|type|struct|impl|fn)\s+\w"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+\w+\s*=\s*(?:\(|function|async|\{)"
    r"|^\s*\{%\s*block\b",
    re.MULTILINE,
)

# Below this the shrink is not "an edit", it is a loss.
_GUTTING_SIZE_RATIO = 0.25


def _unwrap_serialized_tool_call(content: str, tool: str) -> str | None:
    """The real file content when *content* is a tool call the model serialized.

    Small models sometimes emit the call itself where the argument should go:
    `{"name": "write_file", "arguments": {"content": "...", "filepath": "..."}}`
    written verbatim into the file. Observed 2026-08-03 - a whole template was
    replaced by its own JSON envelope, and the page failed much later at render
    time with a baffling escaped-quote error.

    Returns the inner content when the envelope names *this* tool and carries a
    `content` argument, because then the model has already said exactly what it
    wanted written and refusing would throw that away. Returns None when the
    text is ordinary file content (including a legitimate .json file).
    """
    stripped = content.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        payload = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("name") != tool:
        return None
    arguments = payload.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (ValueError, TypeError):
            return None
    if not isinstance(arguments, dict):
        return None
    inner = arguments.get("content")
    return inner if isinstance(inner, str) and inner.strip() else None


def _breaks_working_python(target: Path, content: str) -> str:
    """Why this write would replace parsing Python with unparseable Python.

    The gutting guard needs the file to lose every declaration, so a generation
    that stops mid-string slips past it: `return render(request, "item` keeps
    all seven `def`s and does not parse. Observed twice on the same file. The
    verify gate's syntax stage catches it a step later, but by then the working
    file is already gone and the run is reporting "partial" - refusing the write
    keeps the good file on disk.

    Only files that parsed BEFORE are protected: repairing an already-broken
    file must stay possible.
    """
    if target.suffix.lower() != ".py":
        return ""
    try:
        if not target.is_file():
            return ""
        existing = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    try:
        compile(existing, str(target), "exec")
    except (SyntaxError, ValueError):
        return ""  # already broken - let the model fix it
    try:
        compile(content, str(target), "exec")
    except SyntaxError as exc:
        return f"the replacement does not parse ({exc.msg} at line {exc.lineno})"
    except ValueError as exc:
        return f"the replacement does not parse ({exc})"
    return ""


def _gutting_overwrite(target: Path, content: str) -> str:
    """Why this overwrite would destroy the file, or "" when it is a real edit.

    A model that loses its place mid-generation can emit a fragment - live on
    2026-08-03 a working `core/views.py` holding four view functions was
    replaced by the three bytes `}`. It is not empty, so the empty-write guard
    passed it, and nothing else looked at what was already there.

    Deliberately conservative: BOTH a drastic shrink AND the loss of every
    declaration are required, so legitimately reducing a file to one small
    function is still allowed.
    """
    try:
        if not target.is_file():
            return ""
        existing = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    existing_definitions = len(_DEFINITION_RE.findall(existing))
    if existing_definitions == 0 or len(existing) < 200:
        return ""
    if _DEFINITION_RE.search(content):
        return ""
    if len(content) >= len(existing) * _GUTTING_SIZE_RATIO:
        return ""
    return (
        f"the existing file is {len(existing)} bytes and defines "
        f"{existing_definitions} top-level name(s); the replacement is "
        f"{len(content)} bytes and defines none."
    )


def _is_readable_text(target: Path) -> bool:
    return (
        is_readable_text_file(target)
        or target.suffix.lower() in _READABLE_TEXT_EXTENSIONS
        or target.name in _READABLE_FILENAMES
    )


def _parse_extensions(value: Any) -> set[str]:
    exts: set[str] = set()
    for item in str(value or "").replace(" ", "").split(","):
        item = item.strip().lower()
        if not item:
            continue
        if not item.startswith("."):
            item = "." + item
        exts.add(item)
    return exts


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


# A backslash followed by n, r or t - but not one that is itself escaped, so
# `"\\n"` in real JavaScript source is left exactly as the file has it.
_LITERAL_ESCAPE_RE = re.compile(r"(?<!\\)\\([nrt])")
_LITERAL_ESCAPE_CHARS = {"n": "\n", "r": "\r", "t": "\t"}


def _decode_literal_escapes(text: str) -> str:
    """Turn a literal backslash-n into a newline. Whitespace escapes only.

    Small models routinely emit `\\n` as two characters where a newline belongs,
    mixed with real newlines in the same string - live 2026-08-19 that string
    matched nothing in 24 attempts, and the same payload was sent nine times.

    Deliberately narrow. `\\d`, `\\s`, `\\\\` and every other escape are left
    alone, because they are ordinary content in a regex or a Windows path and
    "helpfully" decoding them would corrupt the very edit being made. The caller
    adopts the result only when it actually matches the file, so an over-eager
    decode here cannot reach disk on its own.
    """
    return _LITERAL_ESCAPE_RE.sub(lambda m: _LITERAL_ESCAPE_CHARS[m.group(1)], text)


def _mentions_literal_escapes(text: str) -> bool:
    """Whether a failed `old_string` looks mangled in the way the log showed."""
    return bool(_LITERAL_ESCAPE_RE.search(text or ""))


def _fuzzy_match_block(content: str, old_string: str) -> str | None:
    """Return the exact substring of ``content`` that matches ``old_string``
    ignoring per-line trailing whitespace and line-ending style, but only when
    that match is unique.

    Local models routinely reproduce an edit block with the right text but
    slightly different trailing whitespace, a stray CRLF, or a reflowed blank
    line - which makes the exact ``content.count(old_string)`` check miss and
    the whole edit fail. Matching on line-by-line ``rstrip()`` recovers those
    cases while staying safe: leading indentation (tabs vs spaces, nesting
    depth) is compared exactly, so we never silently re-indent code, and we
    return the file's own bytes so the surviving context keeps its real
    whitespace. Ambiguous (>1) matches return None and fall through to the
    normal "add more context" error rather than editing the wrong place."""
    if not old_string:
        return None
    file_lines = content.split("\n")
    old_lines = old_string.split("\n")
    span = len(old_lines)
    if span == 0 or span > len(file_lines):
        return None
    target = [line.rstrip() for line in old_lines]
    matches = [
        start
        for start in range(len(file_lines) - span + 1)
        if [line.rstrip() for line in file_lines[start:start + span]] == target
    ]
    if len(matches) != 1:
        return None
    start = matches[0]
    return "\n".join(file_lines[start:start + span])


# Lines of real file shown either side of a failed edit's nearest anchor, and
# the cap on each. Fifteen lines is enough to copy an anchor out of and small
# enough that a wrong guess costs little.
EDIT_HINT_CONTEXT_LINES = 7
EDIT_HINT_LINE_CHARS = 160


def _nearby_edit_hint(content: str, old_string: str) -> str:
    """When `old_string` is not found, hand back the file's OWN text there.

    The old version returned one line - *"Nearest similar line is line 424:
    ..."* - which is accurate and useless on repeat. Live 2026-08-19 that exact
    sentence came back 29 times unchanged. It never widened and never offered
    the actual text, so the model had nothing new to reason from and computed
    the same call again from memory, which is where the error was in the first
    place.

    `read_and_patch` already carries the right rule: a half-failure returns the
    half that worked. Plain `patch_file` did not. Now it does - the real,
    numbered lines around the nearest anchor, so the next attempt can be copied
    rather than recalled.
    """
    probe_lines = [line.strip() for line in old_string.splitlines() if line.strip()]
    if not probe_lines:
        return "Read the file (optionally with start_line/end_line) to copy the exact text."
    probe = probe_lines[0]
    file_lines = content.splitlines()
    stripped = [line.strip() for line in file_lines]
    close = difflib.get_close_matches(probe, stripped, n=1, cutoff=0.6)
    if not close:
        return (
            "Nothing in the file resembles the first line of your old_string. Call "
            "read_file (optionally with start_line/end_line) and copy the exact text, "
            "including whitespace, out of the result."
        )
    anchor = stripped.index(close[0])
    start = max(0, anchor - EDIT_HINT_CONTEXT_LINES)
    end = min(len(file_lines), anchor + EDIT_HINT_CONTEXT_LINES + 1)
    width = len(str(end))
    window = chr(10).join(
        f"{number:>{width}} | {file_lines[number - 1][:EDIT_HINT_LINE_CHARS]}"
        for number in range(start + 1, end + 1)
    )
    return (
        f"The nearest line is {anchor + 1}. This is what the file ACTUALLY says "
        f"around it:" + chr(10) * 2 + window + chr(10) * 2 +
        "Copy the text you want to replace out of those lines, character for "
        "character, rather than writing it from memory."
    )


def _edit_recovery_excerpt(content: str, old_string: str, *, max_chars: int = 4000) -> str:
    """Return bounded current source near a failed edit's closest anchor."""
    if len(content) <= max_chars:
        return content
    probe_lines = [line.strip() for line in old_string.splitlines() if line.strip()]
    file_lines = content.splitlines()
    center = 0
    if probe_lines:
        stripped = [line.strip() for line in file_lines]
        close = difflib.get_close_matches(probe_lines[0], stripped, n=1, cutoff=0.45)
        if close:
            center = stripped.index(close[0])
    start = max(0, center - 30)
    excerpt = "\n".join(file_lines[start : start + 80])
    return excerpt[:max_chars]


def _indent_multiline_replacement(content: str, old_string: str, new_string: str) -> str:
    """Carry line indentation when a model replaces an in-line anchor with a block."""
    if "\n" not in new_string or "\n" in old_string:
        return new_string
    index = content.find(old_string)
    if index < 0:
        return new_string
    line_start = content.rfind("\n", 0, index) + 1
    prefix = content[line_start:index]
    if not prefix or prefix.strip():
        return new_string
    lines = new_string.split("\n")
    return "\n".join(
        [
            lines[0],
            *[
                prefix + line if line and not line.startswith((" ", "\t")) else line
                for line in lines[1:]
            ],
        ]
    )


def _workspace_file_snapshot(workspace_root: Path) -> dict[str, tuple[int, int]]:
    """Capture a cheap manifest for detecting command-generated workspace changes."""
    snapshot: dict[str, tuple[int, int]] = {}
    root = workspace_root.resolve()
    for path in walk_workspace_files(root):
        try:
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
        except (OSError, ValueError):
            continue
        snapshot[relative] = (stat.st_size, stat.st_mtime_ns)
    return snapshot


def _edit_context_candidates(
    content: str, old_string: str, *, context_lines: int = 2, limit: int = 5
) -> list[dict[str, Any]]:
    """Return bounded exact blocks that can make an ambiguous edit unique."""
    if not old_string:
        return []
    lines = content.splitlines()
    candidates: list[dict[str, Any]] = []
    offset = 0
    while len(candidates) < limit:
        index = content.find(old_string, offset)
        if index < 0:
            break
        start_line = content.count("\n", 0, index)
        span_lines = max(1, old_string.count("\n") + 1)
        block_start = max(0, start_line - context_lines)
        block_end = min(len(lines), start_line + span_lines + context_lines)
        candidates.append(
            {
                "line_start": block_start + 1,
                "line_end": block_end,
                "match_line": start_line + 1,
                "text": "\n".join(lines[block_start:block_end]),
            }
        )
        offset = index + max(len(old_string), 1)
    return candidates


_EDIT_HINT_STOPWORDS = frozenset(
    {
        "after", "before", "change", "code", "file", "fix", "from", "function",
        "make", "returns", "script", "smallest", "then", "this", "verify", "where",
        "with", "output", "into", "instead", "named", "result", "requested",
    }
)


def _select_edit_context(
    candidates: list[dict[str, Any]], user_request: str, old_string: str
) -> dict[str, Any] | None:
    """Select one ambiguous block only when request tokens identify it uniquely."""
    request_tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", user_request or "")
        if token.lower() not in _EDIT_HINT_STOPWORDS
    }
    old_tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", old_string or "")
    }
    hints = request_tokens - old_tokens
    if not hints:
        return None
    scored: list[tuple[int, dict[str, Any]]] = []
    for candidate in candidates:
        candidate_lines = str(candidate.get("text", "")).splitlines()
        relative_match = max(
            0,
            int(candidate.get("match_line", candidate.get("line_end", 1)))
            - int(candidate.get("line_start", 1)),
        )
        hint_text = "\n".join(candidate_lines[: relative_match + 1])
        text_tokens = {
            token.lower()
            for token in re.findall(
                r"[A-Za-z_][A-Za-z0-9_]{2,}", hint_text
            )
        }
        scored.append((len(hints & text_tokens), candidate))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored or scored[0][0] <= 0:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def _edit_preview(filepath: str, old_string: str, new_string: str) -> str:
    return (
        f"edit {filepath}\n"
        f"--- old\n{old_string[:1500]}\n"
        f"+++ new\n{new_string[:1500]}"
    )
