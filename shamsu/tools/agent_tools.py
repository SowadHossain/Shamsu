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
from shamsu.tools.git import GitCommandResult, GitTool
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

        # Git commands run through the same CommandRunner so command safety,
        # approval, logging, timeout handling, and diagnostics still apply.
        self.git_tool = GitTool(self.workspace_root, command_runner=self.command_runner)

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
                result = self.git_tool.diff_result()
                return _git_tool_result(result)

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
            operations=[
                {
                    "op": "edit_file" if exists else "create_file",
                    "path": filepath,
                    "dest_path": "",
                    "reason": "",
                }
            ],
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
        compacted = {str(key): _compact_value(item, per_item_limit) for item in items}
        if len(value) > len(items):
            compacted["..."] = f"truncated {len(value) - len(items)} key(s)"
        return compacted
    return value


def _truncate_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n... [truncated {len(text) - limit} chars]"