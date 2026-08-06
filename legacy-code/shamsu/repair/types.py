"""Shared data types for the strict repair loop."""
from __future__ import annotations

from dataclasses import dataclass, field

from shamsu.repair.comparator import RepairOutcome
from shamsu.repair.kinds import RepairError


@dataclass(frozen=True)
class InspectedSnippet:
    file: str
    line_start: int
    line_end: int
    content: str


@dataclass(frozen=True)
class PreviousAttempt:
    files_changed: list[str]
    before_signature: str
    after_signature: str
    outcome: RepairOutcome
    note: str = ""


@dataclass(frozen=True)
class DebugContext:
    """Everything the model is allowed to see in strict debug mode - the one
    selected error, only the files that were actually inspected, the history
    of failed attempts, and the verification command. Nothing else."""
    primary_error: RepairError
    verify_command: str
    inspected: list[InspectedSnippet] = field(default_factory=list)
    editable_files: list[str] = field(default_factory=list)
    previous_attempts: list[PreviousAttempt] = field(default_factory=list)
    import_suggestion: str = ""


@dataclass(frozen=True)
class RepairPlan:
    """The model's structured proposal for a single root cause."""
    root_cause: str
    target_file: str
    search: str = ""
    replace: str = ""
    full_content: str = ""
    inspected_files: list[str] = field(default_factory=list)

    @property
    def has_edit(self) -> bool:
        return bool(self.full_content) or bool(self.search)

    def payload(self) -> str:
        """Content fingerprint of the proposed change, for the action blocker."""
        return f"{self.target_file}\x00{self.search}\x00{self.replace}\x00{self.full_content}"


@dataclass(frozen=True)
class RepairAttemptRecord:
    index: int
    files_changed: list[str]
    before_signature: str
    after_signature: str
    outcome: RepairOutcome
    kept: bool
    note: str = ""


@dataclass(frozen=True)
class RepairResult:
    success: bool
    exit_code: int
    attempts: list[RepairAttemptRecord]
    final_message: str
    remaining_errors: list[RepairError] = field(default_factory=list)
    stopped_reason: str = ""
