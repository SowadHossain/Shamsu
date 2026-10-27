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

import logging
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from shamsu.context.budget import count_tokens, fit_token_budget
from shamsu.memory.service import MemoryService
from shamsu.memory.types import NOTE_KINDS

# Notes live in the workspace. SQLite is authoritative; Markdown exports
# are generated views and must be edited through the memory API. Under
# `memory/` rather than at the root of `.shamsu/`, next to the
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

_WORD_RE = re.compile(r"[^\W\d]\w*", re.UNICODE)


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
    pinned: bool = False

    def as_line(self) -> str:
        return f"[{self.type}] {self.title}: {self.content}"


class MemoryStore:
    """Compact note view over the same SQLite records used by /memory.

    Legacy JSON/markdown are migration inputs, never rewritten. Markdown under
    notes/exports is a generated view; use remember (same type/title updates)
    and forget to change authoritative records.
    """

    def __init__(self, workspace: Path) -> None:
        from shamsu import paths

        self.workspace = Path(workspace).resolve()
        self.root = paths.memory_notes_dir(self.workspace)
        self.root.mkdir(parents=True, exist_ok=True)
        self.service = MemoryService(self.workspace)
        self.notes: dict[str, Note] = {}
        self._loaded_updated: dict[str, str] = {}
        self._owned: set[str] = set()
        self._load()

    @property
    def index_path(self) -> Path:
        """Legacy import path; SQLite is authoritative after migration."""
        return self.root / INDEX_FILENAME

    def _load(self) -> None:
        self.notes = {}
        self._owned = set()
        reverse_kinds = {kind: note_type for note_type, kind in NOTE_KINDS.items()}
        for memory in self.service.list_local():
            payload = memory.metadata.get("note")
            if isinstance(payload, dict):
                known = {key: value for key, value in payload.items() if key in Note.__dataclass_fields__}
                known.update(id=memory.memory_id)
                known.setdefault("type", reverse_kinds.get(memory.kind, "context"))
                known.setdefault("title", "")
                known.setdefault("content", memory.text)
                known["type"] = known["type"] if known["type"] in MEMORY_TYPES else "context"
                known["title"] = str(known["title"] or "")
                known["content"] = str(known["content"] or memory.text)
                tags = known.get("tags")
                known["tags"] = [str(tag) for tag in tags] if isinstance(tags, list) else []
                for key in ("created_at", "updated_at", "last_used_at"):
                    known[key] = str(known.get(key) or "")
                note = Note(**known)
                self._owned.add(note.id)
            else:
                note = Note(
                    id=memory.memory_id, type=reverse_kinds.get(memory.kind, "convention"),
                    title=memory.kind.replace("_", " "), content=memory.text,
                    created_at=datetime.fromtimestamp(memory.created_at, timezone.utc).isoformat(),
                    pinned=memory.kind in {"user_preference", "safety_rule"},
                )
            self.notes[note.id] = note
        self._loaded_updated = {key: note.updated_at for key, note in self.notes.items()}
        self._baseline = {key: asdict(note) for key, note in self.notes.items()}

    def _save(self) -> None:
        """Compatibility for tier/usage changes; never replace a store snapshot."""
        for note in self.notes.values():
            if note.id not in self._owned:
                continue
            if asdict(note) == self._baseline.get(note.id):
                continue
            note.updated_at = _now()
            saved = self.service.update_note_metadata(
                note.id, asdict(note), self._loaded_updated.get(note.id, ""),
            )
            if saved:
                self._loaded_updated[note.id] = note.updated_at
                self._baseline[note.id] = asdict(note)

    def _export(self, note: Note) -> str:
        """Best-effort generated view, separate from manually edited legacy files."""
        directory = self.root / "exports"
        temporary = directory / f".{uuid.uuid4().hex}.tmp"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", note.id)
            temporary.write_text(
                f"<!-- Generated from memory.db; edit using memory_remember with the same type/title. -->\n"
                f"# {note.title}\n\n- type: {note.type}\n- tags: {', '.join(note.tags) or '-'}\n"
                f"- tier: {note.tier}\n- id: {note.id}\n\n{note.content}\n",
                encoding="utf-8",
            )
            os.replace(temporary, directory / f"{note.type}-{safe_id}.md")
            return ""
        except OSError as exc:
            logging.getLogger(__name__).warning("Memory saved, but export failed: %s", exc)
            return " Markdown export unavailable."
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def remember(self, note_type: str, title: str, content: str,
                 tags: list[str] | None = None) -> tuple[bool, str]:
        kind = (note_type or "").strip().lower()
        if kind not in MEMORY_TYPES:
            return False, f"Unknown memory type {note_type!r}. Use one of: " + ", ".join(MEMORY_TYPES)
        clean_title = " ".join((title or "").split())[:120]
        clean_content = " ".join((content or "").split())[:MAX_NOTE_CHARS]
        if not clean_title or not clean_content:
            return False, "A note needs both a short title and the fact itself."
        note = Note(id="", type=kind, title=clean_title, content=clean_content,
                    tags=[str(tag).strip().lower() for tag in tags or [] if str(tag).strip()][:6],
                    created_at=_now(), updated_at=_now(), last_used_at=_now())
        result = self.service.remember_local(
            f"{clean_title}: {clean_content}", NOTE_KINDS[kind],
            {"note": asdict(note), "source": "model_note"},
        )
        if not result.get("ok"):
            return False, str(result.get("error") or result.get("reason") or "Memory was not saved.")
        try:
            self._load()
        except Exception as exc:  # noqa: BLE001 - the fact is already committed
            logging.getLogger(__name__).warning("Memory saved, but refresh failed: %s", exc)
            return True, f"Remembered [{kind}] {clean_title}. Saved to SQLite; the memory view could not refresh."
        note = self.notes.get(str(result["memory_id"]))
        if note is None:
            return False, "Memory was saved but could not be read back."
        warning = ""
        try:
            self.tidy()
        except Exception as exc:  # noqa: BLE001 - maintenance cannot undo the commit
            logging.getLogger(__name__).warning("Memory saved, but cleanup failed: %s", exc)
            warning = " Memory cleanup unavailable."
        warning += self._export(note)
        prefix = "Already remembered" if result.get("deduped") else "Remembered"
        return True, f"{prefix} [{note.type}] {note.title} ({len(self.notes)} notes held)." + warning

    def forget(self, note_id: str) -> tuple[bool, str]:
        self._load()
        note = self.notes.get((note_id or "").strip())
        if note is None:
            return False, f"No memory with id {note_id!r}."
        result = self.service.forget(note.id)
        self._load()
        return bool(result.get("ok")), f"Forgot [{note.type}] {note.title}." if result.get("ok") else str(result.get("error"))

    # -- reading ---------------------------------------------------------

    def recall(self, task: str, limit: int = MAX_RECALLED_NOTES) -> list[Note]:
        """The notes worth showing for *task*, best first.

        This is the whole reason the store can grow without the prompt growing
        with it: hundreds of notes on disk, five in the window.
        """
        self._load()
        words = {
            word.lower()
            for word in _WORD_RE.findall(task or "")
            if len(word) >= 3
        }
        if not words:
            return []
        scored: list[tuple[float, Note]] = []
        for note in self.notes.values():
            title = note.title.lower() if note.id in self._owned else ""
            body = f"{title} {note.content} {' '.join(note.tags)}".lower()
            score = 0
            for word in words:
                if word in body:
                    score += _WEIGHT_BODY
                if word in title:
                    score += _WEIGHT_TITLE
                if any(word in tag for tag in note.tags):
                    score += _WEIGHT_TAG
            if note.pinned:
                score += 100
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
            self.service.touch_notes([note.id for note in chosen])
        return chosen

    def all_notes(self) -> list[Note]:
        self._load()
        return sorted(self.notes.values(), key=lambda note: note.created_at)

    def stats(self) -> dict[str, int]:
        self._load()
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
            if note.id not in self._owned:
                continue
            age = _age_days(note.last_used_at or note.created_at)
            if note.tier == "hot" and age > ARCHIVE_AFTER_DAYS:
                note.tier = "archive"
                changed += 1
            elif note.tier == "archive" and age > DELETE_AFTER_DAYS:
                self.service.forget(note.id)
                del self.notes[note.id]
                changed += 1
        hot = [n for n in self.notes.values() if n.tier == "hot" and n.id in self._owned]
        if len(hot) > HOT_CAP:
            hot.sort(key=lambda note: note.last_used_at or note.created_at)
            for note in hot[: len(hot) - HOT_CAP]:
                note.tier = "archive"
                changed += 1
        if changed:
            self._save()
        return changed


def render_memory(workspace: Path, task: str = "") -> str:
    """The memory block for this turn, or ``""``.

    Scoped to *task*: with no task there is nothing to be relevant to, so
    nothing is loaded. That is the point - an unconditional dump of every note
    is the thing this design exists to avoid.
    """
    try:
        store = MemoryStore(workspace)
        notes = store.recall(task) if task.strip() else []
    except Exception as exc:  # noqa: BLE001 - memory must never break a turn
        logging.getLogger(__name__).warning("Memory recall unavailable: %s", exc)
        return ""
    if not notes:
        return ""
    lines: list[str] = []
    header = "What you noted earlier about this project:\n"
    for note in notes:
        line = fit_token_budget(f"- {note.as_line()}", MAX_MEMORY_TOKENS // 3,
                                " ... [use memory_list for the full record]")
        if count_tokens(header + "\n".join(lines + [line])) > MAX_MEMORY_TOKENS:
            break
        lines.append(line)
    if not lines:
        return ""
    return header + "\n".join(lines)


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
