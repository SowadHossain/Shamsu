"""`/diagnostics status|repair` support: report which diagnostic helpers are
available without silently falling back to the LLM as a parser."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from shamsu.diagnostics.setup import repair as _repair
from shamsu.diagnostics.setup import status as _status

REPAIR_STEPS = (
    "Diagnostics helper repair steps:\n"
    "1. Run `/diagnostics setup` to install optional local helpers (Drain3).\n"
    "2. Native structured output (JSON/JUnit) and the errorformat-style parser "
    "always work - no install needed.\n"
    "3. LLMLingua stays disabled by default; only enable it if you accept the "
    "extra (heavy) dependency, and it is never used on exact code/diagnostics."
)


def check(workspace: Path) -> dict[str, Any]:
    payload = _status(workspace)
    using_fallback_only = not payload["drain3_available"]
    payload["fallback_parsers_in_use"] = using_fallback_only
    payload["message"] = (
        "Diagnostics helpers ready." if not using_fallback_only else
        "Optional Drain3 log compaction is unavailable; using SHAMSU's small "
        "deterministic template-miner fallback for noisy runtime logs. Native "
        "structured output and the errorformat-style parser are unaffected."
    )
    return payload


def repair(workspace: Path) -> dict[str, Any]:
    result = _repair(workspace)
    result["manual_steps"] = REPAIR_STEPS
    return result


def format_report(payload: dict[str, Any]) -> str:
    lines = [
        "Diagnostics doctor",
        "  native structured output: available",
        "  errorformat-style parser: available",
        f"  reviewdog external binary: {payload.get('reviewdog_external_binary') or 'not configured (using built-in patterns)'}",
        f"  drain3 (runtime log compaction): {'available' if payload.get('drain3_available') else 'unavailable (fallback template-miner in use)'}",
        f"  llmlingua (prose compression): {'enabled' if payload.get('llmlingua_enabled') else 'disabled (default)'}"
        f", {'available' if payload.get('llmlingua_available') else 'not installed'}",
        f"  message: {payload.get('message', '')}",
    ]
    return "\n".join(lines)
