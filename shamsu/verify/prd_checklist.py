"""Soft PRD acceptance checklist (reported, never a gate).

The verifier's exit code is the only hard success gate. This produces an honest,
non-blocking checklist of PRD requirements with a heuristic "appears in the
generated code" signal, so the user can see at a glance which requirements were
plausibly implemented and which need a manual look. It never claims a
requirement is "done" - only whether keywords from it show up in the source.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_SOURCE_EXTS = {".ts", ".tsx", ".js", ".jsx", ".py", ".html", ".css", ".vue", ".svelte"}
_MAX_SOURCE_BYTES = 400_000
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "when", "each",
    "must", "should", "shall", "will", "have", "has", "are", "is", "a", "an",
    "of", "to", "on", "in", "it", "its", "be", "can", "game", "player", "players",
    "screen", "user", "users", "shows", "show", "after", "before", "than", "then",
}


@dataclass(frozen=True)
class ChecklistItem:
    requirement: str
    found_in_code: bool

    def to_dict(self) -> dict[str, Any]:
        return {"requirement": self.requirement, "found_in_code": self.found_in_code}


def build_prd_checklist(contract: Any, target_dir: Path | str) -> list[ChecklistItem]:
    if contract is None:
        return []
    criteria = list(getattr(contract, "acceptance_criteria", []) or [])
    if not criteria:
        criteria = list(getattr(contract, "mechanics", []) or [])
    criteria = criteria[:15]
    if not criteria:
        return []
    source = _read_source(Path(target_dir))
    return [ChecklistItem(text, _appears(text, source)) for text in criteria]


def render_checklist(items: list[ChecklistItem]) -> str:
    if not items:
        return ""
    lines = ["PRD requirements (heuristic, NOT verified - confirm manually):"]
    for item in items:
        mark = "~" if item.found_in_code else " "
        lines.append(f"  [{mark}] {item.requirement}")
    return "\n".join(lines)


def _read_source(target: Path) -> str:
    if not target.is_dir():
        return ""
    chunks: list[str] = []
    total = 0
    for path in sorted(target.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _SOURCE_EXTS:
            continue
        if "node_modules" in path.parts or ".shamsu" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        chunks.append(text.lower())
        total += len(text)
        if total >= _MAX_SOURCE_BYTES:
            break
    return "\n".join(chunks)


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9]{2,}", text.lower())
    return [w for w in words if w not in _STOPWORDS]


def _appears(requirement: str, source: str) -> bool:
    if not source:
        return False
    tokens = _tokens(requirement)
    if not tokens:
        return False
    matched = sum(1 for token in set(tokens) if token in source)
    return matched >= max(1, round(len(set(tokens)) * 0.4))
