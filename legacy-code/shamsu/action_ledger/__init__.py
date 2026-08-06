"""ActionLedger: local human-facing debug/audit trail for SHAMSU runs.

Boundary (see agent context/prompts/audit_log.md): this is NOT memory, NOT
Graphiti, NOT Codebase-Memory MCP, NOT the context pipeline. It is never
retrieved into a model call automatically. It exists so a user can manually
inspect what SHAMSU did during one run: prompt, decisions, tool calls,
commands, patches, and the final answer.
"""
from __future__ import annotations

from shamsu.action_ledger.config import DEFAULT_CONFIG, load_config, save_config
from shamsu.action_ledger.context import clear_current_run, get_current_run, set_current_run
from shamsu.action_ledger.ids import new_run_id
from shamsu.action_ledger.ledger import ActionLedger, start_run

__all__ = [
    "ActionLedger",
    "start_run",
    "new_run_id",
    "DEFAULT_CONFIG",
    "load_config",
    "save_config",
    "get_current_run",
    "set_current_run",
    "clear_current_run",
]
