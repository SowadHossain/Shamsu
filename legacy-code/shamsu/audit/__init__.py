"""Per-session JSONL audit trail under .shamsu/audit/.

This is the detailed, machine-readable record of every step SHAMSU takes for a
prompt: the user request, the route/workflow, the model tier, planner and
executor output, tool calls with args, tool outputs (stdout/stderr/result),
files read/written/edited, generated code, diffs, approvals, errors/timeouts,
and the final answer. It is written locally, never fed back to a model.

See `SessionAuditLog` for the API and the on-disk layout.
"""
from __future__ import annotations

from shamsu.audit.trail import SessionAuditLog

__all__ = ["SessionAuditLog"]
