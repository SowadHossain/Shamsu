"""What the portal serves, as plain functions over plain dicts.

Deliberately free of HTTP: every rule that decides what a caller may see lives
here, so it can be tested without a socket and reused by whatever transport
comes next. `server.py` does routing, auth and framing, and nothing else.

Two invariants worth naming, because both have teeth:

- **Ids, never paths.** A workspace id is a handle into a list the portal
  already knows; a session id is resolved by the `SessionManager`. Nothing here
  accepts a filesystem path from a caller, so there is no traversal to defend
  against rather than a defence to get right.
- **Redact on the way out.** The same `redact()` the Telegram transport uses.
  The transcript on disk is lossless on purpose; what leaves the process is not.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from shamsu.runtime.turn_stream import TurnStream, activity_path
from shamsu.safety.commands import redact
from shamsu.session.manager import SessionManager

#: Enough of the digest to be unambiguous across a handful of projects, short
#: enough to sit in a URL without looking like a hash of something secret.
WORKSPACE_ID_CHARS = 12

MAX_MESSAGES = 2000


class NotFound(LookupError):
    """A caller asked for something that is not theirs to see, or is not there.

    One exception for both cases on purpose: "no such session" and "not in a
    workspace this portal knows" should be indistinguishable from outside.
    """


def workspace_id(workspace: Path | str) -> str:
    resolved = str(Path(workspace).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:WORKSPACE_ID_CHARS]


def workspace_for_id(candidate: str, workspaces: list[Path]) -> Path:
    for workspace in workspaces:
        if workspace_id(workspace) == candidate:
            return Path(workspace).resolve()
    raise NotFound(f"unknown workspace: {candidate}")


def workspace_summary(workspace: Path) -> dict[str, Any]:
    resolved = Path(workspace).resolve()
    try:
        sessions = SessionManager(resolved).list_sessions()
    except Exception:  # noqa: BLE001 - an unreadable project is listed, not fatal
        sessions = []
    return {
        "id": workspace_id(resolved),
        "name": resolved.name,
        "path": str(resolved),
        "session_count": len(sessions),
        "last_activity": sessions[0].updated_at if sessions else "",
    }


def workspaces_payload(workspaces: list[Path]) -> dict[str, Any]:
    """Most recently worked-in first.

    Sorted here rather than in the browser so every surface agrees on the
    order, and because the client would otherwise need every workspace's
    sessions loaded just to know how to sort the headings - which is exactly
    the fetch the collapsed sidebar exists to avoid.

    A workspace with no threads has no activity to sort by, so it sinks to the
    bottom instead of floating on an empty string.
    """
    summaries = [workspace_summary(item) for item in workspaces]
    summaries.sort(key=lambda item: (item["last_activity"] or "", item["name"]), reverse=True)
    return {"workspaces": summaries}


def sessions_payload(workspace: Path) -> dict[str, Any]:
    sessions = SessionManager(Path(workspace).resolve()).list_sessions()
    return {
        "workspace": workspace_summary(workspace),
        "sessions": [
            {
                "session_id": item.session_id,
                "title": item.title,
                "status": item.status,
                "updated_at": item.updated_at,
                "message_count": item.message_count,
                "last_user_prompt": redact(item.last_user_prompt or ""),
            }
            for item in sessions
        ],
    }


def session_messages(workspace: Path, session_id: str, after: int = 0) -> dict[str, Any]:
    """The transcript, from `messages.jsonl`, redacted, sliced from `after`.

    `after` is an INDEX, not a byte offset: the browser already holds the first
    N and wants the rest, and an index survives the file being rewritten by
    something else in a way an offset would not.
    """
    manager = SessionManager(Path(workspace).resolve())
    try:
        logger = manager.logger_for(session_id)
    except Exception as exc:  # noqa: BLE001 - resolve() raises ValueError
        raise NotFound(f"unknown session: {session_id}") from exc
    messages = logger.read_messages()
    start = max(0, int(after))
    sliced = messages[start : start + MAX_MESSAGES]
    return {
        "session_id": logger.session_id,
        "title": logger.metadata.title,
        "total": len(messages),
        "after": start,
        "messages": [
            {
                "role": str(item.get("role") or ""),
                "content": redact(str(item.get("content") or "")),
                "timestamp": str(item.get("timestamp") or ""),
                "name": str(item.get("name") or ""),
            }
            for item in sliced
        ],
    }


def session_activity(
    workspace: Path, session_id: str, *, since_seq: int = -1, turn_id: str = ""
) -> dict[str, Any]:
    """A turn's event stream, replayed from `activity.jsonl`.

    The same file the CLI and the Telegram card render from - which is the whole
    point of the turn stream. Three surfaces, one record, no chance of them
    quietly disagreeing about what happened.
    """
    resolved = _resolved_session(workspace, session_id)
    stream = TurnStream(workspace, resolved, persist=False)
    events = stream.replay(since_seq)
    if turn_id:
        events = [event for event in events if event.turn_id == turn_id]
    return {
        "session_id": resolved,
        "events": [_event_payload(event) for event in events],
    }


def _event_payload(event: Any) -> dict[str, Any]:
    payload = event.to_dict()
    payload["text"] = redact(str(payload.get("text") or ""))
    return payload


def _resolved_session(workspace: Path, session_id: str) -> str:
    manager = SessionManager(Path(workspace).resolve())
    try:
        return manager.resolve(session_id).session_id
    except Exception as exc:  # noqa: BLE001
        raise NotFound(f"unknown session: {session_id}") from exc


def activity_file(workspace: Path, session_id: str) -> Path:
    return activity_path(workspace, _resolved_session(workspace, session_id))


def queue_payload(store: Any, workspace: Path, session_id: str) -> dict[str, Any]:
    """What is waiting on this thread, and who is running it."""
    holder = store.lease_holder(workspace, session_id)
    return {
        "running_on": holder.owner_surface if holder is not None else "",
        "queued": [
            {
                "queue_id": item.queue_id,
                "source": item.source,
                "text": redact(item.text),
                "created_at": item.created_at,
            }
            for item in store.pending(workspace, session_id)
        ],
    }


def approvals_payload(store: Any, workspace: Path | None = None, session_id: str = "") -> dict[str, Any]:
    """Pending approvals - install-wide by default.

    Install-wide because the point of answering from anywhere is that you do
    not have to already be looking at the right project to be asked.
    """
    return {
        "approvals": [
            {
                "approval_id": item.approval_id,
                "workspace": item.workspace,
                "session_id": item.session_id,
                "action_type": item.action_type,
                "description": item.description,
                "risk_level": item.risk_level,
                "preview": item.preview,
                "created_at": item.created_at,
            }
            for item in store.pending_approvals(workspace, session_id)
        ]
    }


def health_payload(workspace: Path | None) -> dict[str, Any]:
    from shamsu.runtime.models import model_for_role

    try:
        model = model_for_role("agent-chat")
    except Exception:  # noqa: BLE001 - a missing cookbook is not a dead portal
        model = ""
    return {
        "ok": True,
        "workspace": str(Path(workspace).resolve()) if workspace else "",
        "model": model,
        "read_only": False,
    }


#: What the composer offers after a "/". Each is handled by the portal itself,
#: not sent to the model - the same split the REPL makes, so the two surfaces
#: mean the same thing by the same word.
WEB_COMMANDS: tuple[dict[str, str], ...] = (
    {"name": "/new", "args": "[title]", "help": "Start a new thread here"},
    {"name": "/settings", "args": "", "help": "Model, context window, tools, Telegram"},
    {"name": "/workspace", "args": "<path>", "help": "Add a workspace by path"},
    {"name": "/threads", "args": "", "help": "Focus the thread filter"},
    {"name": "/queue", "args": "", "help": "Show what is waiting on this thread"},
    {"name": "/approvals", "args": "", "help": "Jump to anything awaiting approval"},
    {"name": "/help", "args": "", "help": "List these commands"},
)


def commands_payload() -> dict[str, Any]:
    return {"commands": [dict(item) for item in WEB_COMMANDS]}


def settings_payload() -> dict[str, Any]:
    """Everything the settings panel shows, gathered from where it really lives.

    Deliberately assembled rather than cached: a context window changed in the
    terminal must show here on the next open, and a cached copy would be the
    same class of bug as the workspace-scoped bot token.
    """
    from shamsu.agents.simple_chat import CTX_BUCKETS, SESSION_COUNTERS, SIMPLE_TOOLS, max_ctx
    from shamsu.runtime.models import model_for_role
    from shamsu.runtime.settings import load_settings

    try:
        model = model_for_role("agent-chat")
    except Exception:  # noqa: BLE001
        model = ""
    return {
        "model": model,
        "context": {
            "max_ctx": max_ctx(),
            "buckets": list(CTX_BUCKETS),
            "saved": load_settings().get("chat_max_ctx"),
            "env_override": bool(_env("SHAMSU_CHAT_MAX_CTX")),
            "last_prompt_tokens": SESSION_COUNTERS.last_prompt_tokens,
            "last_window": SESSION_COUNTERS.last_window,
            "pct": SESSION_COUNTERS.pct,
        },
        "tools": sorted(SIMPLE_TOOLS),
        "telegram": telegram_status(),
    }


def _env(name: str) -> str:
    import os

    return os.environ.get(name, "").strip()


def telegram_status() -> dict[str, Any]:
    """Whether the bot is configured, and where its token came from.

    The token itself is never included, at any level of detail. That rule
    predates this panel and is not relaxed for it.
    """
    from shamsu.integrations.telegram.service import load_telegram_bot_token

    try:
        token, source = load_telegram_bot_token(Path.cwd())
    except Exception:  # noqa: BLE001
        token, source = "", "missing"
    return {"configured": bool(token), "source": source if token else "missing"}


def create_session(workspace: Path, title: str = "") -> dict[str, Any]:
    from shamsu.session.manager import SessionManager

    logger = SessionManager(Path(workspace).resolve()).create_session(title or None)
    return {
        "session_id": logger.session_id,
        "title": logger.metadata.title,
        "status": logger.metadata.status,
        "updated_at": logger.metadata.updated_at,
        "message_count": 0,
        "last_user_prompt": "",
    }


class BadPath(ValueError):
    """A path a caller offered that cannot be made into a workspace."""


def add_workspace(raw_path: str) -> dict[str, Any]:
    """Register a directory so it appears in the sidebar.

    The one place the portal accepts a filesystem path from a caller, and it is
    checked rather than trusted: it must already exist and be a directory. A
    path that does not exist is a typo, and silently creating a workspace from
    one would put an empty project in the list that nobody meant.
    """
    from shamsu.runtime.workspaces import remember_workspace

    candidate = str(raw_path or "").strip().strip('"').strip("'")
    if not candidate:
        raise BadPath("give a folder path")
    try:
        resolved = Path(candidate).expanduser().resolve()
    except (OSError, ValueError) as exc:
        raise BadPath(f"not a usable path: {candidate}") from exc
    if not resolved.exists():
        raise BadPath(f"no such folder: {resolved}")
    if not resolved.is_dir():
        raise BadPath(f"not a folder: {resolved}")
    remember_workspace(resolved)
    return workspace_summary(resolved)


def dumps(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True).encode("utf-8")
