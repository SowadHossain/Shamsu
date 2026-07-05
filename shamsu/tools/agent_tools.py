"""Tool registry exposed to the local ReAct chat loop."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

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
        return json.dumps(asdict(self), ensure_ascii=True)


class AgentToolRegistry:
    def __init__(
        self,
        workspace_root: Path,
        approval_func=ask_approval,
        session_logger: SessionLogger | None = None,
        approval_manager: ApprovalManager | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.workspace_tool = WorkspaceTool(self.workspace_root)
        self.command_runner = CommandRunner(
            self.workspace_root,
            approval_func=approval_func,
            session_logger=session_logger,
            approval_manager=approval_manager,
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
                "Create or overwrite a file inside the workspace.",
                {
                    "filepath": {"type": "string", "description": "Relative file path."},
                    "content": {"type": "string", "description": "Complete file content."},
                    "overwrite": {
                        "type": "boolean",
                        "description": "Whether overwriting an existing file is intended.",
                        "default": False,
                    },
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
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        try:
            if name == "list_files":
                return self.list_files(str(arguments.get("path") or "."))
            if name == "read_file":
                return self.read_file(str(arguments.get("filepath") or ""))
            if name == "write_file":
                return self.write_file(
                    str(arguments.get("filepath") or ""),
                    str(arguments.get("content") or ""),
                    bool(arguments.get("overwrite", False)),
                )
            if name == "run_command":
                return self.run_command(
                    str(arguments.get("command") or ""),
                    str(arguments.get("cwd") or "."),
                )
            if name == "search_index":
                return self.search_index(str(arguments.get("query") or ""))
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
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(
            True,
            f"Wrote {filepath}.",
            {"filepath": filepath, "bytes_written": len(content.encode("utf-8"))},
        )

    def run_command(self, command: str, cwd: str = ".") -> ToolResult:
        if not command.strip():
            return ToolResult(False, "Missing command.", {})
        code, stdout, stderr = self.command_runner.run(command, self.sandbox.validate(cwd))
        return ToolResult(
            code == 0,
            f"Command exited with {code}.",
            {"exit_code": code, "stdout": stdout, "stderr": stderr},
        )

    def search_index(self, query: str) -> ToolResult:
        index_path = self.workspace_root / ".shamsu" / "index.db"
        if not index_path.exists():
            return ToolResult(False, "No index found. Run /index first.", {"query": query})
        results = SearchAgent(index_path).search(query, top_k=5)
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
