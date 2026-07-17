"""Tool registry exposed to the local ReAct chat loop."""
from __future__ import annotations

import difflib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
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
from shamsu.tools.path_resolve import (
    _find_files_by_query,
    _find_path_candidates,
    _format_path_candidates,
    _normalize_workspace_path,
    _path_exists_case_insensitive,
    _strong_path_candidates,
    _walk_workspace_files,
)
from shamsu.tools.workspace import TEXT_EXTENSIONS, WorkspaceTool
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
MAX_READ_CHARS = 6000


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
                "unless replace_all=true. Use write_file only for new files or full rewrites.",
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
                "write_file",
                "Create a new file or fully rewrite an existing one. Always pass the COMPLETE new "
                "file content (it overwrites). For a small change to an existing file, prefer "
                "edit_file. If a matching file already exists at a different path this refuses and "
                "returns candidates rather than creating a duplicate.",
                {
                    "filepath": {"type": "string", "description": "Relative file path."},
                    "content": {"type": "string", "description": "Complete file content."},
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

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
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
            if name == "write_file":
                # The model-facing tool always overwrites: small models forget an
                # overwrite flag, get blocked, and then hallucinate success. The
                # internal `overwrite` param stays for callers that need it.
                return self.write_file(
                    str(arguments.get("filepath") or ""),
                    str(arguments.get("content") or ""),
                    overwrite=True,
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
            return ToolResult(False, str(exc), {"tool": name})

    def list_files(self, path: str = ".") -> ToolResult:
        target = self.sandbox.validate(path)
        if not target.is_dir():
            return ToolResult(False, f"Not a directory: {path}", {"path": path})
        listing = WorkspaceTool(target).list_files().render()
        return ToolResult(True, "Listed files.", {"path": path, "listing": listing})

    def read_file(
        self,
        filepath: str,
        start_line: Any = None,
        end_line: Any = None,
    ) -> ToolResult:
        normalized = _normalize_workspace_path(filepath)
        if not normalized:
            return ToolResult(False, "Missing filepath.", {"filepath": filepath, "candidates": []})

        if PurePosixPath(normalized).suffix.lower() == ".pdf":
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

        candidates = _find_path_candidates(self.workspace_root, normalized)
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
                data["hint"] = "File is large; pass start_line/end_line to read a specific range."
            else:
                data["truncated"] = False
        data["content"] = content

        if resolved != asked:
            message = f"Read file (resolved '{asked}' -> '{resolved}')."
        else:
            message = "Read file."
        return ToolResult(True, message, data)

    def file_info(self, filepath: str) -> ToolResult:
        normalized = _normalize_workspace_path(filepath)
        if not normalized:
            return ToolResult(False, "Missing filepath.", {"filepath": filepath, "candidates": []})
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
        candidates = _find_path_candidates(self.workspace_root, normalized)
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
        matches = _find_files_by_query(self.workspace_root, normalized, limit)
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
        try:
            base = self.sandbox.validate(_normalize_workspace_path(path) or ".")
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
            candidates = _find_path_candidates(self.workspace_root, normalized)
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
            return ToolResult(
                False,
                f"old_string not found in {normalized}. The file was NOT changed. {hint}",
                {"filepath": normalized, "matches": 0},
            )
        if count > 1 and not replace_all:
            return ToolResult(
                False,
                f"old_string appears {count} times in {normalized}. Add more surrounding context to "
                "make it unique, or set replace_all=true. The file was NOT changed.",
                {"filepath": normalized, "matches": count},
            )
        if old_string == new_string:
            return ToolResult(
                False,
                "old_string and new_string are identical; nothing to change.",
                {"filepath": normalized, "matches": count},
            )

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
        )
        if not self.approval_manager.ask(request):
            return ToolResult(False, "File edit denied by user.", {"filepath": normalized})

        transaction_id = self.transactions.begin(
            reason=f"Agent edit_file: {normalized}",
            operations=[{"op": "edit_file", "path": normalized, "dest_path": "", "reason": ""}],
            destructive=False,
        )
        self.transactions.backup_file(transaction_id, normalized)
        target.write_text(new_content, encoding="utf-8")
        self.transactions.record_after(transaction_id, normalized)
        self.transactions.finalize(transaction_id, "applied")
        _mark_code_memory_stale(self.workspace_root)
        added, removed = _line_change_counts(old_string, new_string)
        first_index = content.find(old_string)
        start_line = content.count("\n", 0, first_index) + 1
        end_line = start_line + old_string.count("\n")
        return ToolResult(
            True,
            f"Edited {normalized}: +{added} -{removed} lines "
            f"(lines {start_line}-{end_line}, {replacements} replacement(s)).",
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
        normalized = _normalize_workspace_path(filepath)
        if not normalized:
            return ToolResult(False, "Missing filepath.", {})
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
            strong = _strong_path_candidates(self.workspace_root, normalized)
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
        self.transactions.backup_file(transaction_id, normalized)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self.transactions.record_after(transaction_id, normalized)
        self.transactions.finalize(transaction_id, "applied")
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
        compacted = {
            str(key): _compact_value(item, per_item_limit)
            for key, item in items
        }
        if len(value) > len(items):
            compacted["..."] = f"truncated {len(value) - len(items)} key(s)"
        return compacted

    return value


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


def _is_readable_text(target: Path) -> bool:
    return target.suffix.lower() in _READABLE_TEXT_EXTENSIONS or target.name in _READABLE_FILENAMES


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


def _nearby_edit_hint(content: str, old_string: str) -> str:
    """When edit_file's old_string is not found, point at the closest existing
    line so the model can fix whitespace/exact-text instead of guessing again."""
    probe_lines = [line.strip() for line in old_string.splitlines() if line.strip()]
    if not probe_lines:
        return "Read the file (optionally with start_line/end_line) to copy the exact text."
    probe = probe_lines[0]
    file_lines = content.splitlines()
    stripped = [line.strip() for line in file_lines]
    close = difflib.get_close_matches(probe, stripped, n=1, cutoff=0.6)
    if not close:
        return "Read the file (optionally with start_line/end_line) to copy the exact text, including whitespace."
    for idx, line in enumerate(file_lines, start=1):
        if line.strip() == close[0]:
            return (
                f"Nearest similar line is line {idx}: {close[0][:120]!r}. "
                "Match the exact text and whitespace (read a line range first if unsure)."
            )
    return "Read the file to copy the exact text, including whitespace."


def _edit_preview(filepath: str, old_string: str, new_string: str) -> str:
    return (
        f"edit {filepath}\n"
        f"--- old\n{old_string[:1500]}\n"
        f"+++ new\n{new_string[:1500]}"
    )
