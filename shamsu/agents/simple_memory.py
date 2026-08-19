"""Typed, task-scoped project memory the model writes for itself.

Modelled on smallcode `bin/memory.js` + `src/memory/hygiene.js`
(MIT, (c) 2026 Doorman11991 - see reference/smallcode/LICENSE). The type
vocabulary, the retrieval scoring weights and the hot/archive tier model are
theirs; the Python is ours.

Distinct from the rolling summary, and the difference is who is speaking. The
summary is OUR lossy digest, written by the harness when the window fills. This
is the model's own note, written when it decides something, and it survives
compaction because it was never part of the conversation being compacted.

Three things make this affordable, and the first is the one that matters:

* **Only what the turn needs is loaded.** A flat notes file grows into every
  prompt forever. Here the store can hold hundreds of notes while the prompt
  carries the five that score against *this* request. That is the difference
  between memory and a permanent tax on the window.
* **Notes are typed** - a `gotcha` and a `convention` have different lifetimes
  and different value, and a decision from before the project took its current
  shape should not outrank one from this morning.
* **They decay.** Hot notes that go unused are archived, archived ones are
  eventually forgotten. A stale fact stated confidently is worse than no fact.

The block is charged to the context budget like everything else - see
`SimpleChatLoop._fixed_overhead`. An uncounted standing block that grows for
the life of a project is precisely the bug that made a 21,381-token estimate
out of a ~31,400-token prompt.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shamsu.context.budget import count_tokens

# Notes live in the workspace so they are per-project, reviewable and editable
# by hand. Under `memory/` rather than at the root of `.shamsu/`, next to the
# long-term SQLite store, so there is one place to look for "what does SHAMSU
# remember" rather than two.
MEMORY_DIR = Path(".shamsu") / "memory" / "notes"
INDEX_FILENAME = "index.json"

# smallcode's vocabulary. Each answers a different question a later turn asks.
MEMORY_TYPES = (
    "decision",    # a choice a later turn must respect
    "workflow",    # how to build, run or test this project
    "gotcha",      # a trap and its workaround
    "convention",  # naming, layout, style this project follows
    "context",     # what the project IS - domain, intent, shape
)

# What one recall may put in the prompt.
MAX_RECALLED_NOTES = 5
MAX_MEMORY_TOKENS = 600
MAX_NOTE_CHARS = 400

# Retrieval weights, from smallcode's `loadForTask`: a hit in the title is worth
# three body hits, a tag two. Title and tags are what the model chose to call
# the thing, so they carry more intent than an incidental word in the body.
_WEIGHT_BODY = 1
_WEIGHT_TITLE = 3
_WEIGHT_TAG = 2

# Tier model, from `src/memory/hygiene.js`. An archived note is not deleted, it
# is de-ranked - still findable, no longer crowding the hot set.
HOT_CAP = 20
ARCHIVE_AFTER_DAYS = 60
DELETE_AFTER_DAYS = 90
ARCHIVE_RANK_WEIGHT = 0.3

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_days(stamp: str) -> float:
    try:
        when = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when).total_seconds() / 86400.0


@dataclass
class Note:
    id: str
    type: str
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    last_used_at: str = ""
    tier: str = "hot"

    def as_line(self) -> str:
        return f"[{self.type}] {self.title}: {self.content}"


class MemoryStore:
    """Typed notes on disk: one index plus a markdown file per note.

    The markdown duplicates the index on purpose. The index is what this class
    reads; the markdown is what a human reads, greps and deletes by hand, and
    a memory a user cannot inspect is one they cannot trust.
    """

    def __init__(self, workspace: Path) -> None:
        from shamsu import paths

        self.root = paths.memory_notes_dir(Path(workspace))
        self.notes: dict[str, Note] = {}
        self._load()

    # -- persistence -----------------------------------------------------

    @property
    def index_path(self) -> Path:
        return self.root / INDEX_FILENAME

    def _load(self) -> None:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for entry in raw.get("notes", []):
            if not isinstance(entry, dict) or not entry.get("id"):
                continue
            known = {f: entry.get(f) for f in Note.__dataclass_fields__ if f in entry}
            known.setdefault("type", "context")
            known.setdefault("title", "")
            known.setdefault("content", "")
            try:
                self.notes[str(known["id"])] = Note(**known)  # type: ignore[arg-type]
            except TypeError:
                continue

    def _save(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            self.index_path.write_text(
                json.dumps(
                    {"version": 1, "updated_at": _now(),
                     "notes": [asdict(note) for note in self.notes.values()]},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            return
        live = set()
        for note in self.notes.values():
            name = f"{note.type}-{note.id}.md"
            live.add(name)
            try:
                (self.root / name).write_text(
                    f"# {note.title}\n\n"
                    f"- type: {note.type}\n"
                    f"- tags: {', '.join(note.tags) or '-'}\n"
                    f"- tier: {note.tier}\n"
                    f"- created: {note.created_at}\n\n"
                    f"{note.content}\n",
                    encoding="utf-8",
                )
            except OSError:
                continue
        # Drop markdown for notes that decayed away, so the folder is a true
        # picture of what is remembered rather than an archaeological record.
        try:
            for path in self.root.glob("*.md"):
                if path.name not in live:
                    path.unlink()
        except OSError:
            pass

    # -- writing ---------------------------------------------------------

    def remember(
        self,
        note_type: str,
        title: str,
        content: str,
        tags: list[str] | None = None,
    ) -> tuple[bool, str]:
        kind = (note_type or "").strip().lower()
        if kind not in MEMORY_TYPES:
            return False, (
                f"Unknown memory type {note_type!r}. Use one of: "
                + ", ".join(MEMORY_TYPES)
            )
        clean_title = " ".join((title or "").split())[:120]
        clean_content = " ".join((content or "").split())[:MAX_NOTE_CHARS]
        if not clean_title or not clean_content:
            return False, "A note needs both a short title and the fact itself."
        existing = self._same_note(kind, clean_title, clean_content)
        if existing is not None:
            # A model that decided something once will decide it again every
            # turn. Twenty copies of a line is how a budget evaporates.
            existing.updated_at = _now()
            existing.last_used_at = _now()
            existing.tier = "hot"
            self._save()
            return True, f"Already remembered as [{existing.type}] {existing.title}."
        note = Note(
            id=uuid.uuid4().hex[:8],
            type=kind,
            title=clean_title,
            content=clean_content,
            tags=[t.strip().lower() for t in (tags or []) if str(t).strip()][:6],
            created_at=_now(),
            updated_at=_now(),
            last_used_at=_now(),
        )
        self.notes[note.id] = note
        self.tidy()
        self._save()
        return True, f"Remembered [{note.type}] {note.title} ({len(self.notes)} notes held)."

    def _same_note(self, kind: str, title: str, content: str) -> Note | None:
        key = (title.lower(), content.lower())
        for note in self.notes.values():
            if note.type == kind and (note.title.lower(), note.content.lower()) == key:
                return note
        return None

    def forget(self, note_id: str) -> tuple[bool, str]:
        note = self.notes.pop((note_id or "").strip(), None)
        if note is None:
            return False, f"No memory with id {note_id!r}."
        self._save()
        return True, f"Forgot [{note.type}] {note.title}."

    # -- reading ---------------------------------------------------------

    def recall(self, task: str, limit: int = MAX_RECALLED_NOTES) -> list[Note]:
        """The notes worth showing for *task*, best first.

        This is the whole reason the store can grow without the prompt growing
        with it: hundreds of notes on disk, five in the window.
        """
        words = {
            word.lower()
            for word in _WORD_RE.findall(task or "")
            if len(word) >= 3
        }
        if not words:
            return []
        scored: list[tuple[float, Note]] = []
        for note in self.notes.values():
            body = f"{note.title} {note.content} {' '.join(note.tags)}".lower()
            title = note.title.lower()
            score = 0
            for word in words:
                if word in body:
                    score += _WEIGHT_BODY
                if word in title:
                    score += _WEIGHT_TITLE
                if any(word in tag for tag in note.tags):
                    score += _WEIGHT_TAG
            if score <= 0:
                continue
            weighted = score * (ARCHIVE_RANK_WEIGHT if note.tier == "archive" else 1.0)
            scored.append((weighted, note))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        chosen = [note for _score, note in scored[:limit]]
        for note in chosen:
            note.last_used_at = _now()
            note.tier = "hot"
        if chosen:
            self._save()
        return chosen

    def all_notes(self) -> list[Note]:
        return sorted(self.notes.values(), key=lambda note: note.created_at)

    def stats(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for note in self.notes.values():
            counts[note.type] = counts.get(note.type, 0) + 1
        return counts

    # -- decay -----------------------------------------------------------

    def tidy(self) -> int:
        """Archive what has gone unused, forget what has been archived too long.

        Kept deliberately quiet and best-effort: memory hygiene must never be
        the reason a turn fails.
        """
        changed = 0
        for note in list(self.notes.values()):
            age = _age_days(note.last_used_at or note.created_at)
            if note.tier == "hot" and age > ARCHIVE_AFTER_DAYS:
                note.tier = "archive"
                changed += 1
            elif note.tier == "archive" and age > DELETE_AFTER_DAYS:
                del self.notes[note.id]
                changed += 1
        hot = [n for n in self.notes.values() if n.tier == "hot"]
        if len(hot) > HOT_CAP:
            hot.sort(key=lambda note: note.last_used_at or note.created_at)
            for note in hot[: len(hot) - HOT_CAP]:
                note.tier = "archive"
                changed += 1
        return changed


def render_memory(workspace: Path, task: str = "") -> str:
    """The memory block for this turn, or ``""``.

    Scoped to *task*: with no task there is nothing to be relevant to, so
    nothing is loaded. That is the point - an unconditional dump of every note
    is the thing this design exists to avoid.
    """
    try:
        store = MemoryStore(workspace)
    except Exception:  # noqa: BLE001 - memory must never break a turn
        return ""
    notes = store.recall(task) if task.strip() else []
    if not notes:
        return ""
    lines: list[str] = []
    used = 0
    for note in notes:
        line = f"- {note.as_line()}"
        cost = count_tokens(line)
        if used + cost > MAX_MEMORY_TOKENS:
            break
        lines.append(line)
        used += cost
    if not lines:
        return ""
    return "What you noted earlier about this project:\n" + "\n".join(lines)


def remember(
    workspace: Path,
    note_type: str,
    title: str,
    content: str,
    tags: list[str] | None = None,
) -> tuple[bool, str]:
    try:
        return MemoryStore(workspace).remember(note_type, title, content, tags)
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not save that note: {exc}"
