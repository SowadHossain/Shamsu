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
from shamsu.session.manager import ORIGIN_LOOP, SessionManager

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


def session_exists(workspace: Path, session_id: str) -> bool:
    """Whether this workspace really has that thread.

    Cheap and total: `SessionManager.resolve` raises for an unknown id, and
    everything else here treats an unreadable session as absent rather than as
    a reason to fail. Used before work is queued, so a prompt for a thread that
    does not exist is refused where it is sent instead of disappearing.
    """
    if not str(session_id or "").strip():
        return False
    try:
        SessionManager(Path(workspace).resolve()).logger_for(session_id)
    except Exception:  # noqa: BLE001
        return False
    return True


def session_messages(workspace: Path, session_id: str, after: int = 0) -> dict[str, Any]:
    """The conversation, as a person had it.

    Built from the **turn stream** when there is one, and only from
    `messages.jsonl` when there is not. That inversion is the fix for a real
    complaint: `messages.jsonl` is the model's context, not the conversation,
    and rendering it as chat produced a transcript nobody recognised.

    Measured on a live session (2026-08-20, two questions asked): the file held
    89 records, of which the browser drew 50 bubbles - 12 of them attributed to
    the user, when the user had typed exactly 2. The other ten were loop
    nudges - "You have already called read_file(js/game.js) this turn" - which
    are written with `role: user` because that is the role the MODEL must see
    them in. Four more bubbles were empty, being assistant turns that carried
    only tool calls.

    `activity.jsonl` already had it right: two `turn.start` events, two
    `assistant` events, and the correct source on each - one `telegram`, one
    `cli`. The terminal and the phone both render from that stream. The browser
    was the only surface reading the other file, which is exactly why it was
    the only one showing the wrong conversation.

    `after` is an INDEX, not a byte offset: the browser already holds the first
    N and wants the rest, and an index survives the file being rewritten by
    something else in a way an offset would not.
    """
    manager = SessionManager(Path(workspace).resolve())
    try:
        logger = manager.logger_for(session_id)
    except Exception as exc:  # noqa: BLE001 - resolve() raises ValueError
        raise NotFound(f"unknown session: {session_id}") from exc

    rows = _conversation_from_turns(workspace, logger.session_id)
    built_from = "turns"
    if rows is None:
        rows = _conversation_from_transcript(logger.read_messages())
        built_from = "transcript"

    start = max(0, int(after))
    return {
        "session_id": logger.session_id,
        "title": logger.metadata.title,
        "total": len(rows),
        "after": start,
        "built_from": built_from,
        "messages": rows[start : start + MAX_MESSAGES],
    }


#: Event kinds that describe the CONVERSATION rather than the working. Status
#: ticks, tool calls and activity lines belong to the turn log, which the
#: browser renders separately and which must not become chat bubbles.
_CONVERSATION_KINDS = ("turn.start", "assistant", "turn.end")


def _conversation_from_turns(workspace: Path, session_id: str) -> list[dict[str, Any]] | None:
    """Prompts and answers, in order, from `activity.jsonl`.

    Returns None when this session has no turn stream to read - an older
    session, or one written before the stream existed - so the caller can fall
    back rather than show an empty thread.
    """
    try:
        events = TurnStream(workspace, session_id, persist=False).replay(-1)
    except Exception:  # noqa: BLE001 - an unreadable log costs the nicer
        # rendering, never the thread.
        return None

    turns: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for event in events:
        if event.kind not in _CONVERSATION_KINDS:
            continue
        turn = turns.get(event.turn_id)
        if turn is None:
            # All three start as None, not "": these hold EVENTS, and an empty
            # string here read as "present" against `is not None`.
            turn = {"source": event.source, "prompt": None, "answer": None, "verdict": None}
            turns[event.turn_id] = turn
            order.append(event.turn_id)
        if event.kind == "turn.start":
            turn["prompt"] = event
            turn["source"] = event.source or turn["source"]
        elif event.kind == "assistant":
            turn["answer"] = event
        else:
            turn["verdict"] = event

    if not any(turns[turn_id]["prompt"] is not None for turn_id in order):
        return None

    rows: list[dict[str, Any]] = []
    for turn_id in order:
        turn = turns[turn_id]
        source = str(turn["source"] or "")
        prompt = turn["prompt"]
        if prompt is not None and prompt.text:
            rows.append(_row("user", prompt, source, turn_id))
        answer = turn["answer"]
        end = turn["verdict"]
        if answer is not None and answer.text:
            row = _row("assistant", answer, source, turn_id)
            if end is not None:
                # What the turn cost, on the answer it produced. Without it a
                # three-minute run that changed nothing reads as a quick reply.
                row["verdict"] = redact(str(end.text or ""))
                row["changed_files"] = list((end.data or {}).get("changed_files") or [])
            rows.append(row)
        elif end is not None and end.text:
            # A turn that ended without an answer - stopped, failed, cancelled.
            # Shown, so a thread never looks like it swallowed your question.
            row = _row("assistant", end, source, turn_id)
            row["content"] = ""
            row["verdict"] = redact(str(end.text or ""))
            rows.append(row)
    return rows


