"""Small model-facing logical tools built on the existing low-level registry."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from shamsu.artifacts.code import (
    artifact_brief,
    ensure_repository_artifacts,
    retrieve_structural_context,
    search_artifacts,
)
from shamsu.runtime.phase_contracts import (
    ExecutionPhase,
    evaluate_phase_tool_policy,
    normalize_phase,
)


@dataclass(frozen=True)
class LogicalToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    phases: frozenset[ExecutionPhase]
    risk: str = "low"
    timeout_seconds: int = 30
    output_budget_tokens: int = 1200
    evidence_produced: tuple[str, ...] = ()
    reversible: bool = True
    reversibility: str = "read-only"

    def schema(self) -> dict[str, Any]:
        properties = dict(self.input_schema.get("properties") or {})
        required = list(self.input_schema.get("required") or [])
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
            "x-shamsu": {
                "logical_tool": True,
                "phases": [phase.value for phase in sorted(self.phases, key=lambda item: item.value)],
                "risk": self.risk,
                "timeout_seconds": self.timeout_seconds,
                "output_budget_tokens": self.output_budget_tokens,
                "evidence_produced": list(self.evidence_produced),
                "reversible": self.reversible,
                "reversibility": self.reversibility,
                "output_schema": self.output_schema,
            },
        }


class LogicalBackend(Protocol):
    def list_files(self, path: str) -> Any:
        ...

    def read_file(self, filepath: str, start_line: Any = None, end_line: Any = None) -> Any:
        ...

    def file_info(self, filepath: str) -> Any:
        ...

    def find_file(self, query: str, limit: int = 20) -> Any:
        ...

    def grep_files(self, query: str, path: str = ".", extensions: str = "", limit: int = 50) -> Any:
        ...

    def search_index(self, query: str) -> Any:
        ...

    def edit_file(self, filepath: str, old_string: str, new_string: str, replace_all: bool = False) -> Any:
        ...

    def append_file(self, filepath: str, content: str) -> Any:
        ...

    def write_file(self, filepath: str, content: str, overwrite: bool = True) -> Any:
        ...

    def run_command(self, command: str, cwd: str = ".") -> Any:
        ...

    def request_scope_expansion(self, filepath: str, reason: str) -> Any:
        ...

    @property
    def git_tool(self) -> Any:
        ...

    @property
    def workspace_root(self) -> Path:
        ...


ResultFactory = Callable[[bool, str, dict[str, Any]], Any]


READ_PHASES = frozenset(
    {
        ExecutionPhase.EXPLORE,
        ExecutionPhase.PLAN,
        ExecutionPhase.AUTHOR,
        ExecutionPhase.VERIFY,
        ExecutionPhase.REPAIR,
        ExecutionPhase.DEPLOY,
    }
)
PATCH_PHASES = frozenset({ExecutionPhase.AUTHOR, ExecutionPhase.REPAIR})
TEST_PHASES = frozenset({ExecutionPhase.AUTHOR, ExecutionPhase.VERIFY, ExecutionPhase.REPAIR, ExecutionPhase.DEPLOY})
CHECKPOINT_PHASES = frozenset({ExecutionPhase.VERIFY, ExecutionPhase.DEPLOY})
SCOPE_PHASES = frozenset({ExecutionPhase.AUTHOR, ExecutionPhase.REPAIR})


LOGICAL_TOOL_SPECS: dict[str, LogicalToolSpec] = {
    "project.inspect": LogicalToolSpec(
        name="project.inspect",
        description="Inspect project structure, configuration, and optional Git overview in one read-only call.",
        input_schema={
            "properties": {
                "path": {"type": "string", "description": "Workspace folder to inspect.", "default": "."},
                "include_git": {"type": "boolean", "description": "Include Git status and branch.", "default": True},
            }
        },
        output_schema={"type": "object", "properties": {"files": {"type": "object"}, "git": {"type": "object"}}},
        phases=READ_PHASES,
        timeout_seconds=20,
        output_budget_tokens=1500,
    ),
    "code.search": LogicalToolSpec(
        name="code.search",
        description="Search code by text/symbol and file name. Use before reading when the target is uncertain.",
        input_schema={
            "properties": {
                "query": {"type": "string", "description": "Symbol, text, filename, or concept to search for."},
                "path": {"type": "string", "description": "Workspace folder to search.", "default": "."},
                "extensions": {"type": "string", "description": "Optional comma-separated extensions.", "default": ""},
                "limit": {"type": "integer", "description": "Maximum results.", "default": 20},
            },
            "required": ["query"],
        },
        output_schema={"type": "object", "properties": {"grep": {"type": "object"}, "files": {"type": "object"}, "index": {"type": "object"}}},
        phases=READ_PHASES,
        timeout_seconds=30,
        output_budget_tokens=1800,
    ),
    "file.read": LogicalToolSpec(
        name="file.read",
        description="Read a workspace file, optionally by line range.",
        input_schema={
            "properties": {
                "filepath": {"type": "string", "description": "Workspace-relative file path."},
                "start_line": {"type": "integer", "description": "Optional first 1-based line."},
                "end_line": {"type": "integer", "description": "Optional last 1-based line."},
            },
            "required": ["filepath"],
        },
        output_schema={"type": "object", "properties": {"content": {"type": "string"}, "filepath": {"type": "string"}}},
        phases=READ_PHASES,
        timeout_seconds=15,
        output_budget_tokens=2200,
    ),
    "file.patch": LogicalToolSpec(
        name="file.patch",
        description="Apply a file change. Use operation=replace for exact edits, append for additions, or write for complete file content.",
        input_schema={
            "properties": {
                "operation": {"type": "string", "enum": ["replace", "append", "write"], "description": "Patch operation."},
                "filepath": {"type": "string", "description": "Workspace-relative file path."},
                "old_string": {"type": "string", "description": "Exact text to replace for operation=replace."},
                "new_string": {"type": "string", "description": "Replacement text for operation=replace."},
                "content": {"type": "string", "description": "Content for append/write."},
                "replace_all": {"type": "boolean", "description": "Replace every match for operation=replace.", "default": False},
            },
            "required": ["operation", "filepath"],
        },
        output_schema={"type": "object", "properties": {"resolved_filepath": {"type": "string"}, "touched_files": {"type": "array"}}},
        phases=PATCH_PHASES,
        risk="medium",
        timeout_seconds=30,
        output_budget_tokens=1200,
        evidence_produced=("file_changed",),
        reversible=True,
        reversibility="transaction backup",
    ),
    "scope.expand": LogicalToolSpec(
        name="scope.expand",
        description="Request explicit expansion of the current task's locked write scope before editing an unlisted file.",
        input_schema={
            "properties": {
                "filepath": {"type": "string", "description": "Workspace-relative file path to add."},
                "reason": {"type": "string", "description": "Concise reason this task requires the path."},
            },
            "required": ["filepath", "reason"],
        },
        output_schema={"type": "object", "properties": {"approved": {"type": "boolean"}, "allowed": {"type": "array"}}},
        phases=SCOPE_PHASES,
        risk="medium",
        timeout_seconds=10,
        output_budget_tokens=500,
    ),
    "test.run": LogicalToolSpec(
        name="test.run",
        description="Run a test, lint, typecheck, build, or local health-check command.",
        input_schema={
            "properties": {
                "command": {"type": "string", "description": "Verification command to run."},
                "cwd": {"type": "string", "description": "Workspace-relative working directory.", "default": "."},
            },
            "required": ["command"],
        },
        output_schema={"type": "object", "properties": {"exit_code": {"type": "integer"}, "stdout": {"type": "string"}, "stderr": {"type": "string"}}},
        phases=TEST_PHASES,
        risk="low",
        timeout_seconds=120,
        output_budget_tokens=2000,
        evidence_produced=("test_passed", "lint_passed", "typecheck_passed", "build_passed", "service_healthy"),
        reversible=True,
        reversibility="read-only command unless command output writes generated artifacts",
    ),
    "git.inspect": LogicalToolSpec(
        name="git.inspect",
        description="Inspect Git status, branch, diffs, remotes, or recent commits without mutating the repository.",
        input_schema={
            "properties": {
                "mode": {"type": "string", "enum": ["overview", "status", "diff", "staged_diff", "file_diff", "branch", "log"], "default": "overview"},
                "filepath": {"type": "string", "description": "File for mode=file_diff.", "default": ""},
                "limit": {"type": "integer", "description": "Commit limit for mode=log.", "default": 10},
            }
        },
        output_schema={"type": "object", "properties": {"git": {"type": "object"}}},
        phases=READ_PHASES,
        timeout_seconds=20,
        output_budget_tokens=1800,
    ),
    "git.checkpoint": LogicalToolSpec(
        name="git.checkpoint",
        description="Create a local Git checkpoint by staging paths and optionally committing after inspection.",
        input_schema={
            "properties": {
                "action": {"type": "string", "enum": ["stage", "commit"], "description": "Stage paths or stage+commit.", "default": "stage"},
                "paths": {"type": "string", "description": "Comma-separated paths to stage, or empty for all.", "default": ""},
                "message": {"type": "string", "description": "Commit message for action=commit.", "default": ""},
            }
        },
        output_schema={"type": "object", "properties": {"status": {"type": "object"}, "diff": {"type": "object"}, "checkpoint": {"type": "object"}}},
        phases=CHECKPOINT_PHASES,
        risk="medium",
        timeout_seconds=45,
        output_budget_tokens=1600,
        evidence_produced=("git_diff_reviewed",),
        reversible=False,
        reversibility="Git commit can be reverted, staging can be unstaged",
    ),
}


class LogicalToolLayer:
    def __init__(
        self,
        backend: LogicalBackend,
        result_factory: ResultFactory,
        *,
        phase_getter: Callable[[], ExecutionPhase],
        risk_getter: Callable[[], str],
        enabled_advanced_getter: Callable[[], set[str] | frozenset[str]] | None = None,
    ) -> None:
        self.backend = backend
        self._result = result_factory
        self._phase_getter = phase_getter
        self._risk_getter = risk_getter
        self._enabled_advanced_getter = enabled_advanced_getter or (lambda: frozenset())

    def is_logical_tool(self, name: str) -> bool:
        return name in LOGICAL_TOOL_SPECS

    def schemas(self, *, allowed_names: set[str] | None = None) -> list[dict[str, Any]]:
        phase = self._phase_getter()
        schemas: list[dict[str, Any]] = []
        for spec in LOGICAL_TOOL_SPECS.values():
            if phase not in spec.phases:
                continue
            if allowed_names is not None and spec.name not in allowed_names:
                continue
            schemas.append(spec.schema())
        return schemas

    def execute(self, name: str, arguments: dict[str, Any]) -> Any:
        spec = LOGICAL_TOOL_SPECS.get(name)
        if spec is None:
            return self._result(False, f"Unknown logical tool: {name}", {"tool": name})
        phase = self._phase_getter()
        if phase not in spec.phases:
            return self._denial(name, phase, f"{name} is not available in {phase.value}.")
        handler = getattr(self, f"_execute_{name.replace('.', '_')}")
        result = handler(arguments or {})
        return self._with_metadata(result, spec)

    def alias(self, name: str, arguments: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        if name == "list_files":
            return "project.inspect", {"path": arguments.get("path", "."), "include_git": False}
        if name == "file_info":
            return "project.inspect", {"path": arguments.get("filepath", "."), "include_git": False}
        if name == "read_file":
            return "file.read", dict(arguments)
        if name in {"find_file", "grep_files", "search_index"}:
            return "code.search", _alias_search_args(name, arguments)
        if name in {"write_file", "edit_file", "append_file"}:
            return "file.patch", _alias_patch_args(name, arguments)
        if name == "request_scope_expansion":
            return "scope.expand", dict(arguments)
        if name == "run_command":
            return "test.run", dict(arguments)
        if name.startswith("git_"):
            if name in {"git_add", "git_add_all", "git_commit"}:
                return "git.checkpoint", _alias_git_checkpoint_args(name, arguments)
            return "git.inspect", _alias_git_inspect_args(name, arguments)
        return None

    def _execute_project_inspect(self, arguments: dict[str, Any]) -> Any:
        path = str(arguments.get("path") or ".")
        include_git = _as_bool(arguments.get("include_git"), default=True)
        files = self.backend.list_files(path)
        data: dict[str, Any] = {"files": _compact_result(files)}
        ok = bool(getattr(files, "ok", False))
        try:
            artifacts = ensure_repository_artifacts(self.backend.workspace_root)
            data["code_artifacts"] = {
                "root": str(artifacts.root),
                "manifest_hash": artifacts.manifest_hash,
                "files_indexed": artifacts.files_indexed,
                "modules": artifacts.modules,
                "symbols": artifacts.symbols,
                "brief": artifact_brief(self.backend.workspace_root, files=[]),
            }
        except Exception as exc:
            data["code_artifacts"] = {"ok": False, "message": str(exc)}
        if include_git:
            status = self.backend.git_tool.status()
            branch = self.backend.git_tool.branch()
            data["git"] = {
                "status": {
                    "is_git_repo": status.is_git_repo,
                    "is_dirty": status.is_dirty,
                    "changed_files": status.changed_files,
                    "raw_output": _budget_text(status.raw_output, 1200),
                    "error": _budget_text(status.error, 1200),
                },
                "branch": _git_result_data(branch),
            }
        return self._result(ok, "Inspected project.", data)

    def _execute_code_search(self, arguments: dict[str, Any]) -> Any:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return self._result(False, "code.search needs a query.", {"query": query})
        path = str(arguments.get("path") or ".")
        extensions = str(arguments.get("extensions") or "")
        limit = _as_int(arguments.get("limit"), default=20, minimum=1, maximum=100)
        grep = self.backend.grep_files(query, path, extensions, limit)
        files = self.backend.find_file(query, limit)
        data = {
            "query": query,
            "structural": retrieve_structural_context(self.backend.workspace_root, query, limit=limit),
            "artifacts": search_artifacts(self.backend.workspace_root, query, limit=limit),
            "grep": _compact_result(grep),
            "files": _compact_result(files),
        }
        try:
            index = self.backend.search_index(query)
            data["index"] = _compact_result(index)
        except Exception as exc:
            data["index"] = {"ok": False, "message": str(exc)}
        ok = bool(getattr(grep, "ok", False) or getattr(files, "ok", False) or data["index"].get("ok"))
        return self._result(ok, "Searched code." if ok else "No code search results.", data)

    def _execute_file_read(self, arguments: dict[str, Any]) -> Any:
        return self.backend.read_file(
            str(arguments.get("filepath") or ""),
            start_line=arguments.get("start_line"),
            end_line=arguments.get("end_line"),
        )

    def _execute_file_patch(self, arguments: dict[str, Any]) -> Any:
        operation = str(arguments.get("operation") or "").strip().lower()
        filepath = str(arguments.get("filepath") or "")
        if operation == "replace":
            result = self.backend.edit_file(
                filepath,
                str(arguments.get("old_string") or ""),
                str(arguments.get("new_string") or ""),
                replace_all=_as_bool(arguments.get("replace_all")),
            )
        elif operation == "append":
            result = self.backend.append_file(filepath, str(arguments.get("content") or ""))
        elif operation == "write":
            result = self.backend.write_file(
                filepath,
                str(arguments.get("content") or ""),
                overwrite=True,
            )
        else:
            return self._result(
                False,
                "file.patch operation must be replace, append, or write.",
                {"operation": operation, "filepath": filepath},
            )
        if bool(getattr(result, "ok", False)):
            data = result.data if isinstance(result.data, dict) else {}
            touched = str(data.get("resolved_filepath") or data.get("filepath") or filepath)
            if touched:
                data = {**data, "touched_files": [touched], "resolved_filepath": touched}
                return self._result(True, result.message, data)
        return result

    def _execute_scope_expand(self, arguments: dict[str, Any]) -> Any:
        return self.backend.request_scope_expansion(
            str(arguments.get("filepath") or ""),
            str(arguments.get("reason") or ""),
        )

    def _execute_test_run(self, arguments: dict[str, Any]) -> Any:
        command = str(arguments.get("command") or "")
        denial = evaluate_phase_tool_policy(
            "run_command",
            {"command": command},
            phase=self._phase_getter(),
            task_risk=self._risk_getter(),
            enabled_advanced_capabilities=self._enabled_advanced_getter(),
        )
        if not denial.allowed:
            return self._result(False, f"test.run denied: {denial.reason}", denial.denial_payload())
        return self.backend.run_command(command, str(arguments.get("cwd") or "."))

    def _execute_git_inspect(self, arguments: dict[str, Any]) -> Any:
        mode = str(arguments.get("mode") or "overview").strip().lower()
        git = self.backend.git_tool
        if mode == "overview":
            status = git.status()
            branch = git.branch()
            diff = git.diff_result()
            data = {
                "status": {
                    "is_git_repo": status.is_git_repo,
                    "is_dirty": status.is_dirty,
                    "changed_files": status.changed_files,
                    "raw_output": _budget_text(status.raw_output, 1200),
                    "error": _budget_text(status.error, 1200),
                },
                "branch": _git_result_data(branch),
                "diff": _git_result_data(diff),
            }
            return self._result(status.is_git_repo, "Inspected Git overview.", {"git": data})
        if mode == "status":
            status = git.status()
            return self._result(
                status.is_git_repo,
                "Read Git status." if status.is_git_repo else "Workspace is not a Git repository.",
                {"git": {"status": {
                    "is_git_repo": status.is_git_repo,
                    "is_dirty": status.is_dirty,
                    "changed_files": status.changed_files,
                    "raw_output": _budget_text(status.raw_output, 1200),
                    "error": _budget_text(status.error, 1200),
                }}},
            )
        if mode == "diff":
            return self._result_from_git("Read Git diff.", git.diff_result())
        if mode == "staged_diff":
            return self._result_from_git("Read staged Git diff.", git.diff_staged())
        if mode == "file_diff":
            return self._result_from_git(
                "Read file Git diff.",
                git.diff_file(str(arguments.get("filepath") or "")),
            )
        if mode == "branch":
            return self._result_from_git("Read Git branch.", git.branch())
        if mode == "log":
            return self._result_from_git(
                "Read Git log.",
                git.log(_as_int(arguments.get("limit"), default=10, minimum=1, maximum=100)),
            )
        return self._result(False, f"Unknown git.inspect mode: {mode}", {"mode": mode})

    def _execute_git_checkpoint(self, arguments: dict[str, Any]) -> Any:
        phase = self._phase_getter()
        if phase not in CHECKPOINT_PHASES:
            return self._denial("git.checkpoint", phase, "git.checkpoint is available only after verification.")
        action = str(arguments.get("action") or "stage").strip().lower()
        paths = _split_csv(arguments.get("paths"))
        git = self.backend.git_tool
        status_before = git.status()
        diff_before = git.diff_result()
        if action == "stage":
            checkpoint = git.add(paths) if paths else git.add_all()
        elif action == "commit":
            message = str(arguments.get("message") or "").strip()
            if not message:
                return self._result(False, "git.checkpoint commit needs a message.", {"action": action})
            staged = git.add(paths) if paths else git.add_all()
            if not staged.ok:
                return self._result(False, staged.message, {"stage": _git_result_data(staged)})
            checkpoint = git.commit(message)
        else:
            return self._result(False, "git.checkpoint action must be stage or commit.", {"action": action})
        return self._result(
            checkpoint.ok,
            checkpoint.message or ("Git checkpoint completed." if checkpoint.ok else "Git checkpoint failed."),
            {
                "status": {
                    "is_git_repo": status_before.is_git_repo,
                    "is_dirty": status_before.is_dirty,
                    "changed_files": status_before.changed_files,
                    "raw_output": _budget_text(status_before.raw_output, 1200),
                    "error": _budget_text(status_before.error, 1200),
                },
                "diff": _git_result_data(diff_before),
                "checkpoint": _git_result_data(checkpoint),
            },
        )

    def _result_from_git(self, message: str, git_result: Any) -> Any:
        return self._result(git_result.ok, message if git_result.ok else git_result.message, {"git": _git_result_data(git_result)})

    def _denial(self, name: str, phase: ExecutionPhase, reason: str) -> Any:
        allowed = [
            spec.name
            for spec in LOGICAL_TOOL_SPECS.values()
            if phase in spec.phases
        ]
        return self._result(
            False,
            f"Logical tool {name} denied by phase contract: {reason}",
            {
                "requested_tool": name,
                "current_phase": phase.value,
                "allowed_tools": sorted(allowed),
                "reason": reason,
                "phase_denied": True,
            },
        )

    def _with_metadata(self, result: Any, spec: LogicalToolSpec) -> Any:
        data = getattr(result, "data", {})
        if not isinstance(data, dict):
            return result
        data = {
            **data,
            "logical_tool": {
                "name": spec.name,
                "risk": spec.risk,
                "timeout_seconds": spec.timeout_seconds,
                "output_budget_tokens": spec.output_budget_tokens,
                "evidence_produced": list(spec.evidence_produced),
                "reversible": spec.reversible,
                "reversibility": spec.reversibility,
                "output_schema": spec.output_schema,
            },
        }
        return self._result(bool(getattr(result, "ok", False)), str(getattr(result, "message", "")), data)


def logical_tool_names_for_phase(phase: ExecutionPhase) -> set[str]:
    return {name for name, spec in LOGICAL_TOOL_SPECS.items() if normalize_phase(phase) in spec.phases}


def all_logical_tool_names() -> set[str]:
    return set(LOGICAL_TOOL_SPECS)


def _compact_result(result: Any) -> dict[str, Any]:
    data = getattr(result, "data", {})
    return {
        "ok": bool(getattr(result, "ok", False)),
        "message": str(getattr(result, "message", "")),
        "data": _budget_value(data if isinstance(data, dict) else {"value": data}),
    }


def _git_result_data(result: Any) -> dict[str, Any]:
    return {
        "ok": bool(getattr(result, "ok", False)),
        "command": str(getattr(result, "command", "")),
        "exit_code": int(getattr(result, "exit_code", 1) or 0),
        "stdout": _budget_text(str(getattr(result, "stdout", "") or ""), 3000),
        "stderr": _budget_text(str(getattr(result, "stderr", "") or ""), 2000),
        "message": str(getattr(result, "message", "") or ""),
    }


def _budget_value(value: Any) -> Any:
    if isinstance(value, str):
        return _budget_text(value, 3000)
    if isinstance(value, list):
        return [_budget_value(item) for item in value[:30]]
    if isinstance(value, dict):
        return {str(key): _budget_value(item) for key, item in list(value.items())[:40]}
    return value


def _budget_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _alias_search_args(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "find_file":
        return {"query": arguments.get("query", ""), "limit": arguments.get("limit", 20)}
    if name == "grep_files":
        return dict(arguments)
    return {"query": arguments.get("query", "")}


def _alias_patch_args(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "edit_file":
        return {
            "operation": "replace",
            "filepath": arguments.get("filepath", ""),
            "old_string": arguments.get("old_string", ""),
            "new_string": arguments.get("new_string", ""),
            "replace_all": arguments.get("replace_all", False),
        }
    if name == "append_file":
        return {
            "operation": "append",
            "filepath": arguments.get("filepath", ""),
            "content": arguments.get("content", ""),
        }
    return {
        "operation": "write",
        "filepath": arguments.get("filepath", ""),
        "content": arguments.get("content", ""),
    }


def _alias_git_inspect_args(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    modes = {
        "git_status": "status",
        "git_status_full": "status",
        "git_diff": "diff",
        "git_diff_staged": "staged_diff",
        "git_diff_file": "file_diff",
        "git_branch": "branch",
        "git_branches": "branch",
        "git_remote": "overview",
        "git_log": "log",
        "git_unpushed_commits": "log",
    }
    return {"mode": modes.get(name, "overview"), **dict(arguments)}


def _alias_git_checkpoint_args(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "git_commit":
        return {
            "action": "commit",
            "paths": "",
            "message": arguments.get("message", ""),
        }
    if name == "git_add_all":
        return {"action": "stage", "paths": ""}
    return {"action": "stage", "paths": arguments.get("paths", "")}


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
