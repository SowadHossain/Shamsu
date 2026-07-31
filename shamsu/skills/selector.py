"""Deterministic skill selection.

This intentionally starts metadata-first. A bounded model ranker can be added
later, but the first working version should be predictable, fast, and testable.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from shamsu.context.budget import count_tokens
from shamsu.skills.loader import discover_skills, skills_mode_from_env
from shamsu.skills.types import SelectedSkill, SkillCatalog, SkillPackage, SkillSelection

DEFAULT_MAX_SKILLS = 5
DEFAULT_SKILL_BUDGET_TOKENS = 1800
_CODING_INTENTS = {"code_edit", "bug_fix", "test_gen", "doc_gen", "generate"}
_PRD_RE = re.compile(r"\b(prd|product requirements?|acceptance|build the complete project)\b", re.I)
_REACT_RE = re.compile(r"\b(react|vite|tsx|jsx|frontend|spa|dashboard)\b", re.I)
_UI_RE = re.compile(r"\b(ui|ux|design|responsive|mobile|browser|screen|layout|dashboard)\b", re.I)
_SQLITE_RE = re.compile(r"\b(sqlite|database|persistence|persist|seed|migration|schema)\b", re.I)
_TEST_RE = re.compile(r"\b(test|tests|testing|vitest|pytest|playwright|acceptance|verify|build)\b", re.I)
_MCP_RE = re.compile(r"\b(mcp|model context protocol|external tool|external server)\b", re.I)


def select_skills_for_task(
    workspace: Path | None,
    request: str,
    *,
    intent: str = "",
    stack: str = "",
    required_tools: Iterable[str] = (),
    target_files: Iterable[str] = (),
    max_skills: int = DEFAULT_MAX_SKILLS,
    budget_tokens: int = DEFAULT_SKILL_BUDGET_TOKENS,
    catalog: SkillCatalog | None = None,
    mode: str | None = None,
) -> SkillSelection:
    selected_mode = _normalize_mode(mode or skills_mode_from_env())
    if selected_mode == "off":
        return SkillSelection(mode="off")

    catalog = catalog or discover_skills(workspace)
    text = _selection_text(request, stack, required_tools, target_files, workspace)
    candidates: dict[str, tuple[float, list[str]]] = {}

    if intent in _CODING_INTENTS or _looks_like_coding_request(text):
        _add(candidates, "developer", 100.0, "default for coding work")
    if _PRD_RE.search(text):
        _add(candidates, "prd-planner", 85.0, "PRD or acceptance evidence requested")
    if _REACT_RE.search(text) or _workspace_has_any(workspace, ("package.json", "vite.config.ts")):
        _add(candidates, "react-vite", 75.0, "React/Vite/frontend evidence detected")
    if _UI_RE.search(text):
        _add(candidates, "ui-designer", 55.0, "UI or responsive design work requested")
    if _SQLITE_RE.search(text):
        _add(candidates, "sqlite-persistence", 55.0, "persistence, seed, or database work requested")
    if _TEST_RE.search(text):
        _add(candidates, "testing", 45.0, "test or acceptance verification requested")
    if _MCP_RE.search(text):
        _add(candidates, "mcp-tools", 45.0, "external MCP/tool capability requested")

    for name, skill in catalog.skills.items():
        if name in candidates:
            continue
        matched = _metadata_match(skill, text)
        if matched:
            is_reference = str(skill.metadata.get("kind") or "").lower() == "reference"
            score = 92.0 if is_reference else 30.0
            reason = (
                f"named ingested reference matched: {matched}"
                if is_reference
                else f"matched skill metadata: {matched}"
            )
            _add(candidates, name, score, reason)

    candidates = _add_dependencies(candidates, catalog)
    selected, rejected = _resolve_conflicts_and_budget(candidates, catalog, max_skills, budget_tokens)
    return SkillSelection(
        mode=selected_mode,
        selected=tuple(selected),
        rejected=tuple(rejected),
        issues=catalog.issues,
        budget_tokens=budget_tokens,
    )


def render_skill_context(selection: SkillSelection) -> str:
    if not selection.active:
        return ""
    lines = ["## Active SHAMSU Skills"]
    remaining = selection.budget_tokens
    for item in selection.selected:
        skill = item.skill
        header = f"### {skill.name} ({skill.source})"
        reason = "; ".join(item.reasons)
        body = skill.instructions.strip()
        budget = min(skill.context_budget_tokens, max(remaining, 0))
        if budget <= 0:
            break
        rendered_body = _truncate_to_tokens(body, budget)
        remaining -= count_tokens(rendered_body)
        lines.extend(
            [
                "",
                header,
                f"Why selected: {reason or 'metadata match'}",
                rendered_body,
            ]
        )
    return "\n".join(lines).strip()


def selection_log_payload(selection: SkillSelection) -> dict:
    return selection.to_summary()


def _selection_text(
    request: str,
    stack: str,
    required_tools: Iterable[str],
    target_files: Iterable[str],
    workspace: Path | None,
) -> str:
    parts = [request, stack, " ".join(required_tools), " ".join(target_files)]
    if workspace is not None:
        for marker in ("package.json", "vite.config.ts", "pyproject.toml", "requirements.txt"):
            if (Path(workspace) / marker).exists():
                parts.append(marker)
    return "\n".join(parts).lower()


def _add(candidates: dict[str, tuple[float, list[str]]], name: str, score: float, reason: str) -> None:
    current_score, reasons = candidates.get(name, (0.0, []))
    candidates[name] = (current_score + score, [*reasons, reason])


def _add_dependencies(
    candidates: dict[str, tuple[float, list[str]]],
    catalog: SkillCatalog,
) -> dict[str, tuple[float, list[str]]]:
    expanded = dict(candidates)
    changed = True
    while changed:
        changed = False
        for name in list(expanded):
            skill = catalog.skills.get(name)
            if skill is None:
                continue
            for dependency in skill.dependencies:
                if dependency not in expanded and dependency in catalog.skills:
                    expanded[dependency] = (90.0, [f"dependency of {name}"])
                    changed = True
    return expanded


def _resolve_conflicts_and_budget(
    candidates: dict[str, tuple[float, list[str]]],
    catalog: SkillCatalog,
    max_skills: int,
    budget_tokens: int,
) -> tuple[list[SelectedSkill], list[dict[str, object]]]:
    selected: list[SelectedSkill] = []
    rejected: list[dict[str, object]] = []
    used_tokens = 0
    for name, (score, reasons) in sorted(candidates.items(), key=lambda item: (-item[1][0], item[0])):
        skill = catalog.skills.get(name)
        if skill is None:
            rejected.append({"name": name, "reason": "not installed"})
            continue
        conflict = next((item.skill.name for item in selected if item.skill.name in skill.conflicts), "")
        if conflict:
            rejected.append({"name": name, "reason": f"conflicts with {conflict}"})
            continue
        if len(selected) >= max_skills:
            rejected.append({"name": name, "reason": "max selected skill count reached"})
            continue
        instruction_tokens = count_tokens(skill.instructions)
        if selected and used_tokens + min(instruction_tokens, skill.context_budget_tokens) > budget_tokens:
            rejected.append({"name": name, "reason": "skill context budget reached"})
            continue
        selected.append(SelectedSkill(skill=skill, score=round(score, 3), reasons=tuple(reasons)))
        used_tokens += min(instruction_tokens, skill.context_budget_tokens)
    return selected, rejected


def _metadata_match(skill: SkillPackage, text: str) -> str:
    for item in (*skill.triggers, *skill.applies_to, *skill.tags):
        needle = item.strip().lower()
        if needle and needle in text:
            return item
    return ""


def _looks_like_coding_request(text: str) -> bool:
    return bool(
        re.search(r"\b(build|create|implement|fix|edit|update|write|add|refactor|debug)\b", text)
    )


def _workspace_has_any(workspace: Path | None, names: tuple[str, ...]) -> bool:
    if workspace is None:
        return False
    root = Path(workspace)
    return any((root / name).exists() for name in names)


def _truncate_to_tokens(text: str, max_tokens: int) -> str:
    if count_tokens(text) <= max_tokens:
        return text
    # count_tokens is char/4 by default; use a conservative character limit.
    limit = max(max_tokens * 4, 200)
    return text[:limit].rstrip() + "\n[skill instructions truncated by context budget]"


def _normalize_mode(raw: str) -> str:
    lowered = raw.strip().lower()
    if lowered in {"0", "false", "off", "no", "disabled"}:
        return "off"
    if lowered in {"shadow", "dry-run", "dry_run", "log"}:
        return "shadow"
    return "on"
