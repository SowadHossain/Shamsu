"""
shamsu/types.py

THE SHARED CONTRACT. All three devs import from here. Do not redefine
these types in your own module. If you need a field that doesn't exist,
open an issue and tag the other two devs before changing this file —
a change here can silently break someone else's branch.

Frozen on Day 1. Treat changes as a team decision, not a solo edit.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Literal, Any


# ─────────────────────────────────────────────────────────────────────────
# Retrieval (Dev A owns the producers, everyone consumes)
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class SearchResult:
    file_path: str
    language: str
    line_start: int
    line_end: int
    content: str
    score: float
    symbol_name: Optional[str] = None
    chunk_type: Literal["function", "class", "import_block", "window", "html_block"] = "window"


# ─────────────────────────────────────────────────────────────────────────
# Context packs (Dev B owns the builder, the LLM layer consumes)
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ContextPack:
    task_id: str
    step_id: int
    specialist: str
    user_request: str
    snippets: list[SearchResult] = field(default_factory=list)
    prd_context: str = ""
    error_context: str = ""
    previous_results: dict[str, str] = field(default_factory=dict)
    token_estimate: int = 0

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "specialist": self.specialist,
            "user_request": self.user_request,
            "snippets": [vars(s) for s in self.snippets],
            "prd_context": self.prd_context,
            "error_context": self.error_context,
            "previous_results": self.previous_results,
            "token_estimate": self.token_estimate,
        }


# ─────────────────────────────────────────────────────────────────────────
# Task / planning (Dev C owns the planner + task store)
# ─────────────────────────────────────────────────────────────────────────

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
    type: Literal[
        "file_create", "file_edit", "run_command", "run_test",
        "generate_docs", "ask_user", "migrate",
    ]
    specialist: str = "coder"
    status: TaskStepStatus = TaskStepStatus.PENDING
    depends_on: list[int] = field(default_factory=list)
    target_file: Optional[str] = None
    result: Optional[str] = None
    error: Optional[str] = None
    phase: str = "default"


@dataclass
class TaskState:
    task_id: str
    user_request: str
    steps: list[TaskStep]
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    current_step: int = 0

    def next_pending(self) -> Optional[TaskStep]:
        for step in self.steps:
            if step.status == TaskStepStatus.PENDING:
                if all(
                    s.status == TaskStepStatus.DONE
                    for s in self.steps if s.id in step.depends_on
                ):
                    return step
        return None


# ─────────────────────────────────────────────────────────────────────────
# Project generation spec (PRD → Django project)
# ─────────────────────────────────────────────────────────────────────────

class Archetype(str, Enum):
    WEB_CRUD = "web_crud"
    REST_API = "rest_api"
    SAAS_FULLSTACK = "saas_fullstack"
    REALTIME_3D_GAME = "realtime_3d_game"
    GENERIC_WEB = "generic_web"


@dataclass
class EntityFieldSpec:
    name: str
    django_type: str          # e.g. "CharField", "ForeignKey", "DecimalField"
    kwargs: dict[str, Any] = field(default_factory=dict)   # max_length=200, etc.


@dataclass
class EntitySpec:
    name: str
    fields: list[EntityFieldSpec]
    relationships: list[str] = field(default_factory=list)   # "belongs_to:User"


@dataclass
class EndpointSpec:
    method: str                # GET | POST | PUT | DELETE
    path: str
    resource: str
    auth_required: bool = True


@dataclass
class PageSpec:
    name: str
    page_type: Literal["list", "form", "detail", "dashboard", "auth"]
    purpose: str
    resource: Optional[str] = None
    fields_shown: list[str] = field(default_factory=list)
    requires_login: bool = True


@dataclass
class DjangoFileSpec:
    path: str                  # relative path inside the generated project
    generator: str             # which generator class handles this file
    specialist: Optional[str]  # None = fixed template, no LLM
    depends_on: list[str] = field(default_factory=list)   # other file paths


@dataclass
class Hole:
    id: str
    hole_type: str
    target_file: str
    description: str
    signature: Optional[str] = None
    schema: Optional[dict[str, Any]] = None
    depends_on: list[str] = field(default_factory=list)
    specialist: str = "coder"


@dataclass
class GenerationManifest:
    holes: list[Hole] = field(default_factory=list)


@dataclass
class ProjectSpec:
    project_name: str
    app_name: str
    entities: list[EntitySpec]
    endpoints: list[EndpointSpec]
    pages: list[PageSpec]
    theme: str = "corporate"            # DaisyUI theme
    generation_order: list[DjangoFileSpec] = field(default_factory=list)
    archetype: Archetype = Archetype.WEB_CRUD
    archetype_confidence: float = 1.0
    archetype_spec: Any = None
    category: str | None = None
    master_prompt: str = ""
    manifest_path: str = ""
    dod_path: str = ""
    feature_requests: list[str] = field(default_factory=list)
    # Populated by build_project_spec: the structured PRD (source of truth) and
    # the chosen generation strategy. Typed as Any to avoid importing the prd/
    # registry dataclasses into this base types module.
    prd_contract: Any = None            # shamsu.prd.contract.PRDContract
    suitability: Any = None             # shamsu.registry.suitability.TemplateSuitability
    generation_ready: bool = True
    needs_input: bool = False
    clarification_question: str = ""
    assumptions: list[str] = field(default_factory=list)
    definition_of_done: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────
# LLM layer
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class LLMResponse:
    raw: str
    parsed: Optional[Any] = None
    format: Literal["text", "json", "diff", "code"] = "text"
    retries_used: int = 0
    error: Optional[str] = None
    model_used: str = ""


@dataclass
class RoutingDecision:
    intent: Literal[
        "code_edit", "bug_fix", "audit", "generate",
        "explain", "test_gen", "doc_gen", "qa",
    ]
    complexity: Literal["single", "multi_step"]
    steps: list[dict] = field(default_factory=list)
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
        import json

        return json.dumps(
            {"ok": self.ok, "message": self.message, "data": self.data},
            ensure_ascii=True,
            default=str,
        )


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


# ─────────────────────────────────────────────────────────────────────────
# Safety
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ApprovalRequest:
    action_type: Literal[
        "file_write", "file_edit", "file_delete",
        "run_command", "web_search", "mcp_tool",
    ]
    description: str
    risk_level: Literal["safe", "medium", "high"]
    preview: Optional[str] = None
    working_dir: Optional[str] = None
    reason: Optional[str] = None
    target_paths: list[str] = field(default_factory=list)


class CommandRisk(str, Enum):
    SAFE = "safe"
    MEDIUM = "medium"
    BLOCKED = "blocked"


# ─────────────────────────────────────────────────────────────────────────
# PRD parsing
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedPRD:
    title: str
    sections: dict[str, list[str]]    # heading -> list of bullet/line strings
    raw_text: str = ""
    source_path: str = ""
    source_kind: str = "text"
    source_refs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tables: list[dict[str, Any]] = field(default_factory=list)
    extraction_confidence: float = 1.0
    extraction_warnings: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────
# Test results
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class TestFailure:
    file: str
    test_name: str
    error_message: str
    line: Optional[int] = None


@dataclass
class TestRunResult:
    passed: int
    failed: int
    failures: list[TestFailure] = field(default_factory=list)
    raw_output: str = ""
