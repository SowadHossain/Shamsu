"""Tool registry exposed to the local ReAct chat loop."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.retriever.search import SearchAgent
from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.session.manager import SessionLogger
from shamsu.tools.executor import CommandRunner
from shamsu.tools.workspace import WorkspaceTool
from shamsu.types import ApprovalRequest


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
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.workspace_tool = WorkspaceTool(self.workspace_root)
        self.transactions = TransactionWorkspace(self.workspace_root)
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
        self.approval_manager = approval_manager or ApprovalManager(approval_func, session_logger)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return [
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
                "Read a text file inside the workspace.",
                {"filepath": {"type": "string", "description": "Relative file path."}},
                required=["filepath"],
            ),
            _tool_schema(
                "write_file",
                "Create or update a file inside the workspace. If the file exists it is "
                "overwritten with the content you provide, so always pass the COMPLETE new "
                "file content. Use this for every file change.",
                {
                    "filepath": {"type": "string", "description": "Relative file path."},
                    "content": {"type": "string", "description": "Complete file content."},
                },
                required=["filepath", "content"],
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
                "find_file",
                "Find files whose path or name matches a query, to resolve a wrong or "
                "ambiguous path before reading. Returns matching relative paths.",
                {"query": {"type": "string", "description": "File name or path fragment to look for."}},
                required=["query"],
            ),
            _tool_schema(
                "grep_files",
                "Search file contents inside the workspace for a literal string and return "
                "matching file:line locations. Use this to locate code before editing.",
                {
                    "query": {"type": "string", "description": "Literal text to search for."},
                    "path": {
                        "type": "string",
                        "description": "Relative folder to search inside.",
                        "default": ".",
                    },
                },
                required=["query"],
            ),
            _tool_schema(
                "ask_user",
                "Ask the user a clarifying question when required input is missing and you "
                "cannot safely infer it with read-only tools. Calling this ends your turn "
                "and waits for the user's answer. Prefer find_file/grep_files/list_files "
                "first; only ask when genuinely blocked or when choosing between "
                "ambiguous/destructive options.",
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

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if name == "list_files":
                return self.list_files(str(arguments.get("path") or "."))
            if name == "read_file":
                return self.read_file(str(arguments.get("filepath") or ""))
            if name == "write_file":
                # The model-facing tool always overwrites: small models forget an
                # overwrite flag, get blocked, and then hallucinate success. The
                # internal `overwrite` param stays for callers that need it.
                return self.write_file(
                    str(arguments.get("filepath") or ""),
                    str(arguments.get("content") or ""),
                    overwrite=True,
                )
            if name == "run_command":
                return self.run_command(
                    str(arguments.get("command") or ""),
                    str(arguments.get("cwd") or "."),
                )
            if name == "search_index":
                return self.search_index(str(arguments.get("query") or ""))
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
            return ToolResult(False, str(exc), {"tool": name})

    def list_files(self, path: str = ".") -> ToolResult:
        target = self.sandbox.validate(path)
        if not target.is_dir():
            return ToolResult(False, f"Not a directory: {path}", {"path": path})
        listing = WorkspaceTool(target).list_files().render()
        return ToolResult(True, "Listed files.", {"path": path, "listing": listing})

    def read_file(self, filepath: str) -> ToolResult:
        if Path(filepath).suffix.lower() == ".pdf":
            target = self.sandbox.validate(filepath)
            if not target.is_file():
                return ToolResult(False, f"Not a file: {filepath}", {"filepath": filepath})
            from shamsu.prd.input import parse_prd_file

            content = parse_prd_file(target).raw_text
            if len(content) > 6000:
                content = f"{content[:6000]}\n... [truncated {len(content) - 6000} chars]"
            return ToolResult(True, "Read file.", {"filepath": filepath, "content": content})
        content = self.workspace_tool.read_file(filepath)
        return ToolResult(True, "Read file.", {"filepath": filepath, "content": content})

    def write_file(self, filepath: str, content: str, overwrite: bool = False) -> ToolResult:
        if not filepath.strip():
            return ToolResult(False, "Missing filepath.", {})
        try:
            target = self.sandbox.validate(filepath)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"filepath": filepath})
        exists = target.exists()
        if exists and not overwrite:
            return ToolResult(
                False,
                "File already exists. Set overwrite=true if overwriting is intended.",
                {"filepath": filepath},
            )
        request = ApprovalRequest(
            action_type="file_edit" if exists else "file_write",
            description=f"{'Overwrite' if exists else 'Create'} file: {filepath}",
            risk_level="medium",
            preview=content[:4000],
            working_dir=str(self.workspace_root),
            reason="The agent requested a workspace file write.",
        )
        if not self.approval_manager.ask(request):
            return ToolResult(False, "File write denied by user.", {"filepath": filepath})
        # Every model-driven write goes through a transaction (backup + hash)
        # even for this simple full-overwrite path, so it can be rolled back
        # via /patch rollback like any other mutation - the model never gets
        # to overwrite a file with no safety net.
        transaction_id = self.transactions.begin(
            reason=f"Agent write_file: {filepath}",
            operations=[{"op": "edit_file" if exists else "create_file", "path": filepath, "dest_path": "", "reason": ""}],
            destructive=False,
        )
        self.transactions.backup_file(transaction_id, filepath)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.transactions.record_after(transaction_id, filepath)
        self.transactions.finalize(transaction_id, "applied")
        _mark_code_memory_stale(self.workspace_root)
        return ToolResult(
            True,
            f"Wrote {filepath}.",
            {"filepath": filepath, "bytes_written": len(content.encode("utf-8")), "transaction_id": transaction_id},
        )

    def run_command(self, command: str, cwd: str = ".") -> ToolResult:
        if not command.strip():
            return ToolResult(False, "Missing command.", {})
        code, stdout, stderr = self.command_runner.run(command, self.sandbox.validate(cwd))
        data: dict[str, Any] = {"exit_code": code, "stdout": stdout, "stderr": stderr}
        # DiagnosticDigest already parsed this command's output into a compact
        # ErrorPacket (see CommandRunner._run_diagnostics) - surface that to
        # the model on failure instead of leaving it unread on the command
        # runner, per pipeline.md: "parse errors before giving logs to model."
        if code != 0 and self.command_runner.last_error_packet is not None:
            data["diagnostics"] = self.command_runner.last_error_packet.to_model_context()
        return ToolResult(code == 0, f"Command exited with {code}.", data)

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


    def find_file(self, query: str) -> ToolResult:
        if not query.strip():
            return ToolResult(False, "Missing query.", {})
        matches = self.workspace_tool.find_files(query, limit=20)
        candidates = [path.relative_to(self.workspace_root).as_posix() for path in matches]
        return ToolResult(
            True,
            f"Found {len(candidates)} matching path(s) for {query!r}.",
            {"query": query, "candidates": candidates},
        )

    def grep_files(self, query: str, path: str = ".") -> ToolResult:
        if not query.strip():
            return ToolResult(False, "Missing query.", {})
        try:
            root = self.sandbox.validate(path)
        except SecurityError as exc:
            return ToolResult(False, str(exc), {"query": query, "path": path})
        if not root.exists():
            return ToolResult(False, f"Not found: {path}", {"query": query, "path": path})
        base = root if root.is_dir() else root.parent
        matches: list[dict[str, Any]] = []
        needle = query
        for candidate in sorted(base.rglob("*")):
            if len(matches) >= 50:
                break
            if not candidate.is_file():
                continue
            rel = candidate.relative_to(self.workspace_root).as_posix()
            if any(part in _GREP_IGNORED for part in candidate.relative_to(self.workspace_root).parts):
                continue
            try:
                if candidate.stat().st_size > 1_000_000:
                    continue
                text = candidate.read_text(encoding="utf-8", errors="ignore")
            except (OSError, ValueError):
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if needle in line:
                    matches.append({"file": rel, "line": line_number, "text": line.strip()[:200]})
                    if len(matches) >= 50:
                        break
        return ToolResult(
            True,
            f"Found {len(matches)} match(es) for {query!r}.",
            {"query": query, "path": path, "matches": matches},
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


def _compact_value(value: Any, limit: int = 6000) -> Any:
    if isinstance(value, str):
        return _truncate_text(value, limit)
    if isinstance(value, list):
        compacted = [_compact_value(item, max(limit // 4, 500)) for item in value[:20]]
        if len(value) > 20:
            compacted.append(f"... [truncated {len(value) - 20} item(s)]")
        return compacted
    if isinstance(value, dict):
        items = list(value.items())[:40]
        per_item_limit = max(limit // max(len(items), 1), 500)
        compacted = {str(key): _compact_value(item, per_item_limit) for key, item in items}
        if len(value) > len(items):
            compacted["..."] = f"truncated {len(value) - len(items)} key(s)"
        return compacted
    return value


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated {len(text) - limit} chars]"
