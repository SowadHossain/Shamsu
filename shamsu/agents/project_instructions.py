"""Per-workspace standing instructions, re-read every turn.

SHAMSU had no equivalent of this: ``_system_prompt`` was a static string plus the
workspace path, so there was nowhere for a project to state a rule that had to
hold for the whole session. Anything the user said once - "PostgreSQL 16, never
SQLite", "all API routes go in core/api.py" - lived only in the first user
message, and was the first thing dropped when the budget trimmer evicted early
history.

Codex CLI's lesson is that a persistent instruction file beats a summary: a file
is re-read intact on every turn, while a rolling summary is a lossy paraphrase
that degrades each time it is folded. So this is deliberately re-read per turn
rather than cached at construction - editing the file mid-session takes effect on
the next turn, and the content can never be paraphrased away.

Deliberately capped. A 200-line instruction file on a 7B with a 32k window would
crowd out the file contents the model actually needs to edit, so an oversized file
is truncated with a visible marker rather than silently eating the budget.
"""
from __future__ import annotations

from pathlib import Path

# Checked in order; the first that exists wins. SHAMSU.md is the native name;
# AGENTS.md is the cross-tool convention, so a repo already carrying one for
# another agent works without duplication.
INSTRUCTION_FILENAMES: tuple[str, ...] = (
    "SHAMSU.md",
    "AGENTS.md",
    ".shamsu/instructions.md",
)

# ~2k tokens. Enough for real standing rules, small enough to leave a 7B room to
# read and rewrite an actual source file in the same turn.
MAX_INSTRUCTION_CHARS = 8000

_HEADER = "## Project instructions"
_PREAMBLE = (
    "Standing rules for THIS workspace, from {source}. They outrank your general "
    "defaults. If a rule here conflicts with what you were about to do, follow the "
    "rule and say so."
)
_TRUNCATION_NOTICE = (
    "\n\n[...truncated by SHAMSU: {name} exceeds {limit} characters. Move anything "
    "load-bearing above this point.]"
)


def find_instruction_file(workspace: Path) -> Path | None:
    """The active instruction file for *workspace*, or None."""
    for name in INSTRUCTION_FILENAMES:
        candidate = workspace / name
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def load_project_instructions(workspace: Path) -> str:
    """The rendered project-instructions block, or ``""`` when there is none.

    Never raises: an unreadable instruction file must not break a turn, it just
    means the workspace has no standing rules this round.
    """
    path = find_instruction_file(workspace)
    if path is None:
        return ""
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not body.strip():
        return ""
    name = path.name
    if len(body) > MAX_INSTRUCTION_CHARS:
        body = body[:MAX_INSTRUCTION_CHARS].rstrip() + _TRUNCATION_NOTICE.format(
            name=name, limit=MAX_INSTRUCTION_CHARS
        )
    return f"\n{_HEADER}\n{_PREAMBLE.format(source=name)}\n\n{body.strip()}\n"
