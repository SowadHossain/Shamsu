"""Compact carried-progress checklists for multi-step execution.

Instead of re-dumping the full plan / raw PRD text into every step's prompt —
which re-sends the same hundreds of tokens N times and still lets the model lose
track of what is left — each step carries a compact checklist of ALL items with
progress markers. Completeness (every requirement stays visible, nothing is
silently dropped between steps) at a fraction of the tokens.
"""
from __future__ import annotations

from collections.abc import Sequence


def render_progress_checklist(
    items: Sequence[str],
    current_index: int,
    *,
    header: str = "Progress",
    max_item_chars: int = 200,
) -> str:
    """Render ``items`` as a checklist with done (``x``) / current (``>``) /
    pending (blank) markers. ``current_index`` is 0-based; anything before it is
    marked done, the item at it is the one to implement now. Markers are ASCII on
    purpose (SHAMSU runs on Windows/cp1252 consoles and logs).

    Returns "" for an empty list so callers can drop the section entirely.
    """
    cleaned = [" ".join(str(item).split())[:max_item_chars] for item in items if str(item).strip()]
    if not cleaned:
        return ""
    lines = [f"## {header} (keep EVERY item satisfied - do not drop or undo any)"]
    for i, item in enumerate(cleaned):
        if i < current_index:
            mark, suffix = "x", ""
        elif i == current_index:
            mark, suffix = ">", "   <- implement THIS one now"
        else:
            mark, suffix = " ", ""
        lines.append(f"{i + 1}. [{mark}] {item}{suffix}")
    return "\n".join(lines)
