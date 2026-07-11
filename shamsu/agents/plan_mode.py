"""Plan mode: turn any task into a reviewable, step-by-step plan file.

`plan <task>` runs this workflow to produce a concrete implementation plan grounded
in the workspace, writes it to a markdown file under ``.shamsu/plans/`` for the user
to review/edit, and returns the parsed steps. The REPL stores it as a pending action
and, once the user replies "proceed", executes the steps one milestone at a time
(reusing the same MilestoneTask loop the PRD build uses).

This module never writes project files or asks for tools - it only plans. Execution
happens in the REPL through the existing agent-chat + task-tracker machinery.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from json_repair import repair_json

from shamsu.llm.manager import LLMManager
from shamsu.memory.service import MemoryService
from shamsu.plans.store import new_plan_id, parse_plan_steps, write_plan

_MAX_STEPS = 12

PLAN_SYSTEM = """You are SHAMSU's planner. Produce a concrete, minimal implementation plan for
the task, grounded ONLY in the provided workspace context. Output ONLY JSON matching the schema.
Rules:
- Reference REAL files from the context; do not invent files or frameworks.
- steps: an ordered list of small, verifiable actions (max 12). Each step is one focused change
  a coder can complete in a single pass. Put the file each step touches in target_file.
- Keep it minimal - only what the task needs. No boilerplate, no unrelated work.
- verification: a concrete command or check that proves the whole task is done."""

PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "context": {"type": "string"},
        "files": {"type": "array", "items": {"type": "string"}},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "target_file": {"type": "string"},
                    "verify": {"type": "string"},
                },
                "required": ["description"],
            },
        },
        "verification": {"type": "string"},
    },
    "required": ["title", "steps"],
}


@dataclass(frozen=True)
class PlanStep:
    description: str
    target_file: str = ""
    verify: str = ""


@dataclass(frozen=True)
class PlanDoc:
    plan_id: str
    path: Path
    markdown: str
    title: str
    route: str
    steps: list[str] = field(default_factory=list)


class PlanningWorkflow:
    def __init__(
        self,
        workspace: Path,
        llm: Any | None = None,
        search: Any | None = None,
        memory_service: MemoryService | None = None,
        session_logger: Any | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.llm = llm or LLMManager(session_logger=session_logger)
        self.search = search
        self.memory_service = memory_service or MemoryService(self.workspace)
        self.session_logger = session_logger

    async def run(self, task: str, route: str = "code_edit") -> PlanDoc:
        context_text = self._gather_context(task)
        raw = await self.llm.generate_structured(
            "planner", PLAN_SYSTEM, self._prompt(task, route, context_text), PLAN_SCHEMA
        )
        data = _loads(raw) or {}
        title = str(data.get("title") or task.strip()[:80] or "Plan").strip()
        plan_steps = _steps_from_data(data)
        markdown = _render_markdown(title, task, route, data, plan_steps)
        plan_id = new_plan_id()
        path = write_plan(self.workspace, plan_id, markdown)
        # Prefer the model's structured steps; fall back to re-parsing the rendered
        # markdown so a PlanDoc always agrees with what the file's `## Steps` says.
        steps = [step.description for step in plan_steps] or parse_plan_steps(markdown)
        self._log(plan_id, route, len(steps))
        return PlanDoc(
            plan_id=plan_id, path=path, markdown=markdown, title=title, route=route, steps=steps
        )

    # -- helpers ---------------------------------------------------------------

    def _gather_context(self, task: str) -> str:
        parts: list[str] = []
        files = self._relevant_files(task)
        if files:
            parts.append("Relevant files in the workspace:\n" + "\n".join(f"- {f}" for f in files))
        memory = ""
        try:
            memory = self.memory_service.render_relevant(task) or ""
        except Exception:
            memory = ""
        if memory:
            parts.append(memory)
        return "\n\n".join(parts)

    def _relevant_files(self, task: str) -> list[str]:
        if self.search is None:
            return []
        try:
            results = self.search.search(task, top_k=6)
        except Exception:
            return []
        seen: list[str] = []
        for result in results:
            path = getattr(result, "file_path", "")
            if path and path not in seen:
                seen.append(path)
        return seen[:6]

    def _prompt(self, task: str, route: str, context_text: str) -> str:
        return (
            f"## Task type\n{route}\n\n"
            f"## Workspace context\n{context_text or '(no workspace context found)'}\n\n"
            f"## Task\n{task.strip()}\n\n"
            "Produce the plan JSON now."
        )

    def _log(self, plan_id: str, route: str, step_count: int) -> None:
        if not self.session_logger:
            return
        try:
            self.session_logger.log(
                "plan.created",
                {"plan_id": plan_id, "route": route, "steps": step_count},
                f"Created plan {plan_id} ({step_count} steps)",
                workflow_id="plan-mode",
            )
        except Exception:
            pass


def _steps_from_data(data: dict) -> list[PlanStep]:
    out: list[PlanStep] = []
    for item in (data.get("steps") or [])[:_MAX_STEPS]:
        if isinstance(item, dict):
            description = str(item.get("description") or "").strip()
            if description:
                out.append(
                    PlanStep(
                        description=description,
                        target_file=str(item.get("target_file") or "").strip(),
                        verify=str(item.get("verify") or "").strip(),
                    )
                )
        elif isinstance(item, str) and item.strip():
            out.append(PlanStep(description=item.strip()))
    return out


def _render_markdown(
    title: str, task: str, route: str, data: dict, plan_steps: list[PlanStep]
) -> str:
    lines: list[str] = [f"# Plan: {title}", ""]
    lines.append(f"**Task:** {task.strip()}")
    lines.append(f"**Type:** {route}")
    lines.append("")
    lines.append("## Context")
    lines.append(str(data.get("context") or "").strip() or "(no additional context)")
    lines.append("")
    files = [str(f).strip() for f in (data.get("files") or []) if str(f).strip()]
    if files:
        lines.append("## Files to touch")
        lines.extend(f"- {f}" for f in files)
        lines.append("")
    lines.append("## Steps")
    if plan_steps:
        for index, step in enumerate(plan_steps, 1):
            suffix = f" (`{step.target_file}`)" if step.target_file else ""
            lines.append(f"{index}. {step.description}{suffix}")
    else:
        lines.append("1. (no steps were produced - edit this file to add them, then proceed)")
    lines.append("")
    lines.append("## Verification")
    lines.append(
        str(data.get("verification") or "").strip()
        or "Run the project's tests or build after the change."
    )
    lines.append("")
    lines.append("---")
    lines.append("_Review or edit this plan, then reply `proceed` (or run `/proceed`) to execute it._")
    return "\n".join(lines) + "\n"


def _loads(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(repair_json(text))
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None
