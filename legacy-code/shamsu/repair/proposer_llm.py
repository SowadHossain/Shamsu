"""LLMProposer: the strict-debug-mode model adapter for the repair loop.

Turns a `DebugContext` into a single `RepairPlan` by:
  1. rendering the STRICT_DEBUG_SYSTEM prompt + `build_debug_prompt(context)`,
  2. asking the local model for schema-constrained JSON,
  3. parsing it (with a json_repair fallback) into a `RepairPlan`.

It never fabricates a fix: empty/no-token output or unparseable JSON returns
`None`, which the loop reads as "model produced no actionable repair plan" and
stops rather than guessing. The model call is injected as a *synchronous*
callable so this adapter has no Ollama/asyncio dependency and is unit-testable;
the pipeline bridges async->sync at the wiring boundary (the loop runs in a
worker thread, so a plain `asyncio.run` there is safe).
"""
from __future__ import annotations

import json
from typing import Protocol

from json_repair import repair_json

from shamsu.repair.plan_schema import REPAIR_PLAN_JSON_SCHEMA
from shamsu.repair.prompt import STRICT_DEBUG_SYSTEM, build_debug_prompt
from shamsu.repair.types import DebugContext, RepairPlan


class GenerateJSON(Protocol):
    """(system, user, schema) -> raw model output string. Synchronous."""
    def __call__(self, system: str, user: str, schema: dict) -> str: ...


class LLMProposer:
    def __init__(
        self,
        generate: GenerateJSON,
        *,
        schema: dict | None = None,
        system: str = STRICT_DEBUG_SYSTEM,
    ) -> None:
        self._generate = generate
        self._schema = schema if schema is not None else REPAIR_PLAN_JSON_SCHEMA
        self._system = system

    def propose(self, context: DebugContext) -> RepairPlan | None:
        user = build_debug_prompt(context)
        try:
            raw = self._generate(self._system, user, self._schema)
        except Exception:
            # A model/transport failure is a "no plan", never a crash of the
            # loop and never a fabricated success.
            return None
        plan = _parse_plan(raw or "")
        if plan is None or not plan.has_edit:
            plan = self._retry_invalid_plan(user, raw or "")
            if plan is None or not plan.has_edit:
                return None
        return plan

    def _retry_invalid_plan(self, user: str, raw: str) -> RepairPlan | None:
        retry_user = (
            f"{user}\n\n"
            "## Previous invalid repair JSON\n"
            f"{_preview(raw)}\n\n"
            "That response was malformed, omitted required fields, or diagnosed the failure "
            "without an edit. Return corrected JSON only with root_cause, target_file, search, "
            "replace, and full_content keys. You MUST include either non-empty search+replace "
            "or non-empty full_content for one shown editable file. Set the unused edit mode "
            "to an empty string."
        )
        try:
            retry_raw = self._generate(self._system, retry_user, self._schema)
        except Exception:
            return None
        return _parse_plan(retry_raw or "")


def _parse_plan(raw: str) -> RepairPlan | None:
    text = (raw or "").strip()
    if not text:
        return None
    data = _loads(text)
    if not isinstance(data, dict):
        return None
    target = str(data.get("target_file") or "").strip()
    if not target:
        return None
    return RepairPlan(
        root_cause=str(data.get("root_cause") or "").strip(),
        target_file=target,
        search=str(data.get("search") or ""),
        replace=str(data.get("replace") or ""),
        full_content=str(data.get("full_content") or ""),
        inspected_files=[str(f) for f in (data.get("inspected_files") or [])],
    )


def _loads(text: str) -> object | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(repair_json(text))
    except Exception:
        return None


def _preview(text: str, limit: int = 1200) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit].rstrip() + "\n...<truncated>"
