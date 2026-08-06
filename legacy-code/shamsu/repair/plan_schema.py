"""Ollama-native JSON schema for a strict-debug-mode RepairPlan.

Passed as the `format` param to `/api/generate` so the local model is
constrained to emit a single-root-cause repair plan and nothing else. Mirrors
`shamsu.repair.types.RepairPlan`: EITHER search+replace OR full_content.
"""
from __future__ import annotations

REPAIR_PLAN_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "target_file": {"type": "string"},
        "search": {"type": "string"},
        "replace": {"type": "string"},
        "full_content": {"type": "string"},
    },
    # Ollama's local structured-output implementations are more reliable with
    # one flat required list than with anyOf/oneOf. The unused edit mode is an
    # empty string; LLMProposer still enforces that one mode is actionable.
    "required": ["root_cause", "target_file", "search", "replace", "full_content"],
    "additionalProperties": False,
}
