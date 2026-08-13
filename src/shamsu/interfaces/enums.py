"""The shared vocabulary of the v2 runtime.

These enums are a frozen contract in the same sense v1's ``types.py`` was: they
are referenced across every package, so changing a member is a cross-cutting
change that needs a decision record under ``docs/decisions/``.

Adding a member is usually safe. Removing or renaming one is not.
"""

from __future__ import annotations

from enum import StrEnum


class Phase(StrEnum):
    """What kind of work the runtime is currently doing.

    The phase determines which tools are reachable. A tool that does not
    declare the current phase in its ``allowed_phases`` cannot be executed --
    this is checked by the gateway, not left to the model's judgement.

    See plan section 20 for the per-phase allow/block lists.
    """

    INSPECT = "inspect"
    PLAN = "plan"
    AUTHOR = "author"
    VERIFY = "verify"
    REPAIR = "repair"
    DEPLOY = "deploy"
    COMPLETE = "complete"


class AgentState(StrEnum):
    """Nodes of the runtime state machine (plan section 10).

    The runtime owns transitions between these. The model never sets one
    directly; it proposes a decision and the runtime decides what that means.
    """

    RECEIVE_TASK = "receive_task"
    LOAD_PROJECT_STATE = "load_project_state"
    INSPECT_PROJECT = "inspect_project"
    CLASSIFY_TASK = "classify_task"
    CREATE_PLAN = "create_plan"
    VALIDATE_PLAN = "validate_plan"
    APPROVAL_CHECK = "approval_check"
    EXECUTE_CURRENT_STEP = "execute_current_step"
    VERIFY_CURRENT_STEP = "verify_current_step"
    CREATE_CHECKPOINT = "create_checkpoint"
    REPAIR = "repair"
    REPLAN = "replan"
    WAIT_APPROVAL = "wait_approval"
    CHECK_REMAINING_STEPS = "check_remaining_steps"
    FINAL_VERIFICATION = "final_verification"
    COMPLETION_GATE = "completion_gate"
    FINAL_REPORT = "final_report"
    STOPPED = "stopped"
    BLOCKED = "blocked"


class TaskKind(StrEnum):
    """Classifier output: does this task need a plan at all?"""

    DIRECT = "direct"
    PLANNED = "planned"


class StepOutcome(StrEnum):
    """Result of verifying one plan step (plan section 10).

    Note that ``PASS`` means *the required evidence was produced and verified*,
    not *the model said it was done*.
    """

    PASS = "pass"
    REPAIRABLE = "repairable"
    PLAN_INVALID = "plan_invalid"
    APPROVAL_REQUIRED = "approval_required"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


