"""Workspace-local session storage and structured event logging."""
from __future__ import annotations

import json
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.safety.commands import redact
from shamsu.safety.sandbox import Sandbox
from shamsu.types import ContextPack

SNIPPET_PREVIEW_CHARS = 600
MAX_STRING_CHARS = 4000


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
            title=title or "SHAMSU Session",
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
        sessions = [SessionMetadata(**item) for item in index.get("sessions", [])]
        return sorted(sessions, key=lambda item: item.updated_at, reverse=True)

    def close_session(self, query: str) -> SessionMetadata:
        metadata = self.resolve(query)
        logger = SessionLogger(self, metadata)
        logger.log("session.closed", {}, "Session closed")
        metadata = logger.metadata
        metadata.status = "closed"
        metadata.updated_at = _now()
        self._write_metadata(metadata)
        self._upsert_index(metadata)
        return metadata

    def rename_session(self, query: str, title: str) -> SessionMetadata:
        metadata = self.resolve(query)
        metadata.title = title
        metadata.updated_at = _now()
        self._write_metadata(metadata)
        self._upsert_index(metadata)
        return metadata

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
