"""A scratchpad the model writes for itself.

Distinct from the rolling summary, and the difference is who is speaking. The
summary is OUR lossy digest of what happened, written by the harness when the
window fills. This is the model's own note, written deliberately at the moment
it decides something, and it survives compaction because it was never part of
the conversation being compacted.

That matters for a small model specifically: `.shamsu/memory.md` is where "the
window is 900x700" and "the port is 8080" live, so a turn twenty later does not
re-derive them from prose or invent them. SmallCode calls the same idea working
memory and says it "compensates for small models' limited internal reasoning".

The cost is real and permanent: every note is in every subsequent prompt. So it
is hard-capped, and - unlike every other standing block this codebase has grown
- it is CHARGED to the context budget from the day it ships. An uncounted block
that grows for the life of a project is precisely the bug that made a 21,381
token estimate out of a ~31,400 token prompt.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.context.budget import count_tokens

# Notes live here, in the workspace, so they are per-project and a user can read
# and edit them by hand. Markdown because a human is the second audience.
MEMORY_RELATIVE_PATH = Path(".shamsu") / "memory.md"

# The whole point is that it is cheap enough to carry forever. ~500 tokens is
# about 25 one-line notes; past that the oldest go, because a note from before
# the current shape of the project is more likely to be wrong than useful.
MAX_MEMORY_TOKENS = 500

# One note cannot be a file dump.
MAX_NOTE_CHARS = 300


def memory_path(workspace: Path) -> Path:
    return Path(workspace) / MEMORY_RELATIVE_PATH


def read_memory(workspace: Path) -> str:
    """Everything remembered about this project, or ``""``."""
    try:
        return memory_path(workspace).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _notes(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def trim_to_budget(notes: list[str], budget: int = MAX_MEMORY_TOKENS) -> list[str]:
    """Drop the OLDEST notes until the rest fit.

    Oldest rather than longest: a note written before the project took its
    current shape is the one most likely to be stale, and a stale fact stated
    confidently is worse than no fact at all.
    """
    kept = list(notes)
    while kept and count_tokens("\n".join(kept)) > budget:
        kept.pop(0)
    return kept


def remember(workspace: Path, note: str) -> tuple[bool, str]:
    """Add one note. Returns ``(ok, message)`` for the tool result.

    Deduplicated, because a model that has decided something once will happily
    decide it again every turn, and twenty copies of the same line is how a
    500-token budget evaporates.
    """
    text = " ".join((note or "").split())[:MAX_NOTE_CHARS]
    if not text:
        return False, "Nothing to remember - pass the fact you want to keep as `note`."
    existing = _notes(read_memory(workspace))
    line = text if text.startswith("- ") else f"- {text}"
    if line in existing:
        return True, "Already remembered - nothing to add."
    kept = trim_to_budget([*existing, line])
    dropped = len(existing) + 1 - len(kept)
    path = memory_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    except OSError as exc:
        return False, f"Could not write the note: {exc}"
    if dropped > 0:
        return True, (
            f"Remembered. The {dropped} oldest note(s) were dropped to stay within "
            "the memory budget."
        )
    return True, f"Remembered. {len(kept)} note(s) held for this project."


def render_memory(workspace: Path) -> str:
    """The block that goes into the prompt, or ``""`` when there is nothing."""
    notes = trim_to_budget(_notes(read_memory(workspace)))
    if not notes:
        return ""
    return "What you have chosen to remember about this project:\n" + "\n".join(notes)