class RunStatus(StrEnum):
    """Lifecycle of a registered run.

    Every live run must be observable and cancellable -- the defect that most
    directly motivated v2. v1's live loop had no mid-run cancellation path at
    all (see legacy-code/LEGACY_README.md).
    """

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class Risk(StrEnum):
    """Risk band of a tool or command.

    Drives approval requirements and phase gating, not just logging.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ArtifactStatus(StrEnum):
    """Freshness of a derived artifact (plan section 17).

    A stale artifact may still be used, but only with an explicit warning in
    the compiled frame. An invalidated one may not be used at all.
    """

    FRESH = "fresh"
    STALE = "stale"
    INVALIDATED = "invalidated"
    MISSING = "missing"
    GENERATION_FAILED = "generation_failed"


class ArtifactKind(StrEnum):
    """The artifact types required by plan section 15."""

    REPOSITORY_MANIFEST = "repository_manifest"
    REPOSITORY_MAP = "repository_map"
    MODULE_CARD = "module_card"
    SYMBOL_CARD = "symbol_card"
    DEPENDENCY_GRAPH = "dependency_graph"
    API_MAP = "api_map"
    DATABASE_SCHEMA = "database_schema"
    TEST_MAP = "test_map"
    CONFIGURATION_MAP = "configuration_map"
    TASK_PACKET = "task_packet"
    CHANGE_MANIFEST = "change_manifest"
    FAILURE_CAPSULE = "failure_capsule"


class EvidenceKind(StrEnum):
    """Kinds of verified evidence a claim can rest on (plan section 25).

    The completion rule is ``required_evidence <= verified_evidence``. Evidence
    is registered by the runtime after a tool actually produced it -- never by
    the model asserting it.
    """

    FILE_CHANGED = "file_changed"
    GIT_DIFF_REVIEWED = "git_diff_reviewed"
    TESTS_PASSED = "tests_passed"
    LINT_PASSED = "lint_passed"
    TYPECHECK_PASSED = "typecheck_passed"
    BUILD_SUCCEEDED = "build_succeeded"
    HEALTH_CHECK_PASSED = "health_check_passed"
    SMOKE_TEST_PASSED = "smoke_test_passed"
    MIGRATION_APPLIED = "migration_applied"
    SCHEMA_VERIFIED = "schema_verified"
    CHECKPOINT_CREATED = "checkpoint_created"


class FailureKind(StrEnum):
    """Failure taxonomy driving repair strategy (plan section 27)."""

    SYNTAX_ERROR = "syntax_error"
    TYPE_ERROR = "type_error"
    TEST_FAILURE = "test_failure"
    DEPENDENCY_CONFLICT = "dependency_conflict"
    BUILD_FAILURE = "build_failure"
    RUNTIME_FAILURE = "runtime_failure"
    TOOL_FAILURE = "tool_failure"

    #: The step ended without producing the evidence its gate requires. Not a
    #: tool failure -- every call may have succeeded -- and not a test failure.
    #: The work is simply unfinished, and naming that is what lets the runtime
    #: offer another attempt instead of blocking.
    INCOMPLETE_EVIDENCE = "incomplete_evidence"
    PERMISSION_FAILURE = "permission_failure"
    MISSING_CONTEXT = "missing_context"
    PLAN_INVALIDATION = "plan_invalidation"
    RESOURCE_LIMIT = "resource_limit"
    NETWORK_FAILURE = "network_failure"
    DATABASE_FAILURE = "database_failure"
    SERVICE_HEALTH_FAILURE = "service_health_failure"


class ApprovalDecision(StrEnum):
    """Outcome of an approval request. ``TIMED_OUT`` is never an implicit yes."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    TIMED_OUT = "timed_out"


class FactKind(StrEnum):
    """What a remembered project fact is about (plan section 13.1, layer 2)."""

    CONVENTION = "convention"
    STACK = "stack"
    ENVIRONMENT = "environment"
    LIMITATION = "limitation"
    DEPENDENCY = "dependency"
    CONSTRAINT = "constraint"


class FactOrigin(StrEnum):
    """How a fact was learned.

    This is what confidence is derived from, and why it is not a number the
    model chooses. An ``OBSERVED`` fact traces to a tool event; an ``ASSERTED``
    one is a model's claim and starts low. ``USER`` outranks both, because a
    user-stated constraint is not something the runtime gets to second-guess.
    """

    OBSERVED = "observed"
    DERIVED = "derived"
    ASSERTED = "asserted"
    USER = "user"


class DecisionStatus(StrEnum):
    """ADR lifecycle (plan section 15.13)."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class MemoryKind(StrEnum):
    """What a memory record holds."""

    FAILURE_LESSON = "failure_lesson"
    TASK_SUMMARY = "task_summary"


__all__ = [
    "AgentState",
    "ApprovalDecision",
    "DecisionStatus",
    "FactKind",
    "FactOrigin",
    "MemoryKind",
    "ArtifactKind",
    "ArtifactStatus",
    "EvidenceKind",
    "FailureKind",
    "Phase",
    "Risk",
    "RunStatus",
    "StepOutcome",
    "TaskKind",
]
