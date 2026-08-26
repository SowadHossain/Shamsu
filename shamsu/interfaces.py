"""Abstract contracts shared by the small SHAMSU harness."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from shamsu.types import (
    ApprovalRequest,
    CommandRisk,
    ContextPack,
    LLMResponse,
    RoutingDecision,
    SearchResult,
    TestRunResult,
)


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


class IContextBuilder(ABC):
    @abstractmethod
    def pack(
        self,
        results: list[SearchResult],
        request: str,
        task_id: str,
        step_id: int,
        specialist: str,
        budget_tokens: int = 6554,
    ) -> ContextPack: ...


class ILLMManager(ABC):
    @abstractmethod
    async def route(self, prompt: str, project_summary: str) -> RoutingDecision: ...

    @abstractmethod
    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse: ...


class ISafetyManager(ABC):
    @abstractmethod
    def validate_path(self, path: str | Path) -> Path: ...

    @abstractmethod
    def classify_command(self, cmd: str) -> CommandRisk: ...

    @abstractmethod
    def redact(self, text: str) -> str: ...

    @abstractmethod
    def ask_approval(self, request: ApprovalRequest) -> bool: ...


class ICommandRunner(ABC):
    @abstractmethod
    def run(self, command: str, cwd: Path) -> tuple[int, str, str]: ...

    @abstractmethod
    def run_tests(self, cwd: Path) -> TestRunResult: ...
