"""Workspace-local session storage and structured event logging."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
import zipfile
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.safety.commands import redact
from shamsu.safety.sandbox import Sandbox
from shamsu.types import ContextPack

SNIPPET_PREVIEW_CHARS = 600
MAX_STRING_CHARS = 4000
MESSAGE_PREVIEW_CHARS = 16000

DEFAULT_TITLE = "Untitled Session"
# Legacy default titles that are treated as "not a real title yet" so the
# first meaningful prompt can auto-title over them.
PLACEHOLDER_TITLES = {"", DEFAULT_TITLE, "SHAMSU Session"}


@dataclass
class SessionMetadata:
    session_id: str
    title: str
    workspace: str
    created_at: str
    updated_at: str
    status: str = "active"
    last_user_prompt: str = ""
    event_count: int = 0
    parent_session_id: str | None = None
    # New (M+): auto-naming, transcript accounting, and summary bookkeeping.
    auto_titled: bool = False
    message_count: int = 0
    summary_updated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionMetadata":
        """Build metadata from a persisted dict, tolerating both old
        `session.json` files (missing new fields) and future files (unknown
        extra fields) so upgrades never crash on existing sessions."""
        known = {field.name for field in fields(cls)}
        filtered = {key: value for key, value in data.items() if key in known}
        return cls(**filtered)


class SessionManager:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.sandbox = Sandbox(self.workspace)
        self.root = self.sandbox.validate(Path(".shamsu") / "sessions")
        self.index_path = self.sandbox.validate(Path(".shamsu") / "sessions" / "index.json")

    def create_session(self, title: str | None = None, parent_session_id: str | None = None) -> "SessionLogger":
        now = _now()
        metadata = SessionMetadata(
            session_id=_new_session_id(),
            title=self.unique_title(title or DEFAULT_TITLE),
            workspace=str(self.workspace),
            created_at=now,
            updated_at=now,
            parent_session_id=parent_session_id,
        )
        self._write_metadata(metadata)
        self._upsert_index(metadata)
        logger = SessionLogger(self, metadata)
        logger.log("session.started", {"title": metadata.title}, "Session started")
        return logger

    def latest_active(self) -> "SessionLogger | None":
        sessions = [item for item in self.list_sessions() if item.status == "active"]
        if not sessions:
            return None
        return SessionLogger(self, sessions[0])

    def get_or_create_latest(self) -> "SessionLogger":
        latest = self.latest_active()
        if latest is None:
            return self.create_session()
        latest.log("session.resumed", {"query": "latest-active"}, "Session resumed")
        return latest

    def resume_session(self, query: str) -> "SessionLogger":
        metadata = self.resolve(query)
        metadata.status = "active"
        metadata.updated_at = _now()
        self._write_metadata(metadata)
        self._upsert_index(metadata)
        logger = SessionLogger(self, metadata)
        logger.log("session.resumed", {"query": query}, "Session resumed")
        return logger

    def resolve(self, query: str) -> SessionMetadata:
        matches = [
            item for item in self.list_sessions()
            if item.session_id == query
            or item.session_id.startswith(query)
            or item.title.lower().startswith(query.lower())
        ]
        if not matches:
            raise ValueError(f"No session found for: {query}")
        if len(matches) > 1:
            exact = [item for item in matches if item.session_id == query or item.title.lower() == query.lower()]
            if len(exact) == 1:
                return exact[0]
            raise ValueError(f"Session query is ambiguous: {query}")
        return matches[0]

    def list_sessions(self) -> list[SessionMetadata]:
        index = self._read_index()
        sessions = [SessionMetadata.from_dict(item) for item in index.get("sessions", [])]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def close_session(self, query: str) -> SessionMetadata:
        metadata = self.resolve(query)
        logger = SessionLogger(self, metadata)
        # Persisting a summary + best-effort long-term memory on close is what
        # lets a future session resume this one without replaying every event.
        # Both are best-effort: a failure must never block closing the session.
        try:
            summary = logger.update_summary_from_events()
            if summary.strip():
                logger.save_long_term_memory(
                    "session_summary",
                    f"Session '{metadata.title}' summary: {summary}",
                    {"reason": "session_closed"},
                )
        except Exception as exc:  # pragma: no cover - defensive
            logger.log("session.summary.failed", {"error": str(exc)}, "Summary on close failed", workflow_id="session")
        logger.log("session.closed", {}, "Session closed")
        metadata = logger.metadata
        metadata.status = "closed"
        metadata.updated_at = _now()
        self._write_metadata(metadata)
        self._upsert_index(metadata)
        return metadata

    def rename_session(self, query: str, title: str) -> SessionMetadata:
        metadata = self.resolve(query)
        # A manual rename is authoritative, so dedupe the visible title but never
        # auto-title over it afterwards.
        metadata.title = self.unique_title(title, exclude_session_id=metadata.session_id)
        metadata.auto_titled = True
        metadata.updated_at = _now()
        self._write_metadata(metadata)
        self._upsert_index(metadata)
        return metadata

    # -- Deterministic auto-naming ------------------------------------------

    def unique_title(self, desired_title: str, exclude_session_id: str | None = None) -> str:
        """Titles are display-only; `session_id` is the real identity. Keep the
        visible list unambiguous by suffixing duplicates as `Title (2)`, `(3)`."""
        desired = (desired_title or DEFAULT_TITLE).strip() or DEFAULT_TITLE
        existing = {
            item.title.strip().lower()
            for item in self.list_sessions()
            if item.session_id != exclude_session_id
        }
        if desired.lower() not in existing:
            return desired
        suffix = 2
        while f"{desired} ({suffix})".lower() in existing:
            suffix += 1
        return f"{desired} ({suffix})"

    def generate_title_from_prompt(self, prompt: str) -> str:
        """Deterministic, LLM-free title from the first meaningful task.

        Returns `Untitled Session` for greetings/affirmatives or when no useful
        head clause can be extracted."""
        return generate_title_from_prompt(prompt)

    def maybe_auto_title(self, logger: "SessionLogger", prompt: str) -> None:
        """Auto-name a still-placeholder session from its first meaningful
        prompt. Runs at most once and never overrides a manual/explicit title."""
        metadata = logger.metadata
        if metadata.auto_titled or not _is_placeholder_title(metadata.title):
            return
        candidate = generate_title_from_prompt(prompt)
        if candidate == DEFAULT_TITLE:
            return
        unique = self.unique_title(candidate, exclude_session_id=metadata.session_id)
        metadata.title = unique
        metadata.auto_titled = True
        metadata.updated_at = _now()
        self._write_metadata(metadata)
        self._upsert_index(metadata)
        logger.log(
            "session.auto_titled",
            {"title": unique, "from_prompt": prompt},
            f"Auto-titled session: {unique}",
            workflow_id="session",
        )

    def logger_for(self, query: str) -> "SessionLogger":
        """Resolve a session by id/title prefix and return a logger bound to it,
        for read-only inspection commands (trace/summary/memory)."""
        return SessionLogger(self, self.resolve(query))

    def search_sessions(self, query: str, limit: int = 40) -> list[dict[str, Any]]:
        """Local, JSONL-scan search across titles, summaries, transcript/chat
        messages, and local memory. No FTS/index — a simple scan is enough for
        the workspace-local scale."""
        needle = query.strip().lower()
        if not needle:
            return []
        matches: list[dict[str, Any]] = []
        for metadata in self.list_sessions():
            logger = SessionLogger(self, metadata)
            for match in _search_one_session(logger, needle):
                matches.append(match)
                if len(matches) >= limit:
                    return matches
        return matches

    def export_session(self, query: str) -> Path:
        metadata = self.resolve(query)
        session_dir = self.session_dir(metadata.session_id)
        exports_dir = self.sandbox.validate(session_dir / "exports")
        exports_dir.mkdir(parents=True, exist_ok=True)
        summary_path = exports_dir / "summary.md"
        summary_path.write_text(_summary_markdown(metadata), encoding="utf-8")
        zip_path = exports_dir / f"{metadata.session_id}.zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in ("session.json", "events.jsonl"):
                path = session_dir / name
                if path.exists():
                    archive.write(path, arcname=name)
            archive.write(summary_path, arcname="summary.md")
        return zip_path

    def session_dir(self, session_id: str) -> Path:
        return self.sandbox.validate(Path(".shamsu") / "sessions" / session_id)

    def _write_metadata(self, metadata: SessionMetadata) -> None:
        session_dir = self.session_dir(metadata.session_id)
        self.sandbox.validate(session_dir / "context").mkdir(parents=True, exist_ok=True)
        self.sandbox.validate(session_dir / "exports").mkdir(parents=True, exist_ok=True)
        self.sandbox.validate(session_dir / "session.json").write_text(
            json.dumps(asdict(metadata), indent=2),
            encoding="utf-8",
        )

    def _read_index(self) -> dict[str, list[dict]]:
        if not self.index_path.exists():
            return {"sessions": []}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _write_index(self, index: dict[str, list[dict]]) -> None:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    def _upsert_index(self, metadata: SessionMetadata) -> None:
        index = self._read_index()
        sessions = [item for item in index.get("sessions", []) if item["session_id"] != metadata.session_id]
        sessions.append(asdict(metadata))
        index["sessions"] = sorted(sessions, key=lambda item: item["updated_at"], reverse=True)
        self._write_index(index)


class SessionLogger:
    def __init__(self, manager: SessionManager, metadata: SessionMetadata):
        self.manager = manager
        self.metadata = metadata

    @property
    def session_id(self) -> str:
        return self.metadata.session_id

    @property
    def events_path(self) -> Path:
        return self.manager.sandbox.validate(
            Path(".shamsu") / "sessions" / self.session_id / "events.jsonl"
        )

    def log(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        summary: str = "",
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "timestamp": _now(),
            "session_id": self.session_id,
            "event_id": uuid.uuid4().hex[:12],
            "event_type": event_type,
            "workflow_id": workflow_id,
            "summary": redact(summary),
            "payload": sanitize_payload(payload or {}),
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True) + "\n")
        self.metadata.event_count += 1
        self.metadata.updated_at = event["timestamp"]
        if event_type == "user.prompt":
            self.metadata.last_user_prompt = str(event["payload"].get("prompt", ""))
        if event_type == "chat.message":
            self.metadata.message_count += 1
        self.manager._write_metadata(self.metadata)
        self.manager._upsert_index(self.metadata)
        return event

    def log_context_pack(self, pack: ContextPack, workflow_id: str | None = None) -> None:
        snippets = [
            {
                "file_path": item.file_path,
                "language": item.language,
                "line_start": item.line_start,
                "line_end": item.line_end,
                "score": item.score,
                "symbol_name": item.symbol_name,
                "chunk_type": item.chunk_type,
                "preview": item.content[:SNIPPET_PREVIEW_CHARS],
            }
            for item in pack.snippets
        ]
        self.log(
            "context.pack",
            {
                "task_id": pack.task_id,
                "step_id": pack.step_id,
                "specialist": pack.specialist,
                "token_estimate": pack.token_estimate,
                "snippets": snippets,
            },
            f"Packed {len(snippets)} snippets for {pack.specialist}",
            workflow_id=workflow_id or pack.task_id,
        )

    def tail(self, count: int = 20) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        lines = self.events_path.read_text(encoding="utf-8").splitlines()[-count:]
        return [json.loads(line) for line in lines if line.strip()]

    def _session_file(self, name: str) -> Path:
        return self.manager.sandbox.validate(
            Path(".shamsu") / "sessions" / self.session_id / name
        )

    # -- Clean chat transcript (messages.jsonl) -----------------------------

    @property
    def messages_path(self) -> Path:
        return self._session_file("messages.jsonl")

    def append_message(
        self,
        role: str,
        content: str,
        tool_call_id: str = "",
        name: str = "",
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Append one clean transcript turn. Kept separate from the richer
        `chat.message` event so hydration can prefer a compact, redacted
        transcript and moving fully off events.jsonl later is trivial."""
        record = {
            "timestamp": _now(),
            "role": role,
            "content": _clip(redact(str(content)), MESSAGE_PREVIEW_CHARS),
            "tool_call_id": tool_call_id,
            "name": name,
            "tool_calls": sanitize_payload(tool_calls or []),
        }
        path = self.messages_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
        return record

    def read_messages(self, count: int | None = None) -> list[dict[str, Any]]:
        path = self.messages_path
        if not path.exists():
            return []
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        if count is not None:
            lines = lines[-count:]
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    # -- Session state / pending action (state.json) ------------------------

    @property
    def state_path(self) -> Path:
        return self._session_file("state.json")

    def read_state(self) -> dict[str, Any]:
        path = self.state_path
        state = _default_state()
        if path.exists():
            try:
                stored = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                stored = {}
            if isinstance(stored, dict):
                state.update(stored)
        return state

    def write_state(self, state: dict[str, Any]) -> None:
        clean = sanitize_payload(dict(state or {}))
        clean["updated_at"] = _now()
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(clean, indent=2, ensure_ascii=True), encoding="utf-8")

    def set_pending_action(self, action: dict[str, Any]) -> None:
        state = self.read_state()
        state["pending_action"] = sanitize_payload(dict(action or {}))
        self.write_state(state)
        self.log(
            "session.pending_action.set",
            {"type": str((action or {}).get("type", "")), "awaiting": str((action or {}).get("awaiting", ""))},
            "Pending action stored",
            workflow_id="session",
        )

    def get_pending_action(self) -> dict[str, Any]:
        pending = self.read_state().get("pending_action")
        return pending if isinstance(pending, dict) else {}

    def clear_pending_action(self) -> None:
        state = self.read_state()
        had = bool(state.get("pending_action"))
        state["pending_action"] = {}
        self.write_state(state)
        if had:
            self.log("session.pending_action.cleared", {}, "Pending action cleared", workflow_id="session")

    def set_last_route(self, route: dict[str, Any]) -> None:
        state = self.read_state()
        state["last_route"] = sanitize_payload(dict(route or {}))
        self.write_state(state)
        self.log(
            "session.route.updated",
            {"route": str((route or {}).get("route") or (route or {}).get("action") or "")},
            "Route updated",
            workflow_id="session",
        )

    def get_last_route(self) -> dict[str, Any]:
        route = self.read_state().get("last_route")
        return route if isinstance(route, dict) else {}

    def set_last_tool_plan(self, plan: list[dict[str, Any]]) -> None:
        state = self.read_state()
        state["last_tool_plan"] = sanitize_payload(list(plan or []))
        self.write_state(state)

    def get_last_tool_plan(self) -> list[dict[str, Any]]:
        plan = self.read_state().get("last_tool_plan")
        return plan if isinstance(plan, list) else []

    def set_last_failure(self, command: str, errors: str, exit_code: int = 1) -> None:
        """Remember the most recent failing command and its error output so a
        later bug-fix request with no explicit target can reuse it (e.g. after a
        build fails, the user just says "fix it")."""
        state = self.read_state()
        state["last_failure"] = {
            "command": _clip(str(command or ""), 500),
            "errors": _clip(str(errors or ""), 6000),
            "exit_code": int(exit_code),
            "timestamp": _now(),
        }
        self.write_state(state)
        self.log(
            "session.failure.recorded",
            {"command": _clip(str(command or ""), 200), "exit_code": int(exit_code)},
            "Recorded last failing command",
            workflow_id="session",
        )

    def get_last_failure(self) -> dict[str, Any]:
        failure = self.read_state().get("last_failure")
        return failure if isinstance(failure, dict) else {}

    def clear_last_failure(self) -> None:
        state = self.read_state()
        if state.pop("last_failure", None) is not None:
            self.write_state(state)

    def set_last_user_intent(self, intent: str) -> None:
        state = self.read_state()
        state["last_user_intent"] = str(intent or "")
        self.write_state(state)

    def set_last_assistant_summary(self, summary: str) -> None:
        state = self.read_state()
        state["last_assistant_summary"] = _clip(str(summary or ""), 1200)
        self.write_state(state)

    # -- Session summary (summary.md) ---------------------------------------

    @property
    def summary_path(self) -> Path:
        return self._session_file("summary.md")

    def read_summary(self) -> str:
        path = self.summary_path
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def write_summary(self, text: str) -> None:
        path = self.summary_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        self.metadata.summary_updated_at = _now()
        self.metadata.updated_at = self.metadata.summary_updated_at
        self.manager._write_metadata(self.metadata)
        self.manager._upsert_index(self.metadata)

    def update_summary_from_events(self, max_events: int = 400) -> str:
        """Deterministic v1 summary (no LLM): latest goal, tools used, files
        changed, commands run, pending action, and the last assistant answer."""
        text = _build_summary_markdown(self, self.tail(max_events))
        self.write_summary(text)
        self.log(
            "session.summary.updated",
            {"chars": len(text)},
            "Session summary updated",
            workflow_id="session",
        )
        return text

    # -- Long-term / local memory (memory.jsonl + MemoryService) ------------

    @property
    def memory_path(self) -> Path:
        return self._session_file("memory.jsonl")

    def read_local_memory(self) -> list[dict[str, Any]]:
        path = self.memory_path
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return records

    def _local_memory_hashes(self) -> set[str]:
        return {str(record.get("hash", "")) for record in self.read_local_memory()}

    def append_local_memory(
        self,
        kind: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Append a durable local memory record, deduped by content hash within
        this session. Returns the record, or None if empty or a duplicate."""
        clean = redact(str(text)).strip()
        if not clean:
            return None
        digest = hashlib.sha1(f"{kind}:{_norm(clean)}".encode("utf-8")).hexdigest()[:16]
        if digest in self._local_memory_hashes():
            return None
        record = {
            "timestamp": _now(),
            "session_id": self.session_id,
            "kind": kind,
            "text": _clip(clean, MESSAGE_PREVIEW_CHARS),
            "hash": digest,
            "metadata": sanitize_payload(metadata or {}),
        }
        path = self.memory_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "\n")
        self.log("memory.local.appended", {"kind": kind}, f"Local memory appended: {kind}", workflow_id="memory")
        return record

    def save_long_term_memory(
        self,
        kind: str,
        text: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Always record locally (memory.jsonl); best-effort mirror to the
        existing MemoryService. A missing/failed memory backend never raises."""
        local = self.append_local_memory(kind, text, metadata)
        result: dict[str, Any] = {"local": local is not None}
        clean = redact(str(text)).strip()
        if not clean:
            return result
        full_metadata = {
            "session_id": self.session_id,
            "session_title": self.metadata.title,
            "workspace": self.metadata.workspace,
            "kind": kind,
            "source": "session_manager",
            **(metadata or {}),
        }
        try:
            from shamsu.memory.service import MemoryService

            outcome = MemoryService(self.manager.workspace).remember(
                clean, _map_memory_kind(kind), full_metadata
            )
        except Exception as exc:
            self.log(
                "memory.long_term.failed",
                {"kind": kind, "error": str(exc)},
                "Long-term memory write failed",
                workflow_id="memory",
            )
            result["error"] = str(exc)
            return result
        result["long_term"] = outcome
        if outcome.get("ok"):
            self.log(
                "memory.long_term.saved",
                {"kind": kind, "deduped": bool(outcome.get("deduped"))},
                "Long-term memory saved",
                workflow_id="memory",
            )
        else:
            self.log(
                "memory.long_term.skipped",
                {"kind": kind, "reason": str(outcome.get("reason") or outcome.get("error") or "not stored")},
                "Long-term memory skipped",
                workflow_id="memory",
            )
        return result


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, ContextPack):
        return sanitize_payload(value.to_dict())
    if isinstance(value, dict):
        return {str(key): sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return _sanitize_scalar(value)
    try:
        return sanitize_payload(asdict(value))
    except TypeError:
        return _truncate(redact(str(value)))


def _sanitize_scalar(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate(redact(value))
    return value


def _truncate(text: str) -> str:
    if len(text) <= MAX_STRING_CHARS:
        return text
    return f"{text[:MAX_STRING_CHARS]}... [truncated {len(text) - MAX_STRING_CHARS} chars]"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _default_state() -> dict[str, Any]:
    return {
        "pending_action": {},
        "last_route": {},
        "last_tool_plan": [],
        "last_user_intent": "",
        "last_assistant_summary": "",
        "updated_at": "",
    }


def _is_placeholder_title(title: str) -> bool:
    """True for still-unnamed sessions, including deduped placeholders like
    `Untitled Session (2)`, so the first prompt can auto-title over them."""
    base = re.sub(r"\s*\(\d+\)$", "", (title or "").strip())
    return base in PLACEHOLDER_TITLES


# -- Deterministic title generation ----------------------------------------

# Full-prompt greetings/acknowledgements/affirmatives that must never become a
# title (matched against the alnum-normalized prompt).
_TRIVIAL_PROMPTS = {
    "hi", "hii", "hiya", "hello", "hello there", "hey", "hey there", "yo",
    "sup", "wassup", "whatsup", "gm", "good morning", "good afternoon",
    "good evening", "thanks", "thank you", "thx", "ty", "ok", "okay", "kk",
    "cool", "nice", "great", "yes", "yep", "yeah", "yup", "sure", "no", "nope",
    "do it", "go ahead", "continue", "proceed", "please", "test", "ping",
}
# Removed only when they lead the prompt (low-value question/aux openers) so a
# real verb like "show"/"build"/"fix" still starts the title.
_LEADING_SKIP = {
    "what", "whats", "whatis", "are", "is", "was", "were", "the", "a", "an",
    "how", "why", "do", "does", "did", "this", "these", "those", "here",
    "there", "please", "kindly", "pls",
}
# Dropped anywhere (determiners/fillers) without ending the title.
_SKIP_WORDS = {"the", "a", "an", "this", "these", "those", "my", "your", "our", "its", "here", "there", "please", "kindly", "pls"}
# End the head clause (conjunctions / subordinators).
_STOP_WORDS = {"so", "then", "because", "which", "when", "while", "after", "before", "but", "if", "however", "since"}
# Leading filler phrases stripped before tokenizing.
_LEADING_PHRASES = (
    "can you please", "could you please", "would you please",
    "can you", "could you", "would you", "will you",
    "help me to", "help me", "i want you to", "i want to", "i would like to",
    "i'd like to", "i need you to", "i need to", "please help me", "let's", "lets",
)
_ACRONYMS = {
    "prd", "api", "cli", "url", "ui", "ux", "id", "sql", "http", "https",
    "json", "html", "css", "db", "jwt", "csv", "yaml", "toml", "sdk", "orm",
}
_TITLE_MAX_WORDS = 7
_TITLE_MAX_CHARS = 60


def generate_title_from_prompt(prompt: str) -> str:
    text = (prompt or "").strip()
    if not text:
        return DEFAULT_TITLE
    normalized = re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if normalized in _TRIVIAL_PROMPTS:
        return DEFAULT_TITLE
    # Strip a leading filler phrase.
    working = text.lower()
    for phrase in _LEADING_PHRASES:
        if working.startswith(phrase + " "):
            text = text[len(phrase):].strip()
            break
    # Path separators become word breaks; drop other weird characters.
    prepared = re.sub(r"[\\/|]+", " ", text)
    tokens = prepared.split()
    words: list[str] = []
    started = False
    for raw in tokens:
        cleaned = re.sub(r"^[^0-9A-Za-z]+|[^0-9A-Za-z]+$", "", raw)
        boundary = bool(re.search(r"[,;.!?]$", raw))
        low = cleaned.lower()
        if not cleaned:
            if boundary and words:
                break
            continue
        if not started and low in _LEADING_SKIP:
            continue
        if low in _STOP_WORDS:
            break
        if low in _SKIP_WORDS:
            if boundary and words:
                break
            continue
        started = True
        words.append(cleaned)
        if boundary or len(words) >= _TITLE_MAX_WORDS:
            break
    if not words:
        return DEFAULT_TITLE
    title = _titlecase_words(words)
    while len(title) > _TITLE_MAX_CHARS and len(words) > 1:
        words.pop()
        title = _titlecase_words(words)
    return title or DEFAULT_TITLE


def _titlecase_words(words: list[str]) -> str:
    out: list[str] = []
    for word in words:
        low = word.lower()
        if low in _ACRONYMS:
            out.append(low.upper())
        else:
            out.append(low[:1].upper() + low[1:])
    return " ".join(out)


# -- Local memory kind mapping ---------------------------------------------

# Local memory kinds are broader than the durable MemoryService MemoryKind
# literals; map them onto the closest valid long-term kind.
_MEMORY_KIND_MAP = {
    "session_summary": "task_summary",
    "task_summary": "task_summary",
    "user_preference": "user_preference",
    "bug_lesson": "bug_lesson",
    "project_decision": "project_decision",
    "tooling_lesson": "architecture_note",
}


def _map_memory_kind(kind: str) -> str:
    return _MEMORY_KIND_MAP.get(kind, "task_summary")


# -- Deterministic session summary + search --------------------------------

def _build_summary_markdown(logger: "SessionLogger", events: list[dict[str, Any]]) -> str:
    metadata = logger.metadata
    tools_used: list[str] = []
    files_changed: list[str] = []
    commands: list[str] = []
    workflows: list[str] = []
    last_assistant = ""
    for event in events:
        event_type = event.get("event_type", "")
        payload = event.get("payload", {}) or {}
        if event_type == "agent.tool_call":
            name = str(payload.get("tool_name", ""))
            if name and name not in tools_used:
                tools_used.append(name)
            arguments = payload.get("arguments", {}) or {}
            if name == "write_file":
                filepath = str(arguments.get("filepath", "")).strip()
                if filepath and filepath not in files_changed:
                    files_changed.append(filepath)
            if name == "run_command":
                command = str(arguments.get("command", "")).strip()
                if command:
                    commands.append(command)
        elif event_type in {"patch.applied"}:
            for item in payload.get("touched_files", []) or []:
                if str(item) not in files_changed:
                    files_changed.append(str(item))
        elif event_type == "workflow.finished":
            summary = str(event.get("summary", "")).strip()
            if summary:
                workflows.append(summary)
        elif event_type == "assistant.message":
            message = str(payload.get("message", "")).strip()
            if message:
                last_assistant = message
        elif event_type == "chat.message" and str(payload.get("role", "")) == "assistant":
            content = str(payload.get("content", "")).strip()
            if content:
                last_assistant = content

    state = logger.read_state()
    pending = state.get("pending_action") or {}
    if not last_assistant:
        last_assistant = str(state.get("last_assistant_summary", "")).strip()

    def bullets(items: list[str], template: str = "- {}") -> list[str]:
        return [template.format(item) for item in items] if items else ["- none recorded"]

    lines = [
        f"# Session Summary — {metadata.title}",
        "",
        f"- Session: {metadata.session_id}",
        f"- Status: {metadata.status}",
        f"- Messages: {metadata.message_count}",
        f"- Updated: {metadata.updated_at}",
        "",
        "## Latest goal",
        metadata.last_user_prompt.strip() or "-",
        "",
        "## Recent workflows",
        *bullets(workflows[-5:]),
        "",
        "## Files changed",
        *bullets(files_changed[-15:]),
        "",
        "## Tools used",
        ("- " + ", ".join(tools_used)) if tools_used else "- none recorded",
        "",
        "## Commands run",
        *bullets(commands[-8:], "- `{}`"),
        "",
        "## Pending action",
        (f"- {pending.get('type', 'action')} (awaiting: {pending.get('awaiting', '-')})" if pending else "- none"),
        "",
        "## Last assistant answer",
        _clip(last_assistant, 800) or "-",
        "",
    ]
    return "\n".join(lines)


def _search_one_session(logger: "SessionLogger", needle: str) -> list[dict[str, Any]]:
    metadata = logger.metadata
    results: list[dict[str, Any]] = []

    def add(source: str, role: str, snippet: str) -> None:
        results.append(
            {
                "session_id": metadata.session_id,
                "title": metadata.title,
                "timestamp": metadata.updated_at,
                "source": source,
                "role": role,
                "snippet": _snippet(snippet, needle),
            }
        )

    if needle in metadata.title.lower():
        add("title", "-", metadata.title)
    summary = logger.read_summary()
    if summary and needle in summary.lower():
        add("summary", "-", summary)
    # Prefer the clean transcript; fall back to chat.message / legacy events.
    messages = logger.read_messages()
    if messages:
        for record in messages:
            role = str(record.get("role", ""))
            if role == "tool":
                continue
            content = str(record.get("content", ""))
            if needle in content.lower():
                add("message", role, content)
    else:
        for event in logger.tail(400):
            event_type = event.get("event_type", "")
            payload = event.get("payload", {}) or {}
            if event_type == "chat.message" and str(payload.get("role", "")) in {"user", "assistant"}:
                content = str(payload.get("content", ""))
                if needle in content.lower():
                    add("message", str(payload.get("role", "")), content)
            elif event_type == "user.prompt":
                content = str(payload.get("prompt", ""))
                if needle in content.lower():
                    add("message", "user", content)
            elif event_type == "assistant.message":
                content = str(payload.get("message", ""))
                if needle in content.lower():
                    add("message", "assistant", content)
    for record in logger.read_local_memory():
        content = str(record.get("text", ""))
        if needle in content.lower():
            add("memory", str(record.get("kind", "")), content)
    return results


def _snippet(text: str, needle: str, width: int = 80) -> str:
    flat = " ".join(text.split())
    index = flat.lower().find(needle)
    if index == -1:
        return _clip(flat, width * 2)
    start = max(index - width, 0)
    end = min(index + len(needle) + width, len(flat))
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(flat) else ""
    return f"{prefix}{flat[start:end]}{suffix}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_session_id() -> str:
    compact = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{compact}-{uuid.uuid4().hex[:4]}"


def _summary_markdown(metadata: SessionMetadata) -> str:
    return "\n".join(
        [
            f"# SHAMSU Session {metadata.session_id}",
            "",
            f"- Title: {metadata.title}",
            f"- Workspace: {metadata.workspace}",
            f"- Created: {metadata.created_at}",
            f"- Updated: {metadata.updated_at}",
            f"- Status: {metadata.status}",
            f"- Events: {metadata.event_count}",
            f"- Last prompt: {metadata.last_user_prompt or '-'}",
            "",
            "See `events.jsonl` for the redacted event timeline.",
            "",
        ]
    )
