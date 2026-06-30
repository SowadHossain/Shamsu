"""
shamsu/interfaces.py

Abstract contracts. Each dev implements these in their own module.
Anyone who needs a not-yet-built dependency imports the interface and
writes a Stub* class against it (see shamsu/retriever/search.py for the
canonical example) — never block waiting for someone else's PR to merge.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from shamsu.types import (
    SearchResult, ContextPack, LLMResponse, RoutingDecision,
    ApprovalRequest, CommandRisk, ParsedPRD, TestRunResult,
)


# ─────────────────────────────────────────────────────────────────────────
# Dev A owns: indexer/, retriever/, patch/, storage/
# ─────────────────────────────────────────────────────────────────────────

class ISearchAgent(ABC):
    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> list[SearchResult]: ...

    @abstractmethod
    def symbol_lookup(self, name: str) -> list[SearchResult]: ...

    @abstractmethod
    def fts_search(self, query: str, top_k: int = 5) -> list[SearchResult]: ...


class IPatchEngine(ABC):
    @abstractmethod
    def validate_diff(self, diff_text: str) -> tuple[bool, Optional[str]]: ...

    @abstractmethod
    def apply(self, diff_text: str, workspace_root: Path) -> bool: ...

    @abstractmethod
    def rollback(self, file_path: Path) -> bool: ...


# ─────────────────────────────────────────────────────────────────────────
# Dev B owns: llm/, agents/, context/, core/
# ─────────────────────────────────────────────────────────────────────────

class IContextBuilder(ABC):
    @abstractmethod
    def pack(
        self, results: list[SearchResult], request: str,
        task_id: str, step_id: int, specialist: str,
        budget_tokens: int = 6554,
    ) -> ContextPack: ...


class ILLMManager(ABC):
    @abstractmethod
    async def route(self, prompt: str, project_summary: str) -> RoutingDecision: ...

    @abstractmethod
    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse: ...


# ─────────────────────────────────────────────────────────────────────────
# Dev C owns: cli/, safety/, prd/, tools/
# ─────────────────────────────────────────────────────────────────────────

class ISafetyManager(ABC):
    @abstractmethod
    def validate_path(self, path: str | Path) -> Path: ...

    @abstractmethod
    def classify_command(self, cmd: str) -> CommandRisk: ...

    @abstractmethod
    def redact(self, text: str) -> str: ...

    @abstractmethod
    def ask_approval(self, request: ApprovalRequest) -> bool: ...


class IPRDParser(ABC):
    @abstractmethod
    def parse(self, file_path: Path) -> ParsedPRD: ...


class ICommandRunner(ABC):
    @abstractmethod
    def run(self, command: str, cwd: Path) -> tuple[int, str, str]: ...

    @abstractmethod
    def run_tests(self, cwd: Path) -> TestRunResult: ...
