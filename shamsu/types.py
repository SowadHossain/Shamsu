"""Shared contracts for the small SHAMSU harness."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


@dataclass
class SearchResult:
    file_path: str
    language: str
    line_start: int
    line_end: int
    content: str
    score: float
    symbol_name: str | None = None
    chunk_type: Literal["function", "class", "import_block", "window", "html_block"] = "window"


@dataclass
class ContextPack:
    task_id: str
    step_id: int
    specialist: str
    user_request: str
    snippets: list[SearchResult] = field(default_factory=list)
    document_context: str = ""
    error_context: str = ""
    previous_results: dict[str, str] = field(default_factory=dict)
    token_estimate: int = 0

    @property
    def prd_context(self) -> str:
        return self.document_context

    @prd_context.setter
    def prd_context(self, value: str) -> None:
        self.document_context = value

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "specialist": self.specialist,
            "user_request": self.user_request,
            "snippets": [vars(item) for item in self.snippets],
            "document_context": self.document_context,
            "error_context": self.error_context,
            "previous_results": self.previous_results,
            "token_estimate": self.token_estimate,
        }


class TaskStepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass
class TaskStep:
    id: int
    description: str
    type: str
    specialist: str = "coder"
    status: TaskStepStatus = TaskStepStatus.PENDING
    depends_on: list[int] = field(default_factory=list)
    target_file: str | None = None
    result: str | None = None
    error: str | None = None
    phase: str = "default"


@dataclass
class TaskState:
    task_id: str
    user_request: str
    steps: list[TaskStep]
    current_step: int = 0

    def next_pending(self) -> TaskStep | None:
        for step in self.steps:
            if step.status != TaskStepStatus.PENDING:
                continue
            dependencies = [item for item in self.steps if item.id in step.depends_on]
            if all(item.status == TaskStepStatus.DONE for item in dependencies):
                return step
        return None


@dataclass
class LLMResponse:
    raw: str
    parsed: Any | None = None
    format: Literal["text", "json", "diff", "code"] = "text"
    retries_used: int = 0
    error: str | None = None
    model_used: str = ""


@dataclass
class RoutingDecision:
    intent: str
    complexity: Literal["single", "multi_step"] = "single"
    steps: list[dict[str, Any]] = field(default_factory=list)
    needs_tools: list[str] = field(default_factory=list)
    target_files: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    required: list[str] = field(default_factory=list)

    def to_ollama_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.parameters,
                    "required": self.required,
                },
            },
        }


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {"ok": self.ok, "message": self.message, "data": self.data},
            ensure_ascii=True,
            default=str,
        )


class RunStatus(str, Enum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass
class ApprovalRequest:
    action_type: str
    description: str
    risk_level: Literal["safe", "medium", "high"]
    preview: str | None = None
    working_dir: str | None = None
    reason: str | None = None
    target_paths: list[str] = field(default_factory=list)


class CommandRisk(str, Enum):
    SAFE = "safe"
    MEDIUM = "medium"
    BLOCKED = "blocked"


@dataclass
class TestFailure:
    file: str
    test_name: str
    error_message: str
    line: int | None = None


@dataclass
class TestRunResult:
    passed: int
    failed: int
    failures: list[TestFailure] = field(default_factory=list)
    raw_output: str = ""