def _row(role: str, event: Any, source: str, turn_id: str) -> dict[str, Any]:
    return {
        "role": role,
        "content": redact(str(event.text or "")),
        "timestamp": _iso(getattr(event, "ts", 0.0)),
        "name": "",
        "source": source,
        "turn_id": turn_id,
        "verdict": "",
        "changed_files": [],
    }


def _iso(ts: float) -> str:
    from datetime import datetime, timezone

    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return ""


def _conversation_from_transcript(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The fallback, for a session with no turn stream.

    Still filtered, because the two things that made the raw file unreadable as
    chat are visible here too: a `user` record the loop wrote to steer the
    model, and an `assistant` record that is empty because it carried only tool
    calls. `origin` marks the first kind going forward; anything unmarked is
    SHOWN, so a missing marker can only ever leak a message in, never hide one.
    """
    rows: list[dict[str, Any]] = []
    for item in messages:
        role = str(item.get("role") or "")
        if role not in ("user", "assistant"):
            continue
        content = str(item.get("content") or "")
        if not content.strip():
            continue
        if role == "user" and str(item.get("origin") or "") == ORIGIN_LOOP:
            continue
        rows.append(
            {
                "role": role,
                "content": redact(content),
                "timestamp": str(item.get("timestamp") or ""),
                "name": str(item.get("name") or ""),
                # Empty for anything written before the field existed - the
                # browser shows no badge rather than guessing.
                "source": str(item.get("source") or ""),
                "turn_id": "",
                "verdict": "",
                "changed_files": [],
            }
        )
    return rows


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
    {"name": "/settings", "args": "", "help": "Model, context, Telegram, Ollama, locks"},
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

    Deliberately CHEAP, too. Anything that costs a round trip - the Ollama tag
    list, the Telegram probe - lives on its own route, so the drawer opens
    instantly and fills in rather than hanging on a server that is down.
    """
    from shamsu.agents.simple_chat import CTX_BUCKETS, SESSION_COUNTERS, SIMPLE_TOOLS, max_ctx
    from shamsu.runtime.models import model_source
    from shamsu.runtime.settings import (
        VERBOSITY_LEVELS,
        load_settings,
        verbosity,
    )
    from shamsu.runtime.turn_stream import body_kinds

    try:
        source, model = model_source("agent-chat")
    except Exception:  # noqa: BLE001
        source, model = "tier", ""
    level = verbosity()
    return {
        "model": model,
        "model_source": source,
        "context": {
            "max_ctx": max_ctx(),
            "buckets": list(CTX_BUCKETS),
            "saved": load_settings().get("chat_max_ctx"),
            "env_override": bool(_env("SHAMSU_CHAT_MAX_CTX")),
            "last_prompt_tokens": SESSION_COUNTERS.last_prompt_tokens,
            "last_window": SESSION_COUNTERS.last_window,
            "pct": SESSION_COUNTERS.pct,
        },
        "verbosity": {
            "level": level,
            "levels": list(VERBOSITY_LEVELS),
            # Served rather than reimplemented in JS, so the browser filters by
            # the same rule the terminal and the phone do.
            "body_kinds": sorted(body_kinds(level)),
        },
        "tools": sorted(SIMPLE_TOOLS),
        "telegram": telegram_status(),
    }


#: Where an effective model came from, in words the panel can print. A workspace
#: pin outranks the install-wide choice, and a page that let you pick a model
#: and then silently did nothing would be worse than offering no picker at all.
MODEL_SOURCE_LABELS = {
    "env": "pinned by SHAMSU_MODEL in the environment",
    "workspace": "pinned by this workspace",
    "install": "chosen here, for every project",
    "tier": "the hardware tier's default",
}


def models_payload(workspace: Path | None = None) -> dict[str, Any]:
    """What can be run, what is running, and what is currently eating the GPU.

    Kept off `/api/settings` because every field costs a request to Ollama and
    a settings drawer must not wait on a model server to draw itself.
    """
    from shamsu.runtime.models import (
        allowed_model_names,
        is_allowed_model,
        model_is_reasoning,
        model_source,
        model_supports_native_tools,
    )
    from shamsu.runtime.ollama import (
        OLLAMA_BASE_URL,
        is_ollama_running,
        list_available_models,
        list_loaded_models,
    )

    running = is_ollama_running()
    available = list_available_models() if running else []
    loaded = list_loaded_models() if running else []
    try:
        source, effective = model_source("agent-chat")
    except Exception:  # noqa: BLE001
        source, effective = "tier", ""

    # Anything pulled is offerable. A model outside the cookbook still gets the
    # right treatment - `model_supports_native_tools` and `model_is_reasoning`
    # fall back to family-name matching - so refusing it would be a restriction
    # with nothing behind it. It is marked, not blocked.
    names = sorted(set(available) | set(allowed_model_names()) | ({effective} if effective else set()))
    return {
        "server_running": running,
        "base_url": OLLAMA_BASE_URL,
        "effective": effective,
        "source": source,
        "source_label": MODEL_SOURCE_LABELS.get(source, source),
        "workspace_pin_shadows": source == "workspace",
        "loaded": loaded,
        "models": [
            {
                "name": name,
                "installed": name in available,
                "known": is_allowed_model(name),
                "reasoning": model_is_reasoning(name),
                "native_tools": model_supports_native_tools(name),
                "loaded": name in loaded,
            }
            for name in names
        ],
    }


def unload_our_models() -> list[str]:
    """Free the GPU of what SHAMSU put there, and nothing else.

    `unload_shamsu_models()` only knows the cookbook, which was right when the
    cookbook was the only way to choose a model. Now that any pulled model can
    be selected, the model this install is actually configured to use may not be
    in it - and a button labelled "Unload models" that leaves the one model you
    are running loaded is worse than no button.

    Still narrow, deliberately: the configured model is ours, an unrelated model
    somebody else loaded is not, and this must never evict theirs.
    """
    from shamsu.runtime.models import model_for_role
    from shamsu.runtime.ollama import list_loaded_models, unload_model, unload_shamsu_models

    unloaded = list(unload_shamsu_models())
    try:
        effective = model_for_role("agent-chat")
    except Exception:  # noqa: BLE001
        return unloaded
    if effective and effective not in unloaded and effective in list_loaded_models():
        if unload_model(effective):
            unloaded.append(effective)
    return unloaded


def locks_payload(store: Any) -> dict[str, Any]:
    """Who holds the machine's run slot, and every thread being run right now.

    The answer to "why has my prompt been queued for ten minutes": usually a
    process that died holding a lease. Stale ones are reclaimable; live ones
    are not, because being un-stealable is the entire point of a lease.
    """
    from shamsu.control.store import MACHINE_LEASE_KEY

    leases = store.active_leases()
    return {
        "leases": [
            {
                "workspace": lease.workspace,
                "session_id": lease.session_id,
                "owner_pid": lease.owner_pid,
                "owner_surface": lease.owner_surface,
                "heartbeat": lease.heartbeat,
                "is_machine_slot": lease.workspace == MACHINE_LEASE_KEY,
                "is_mine": lease.is_mine,
            }
            for lease in leases
        ],
        "machine_slot_held": any(
            lease.workspace == MACHINE_LEASE_KEY and lease.session_id == MACHINE_LEASE_KEY
            for lease in leases
        ),
    }


def telegram_payload(workspace: Path | None = None) -> dict[str, Any]:
    """The full Telegram panel: token, transport, poller, pairings, counters."""
    from shamsu.integrations.telegram.service import poller_status

    status = poller_status(workspace)
    status["pairings"] = pairings_payload(workspace)["pairings"]
    return {"telegram": status}


def pairings_payload(workspace: Path | None = None) -> dict[str, Any]:
    """Paired devices, and what each is currently pointed at.

    Read from the install-scoped state DB, so this is the same list whichever
    project happens to be open - which is also why rebinding the bot to another
    workspace does not unpair anybody.
    """
    from shamsu.integrations.telegram.storage import TelegramStateStore

    resolved = Path(workspace).resolve() if workspace is not None else Path.cwd()
    store = TelegramStateStore(resolved)
    return {
        "pairings": [
            {
                "user_id": int(row.get("telegram_user_id") or 0),
                "chat_id": int(row.get("telegram_chat_id") or 0),
                "display_name": str(row.get("display_name") or ""),
                "permission_level": str(row.get("permission_level") or ""),
                "paired_at": str(row.get("paired_at") or ""),
            }
            for row in store.authorized_users()
        ]
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
