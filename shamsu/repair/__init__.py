"""Strict code-repair feedback loop.

Verify -> select one root error -> propose a minimal patch in strict debug
mode -> re-verify -> keep or roll back. Never claims success unless the
verifier exits 0. Reuses the diagnostics digest and patch transaction layers;
it is not a second parser or a second patch engine.
"""
from __future__ import annotations

from shamsu.repair.action_blocker import RepeatedActionBlocker, action_signature
from shamsu.repair.comparator import ErrorComparator, RepairOutcome
from shamsu.repair.import_resolver import ImportFix, suggest_import_fix
from shamsu.repair.kinds import (
    ErrorKind,
    RepairError,
    classify,
    repair_errors_from_packet,
    select_primary_error,
)
from shamsu.repair.loop import Applier, FileApplier, Proposer, RepairLoop, Verifier, VerifyRun
from shamsu.repair.plan_schema import REPAIR_PLAN_JSON_SCHEMA
from shamsu.repair.prompt import (
    build_debug_prompt,
    build_final_message,
    contains_unverified_success_claim,
    enforce_final_response,
)
from shamsu.repair.proposer_llm import LLMProposer
from shamsu.repair.types import (
    DebugContext,
    InspectedSnippet,
    PreviousAttempt,
    RepairAttemptRecord,
    RepairPlan,
    RepairResult,
)
from shamsu.repair.verifiers import CommandVerifier, DjangoTestVerifier

__all__ = [
    "Applier",
    "CommandVerifier",
    "DebugContext",
    "DjangoTestVerifier",
    "ErrorComparator",
    "ErrorKind",
    "FileApplier",
    "ImportFix",
    "InspectedSnippet",
    "LLMProposer",
    "PreviousAttempt",
    "Proposer",
    "REPAIR_PLAN_JSON_SCHEMA",
    "RepairAttemptRecord",
    "RepairError",
    "RepairLoop",
    "RepairOutcome",
    "RepairPlan",
    "RepairResult",
    "RepeatedActionBlocker",
    "Verifier",
    "VerifyRun",
    "action_signature",
    "build_debug_prompt",
    "build_final_message",
    "classify",
    "contains_unverified_success_claim",
    "enforce_final_response",
    "repair_errors_from_packet",
    "select_primary_error",
    "suggest_import_fix",
]
