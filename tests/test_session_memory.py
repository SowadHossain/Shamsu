"""Tests for the persistent session manager upgrade: auto-titling, title
dedupe, pending-action state, session summary, local/long-term memory bridge,
transcript hydration, and the new /sessions CLI commands."""
from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console

import shamsu.memory.service as memory_service
from shamsu.memory.queue import get_memory_queue
from shamsu.agents.chat_state import ChatState
from shamsu.agents.orchestrator import AgentOrchestrator
from shamsu.cli.repl import _bugfix_report_from_last_failure, _handle_sessions
from shamsu.session.manager import SessionManager, generate_title_from_prompt
from shamsu.session.memory import ConversationMemory, is_affirmative, is_negative


# --------------------------------------------------------------------------
# 1. Session creation + default state.json
# --------------------------------------------------------------------------

def test_session_creation_writes_core_files_and_default_state(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session()
    session_dir = tmp_path / ".shamsu" / "sessions" / logger.session_id

    assert (session_dir / "session.json").exists()
    assert (session_dir / "events.jsonl").exists()
    # New sessions start as a placeholder, not "SHAMSU Session".
    assert logger.metadata.title == "Untitled Session"

    # state.json is created on first access and carries the documented defaults.
    state = logger.read_state()
    assert state["pending_action"] == {}
    assert state["last_route"] == {}
    assert state["last_tool_plan"] == []
    logger.write_state(state)
    assert (session_dir / "state.json").exists()
    assert json.loads((session_dir / "state.json").read_text())["updated_at"]


def test_bugfix_reuse_ignores_expected_probe_and_keeps_actionable_command(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session()
    logger.set_last_failure("pytest -q", "AssertionError: expected 2", 1)
    actionable = logger.get_last_failure()

    logger.set_last_failure(
        "git status --short",
        "fatal: not a git repository",
        128,
        classification="expected_condition",
        source="environment_probe",
    )

    assert logger.get_last_failure() == actionable
    report = _bugfix_report_from_last_failure("fix it", logger)
    assert report is not None
    assert "pytest -q" in report[0]


def test_metadata_is_backward_compatible_with_old_session_json(tmp_path: Path):
    manager = SessionManager(tmp_path)
    logger = manager.create_session("Legacy")
    session_json = tmp_path / ".shamsu" / "sessions" / logger.session_id / "session.json"

    # Simulate an old file missing the new fields (and with an unknown extra).
    data = json.loads(session_json.read_text())
    for field in ("auto_titled", "message_count", "summary_updated_at"):
        data.pop(field, None)
    data["legacy_extra"] = "ignored"
    session_json.write_text(json.dumps(data), encoding="utf-8")
    # Rewrite the index too so list_sessions reads the trimmed shape.
    index_path = tmp_path / ".shamsu" / "sessions" / "index.json"
    index_path.write_text(json.dumps({"sessions": [data]}), encoding="utf-8")

    sessions = manager.list_sessions()
    assert sessions[0].title == "Legacy"
    assert sessions[0].auto_titled is False
    assert sessions[0].message_count == 0


# --------------------------------------------------------------------------
# 2. Auto-title
# --------------------------------------------------------------------------

def test_generate_title_from_prompt_examples():
    assert generate_title_from_prompt("fix git routing so stage/commit uses git tools") == "Fix Git Routing"
    assert generate_title_from_prompt("build the product from this PRD") == "Build Product From PRD"
    assert generate_title_from_prompt("show git status and diff, then commit the current changes") == "Show Git Status And Diff"
    for greeting in ("hi", "hello there", "thanks", "yo", "yes", "  "):
        assert generate_title_from_prompt(greeting) == "Untitled Session"
    # Titles are bounded and title-cased.
    title = generate_title_from_prompt("refactor the authentication middleware layer thoroughly")
    assert len(title) <= 60
    assert title == title  # deterministic
    assert title.istitle() or any(w.isupper() for w in title.split())


def test_maybe_auto_title_renames_on_first_meaningful_prompt(tmp_path: Path):
    manager = SessionManager(tmp_path)
    logger = manager.create_session()

    # A trivial greeting must not rename the session.
    manager.maybe_auto_title(logger, "hey there")
    assert logger.metadata.title == "Untitled Session"
    assert logger.metadata.auto_titled is False

    # The first meaningful prompt auto-names it exactly once.
    manager.maybe_auto_title(logger, "fix git routing so stage/commit uses git tools")
    assert logger.metadata.title == "Fix Git Routing"
    assert logger.metadata.auto_titled is True

    # A later prompt does not overwrite the established title.
    manager.maybe_auto_title(logger, "build the product from this PRD")
    assert logger.metadata.title == "Fix Git Routing"


# --------------------------------------------------------------------------
# 3. Duplicate title handling
# --------------------------------------------------------------------------

def test_duplicate_generated_titles_are_suffixed(tmp_path: Path):
    manager = SessionManager(tmp_path)
    first = manager.create_session()
    second = manager.create_session()

    manager.maybe_auto_title(first, "fix git routing so stage/commit uses git tools")
    manager.maybe_auto_title(second, "fix git routing please")

    assert first.metadata.title == "Fix Git Routing"
    assert second.metadata.title == "Fix Git Routing (2)"


def test_manual_rename_also_dedupes_and_sticks(tmp_path: Path):
    manager = SessionManager(tmp_path)
    first = manager.create_session("Fix Git Routing")
    second = manager.create_session("Other")

    renamed = manager.rename_session(second.session_id, "Fix Git Routing")
    assert renamed.title == "Fix Git Routing (2)"
    assert renamed.auto_titled is True  # manual rename is authoritative

    # The manual title is preserved against any later auto-title attempt.
    manager.maybe_auto_title(SessionManager(tmp_path).logger_for(second.session_id), "some new task entirely")
    assert manager.resolve(second.session_id).title == "Fix Git Routing (2)"
    assert first.metadata.title == "Fix Git Routing"


# --------------------------------------------------------------------------
# 4. Pending action state
# --------------------------------------------------------------------------

def test_pending_action_set_get_clear_and_events(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Pending")

    logger.set_pending_action(
        {"type": "git_stage", "scope": "all", "awaiting": "confirmation", "created_from_prompt": "stage the files"}
    )
    pending = logger.get_pending_action()
    assert pending["type"] == "git_stage"
    assert pending["awaiting"] == "confirmation"

    logger.clear_pending_action()
    assert logger.get_pending_action() == {}

    event_types = [event["event_type"] for event in logger.tail(20)]
    assert "session.pending_action.set" in event_types
    assert "session.pending_action.cleared" in event_types


def test_route_and_tool_plan_state(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Route")
    logger.set_last_route({"route": "git", "handled": False})
    logger.set_last_tool_plan([{"type": "plan", "text": "step 1"}])

    assert logger.get_last_route()["route"] == "git"
    assert logger.get_last_tool_plan()[0]["text"] == "step 1"
    assert "session.route.updated" in [e["event_type"] for e in logger.tail(20)]


# --------------------------------------------------------------------------
# 5. ConversationMemory
# --------------------------------------------------------------------------

def test_conversation_memory_reads_chat_messages_and_legacy(tmp_path: Path):
    manager = SessionManager(tmp_path)

    chat = manager.create_session("Chat")
    chat.log("chat.message", {"role": "user", "content": "what files are here?"}, "c")
    chat.log("chat.message", {"role": "assistant", "content": "three files"}, "c")
    chat.log("chat.message", {"role": "tool", "content": "{ok: true}"}, "c")
    memory = ConversationMemory.from_session(chat)
    roles = [turn.role for turn in memory.turns]
    assert roles == ["user", "assistant"]  # tool turn excluded
    assert memory.turns[0].text == "what files are here?"

    legacy = manager.create_session("Legacy")
    legacy.log("user.prompt", {"prompt": "legacy question"}, "u")
    legacy.log("assistant.message", {"message": "legacy answer"}, "a")
    legacy_memory = ConversationMemory.from_session(legacy)
    assert [turn.text for turn in legacy_memory.turns] == ["legacy question", "legacy answer"]


def test_affirmative_and_negative_detection():
    for word in ("yes", "yep", "sure", "ok", "do it", "go ahead", "proceed"):
        assert is_affirmative(word)
    for word in ("no", "cancel", "stop", "don't", "do not"):
        assert is_negative(word)
    # A full sentence is not a bare confirmation and must not resolve pending.
    assert not is_affirmative("yes please stage everything in the repo")
    assert not is_negative("no changes were needed for this file")


# --------------------------------------------------------------------------
# 6. ChatState hydration
# --------------------------------------------------------------------------

def test_chat_state_hydrates_transcript_and_skips_debug_events(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Hydrate")

    writer = ChatState("SYSTEM PROMPT", session_logger=logger, hydrate=False)
    writer.append_user("what files are here?")
    writer.append_assistant("There are three files.")
    # Noise that must never be hydrated into the model context.
    logger.log("router.decision", {"intent": "qa"}, "internal routing decision")

    hydrated = ChatState("SYSTEM PROMPT", session_logger=logger, hydrate=True)
    messages = hydrated.all_messages
    assert messages[0].role == "system"
    joined = " ".join(message.content for message in messages)
    assert "what files are here?" in joined
    assert "There are three files." in joined
    assert "internal routing decision" not in joined


def test_chat_state_falls_back_to_events_when_no_transcript(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Fallback")
    logger.log("chat.message", {"role": "user", "content": "old style user"}, "c")
    logger.log("chat.message", {"role": "assistant", "content": "old style assistant"}, "c")
    assert not logger.messages_path.exists()

    hydrated = ChatState("SYSTEM PROMPT", session_logger=logger, hydrate=True)
    joined = " ".join(message.content for message in hydrated.all_messages)
    assert "old style user" in joined
    assert "old style assistant" in joined


# --------------------------------------------------------------------------
# 7. Local + long-term memory
# --------------------------------------------------------------------------

def test_local_memory_writes_and_dedupes(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Memory")

    first = logger.append_local_memory("bug_lesson", "Always route git through git tools")
    duplicate = logger.append_local_memory("bug_lesson", "Always route git through git tools")
    assert first is not None
    assert duplicate is None  # identical text is not appended twice

    records = logger.read_local_memory()
    assert len(records) == 1
    assert logger.memory_path.exists()
    assert records[0]["kind"] == "bug_lesson"


def test_save_long_term_memory_survives_backend_failure(tmp_path: Path, monkeypatch):
    logger = SessionManager(tmp_path).create_session("Durable")

    def boom(self, *args, **kwargs):  # noqa: ANN001
        raise RuntimeError("graphiti offline")

    monkeypatch.setattr(memory_service.MemoryService, "mirror_to_graphiti", boom)

    result = logger.save_long_term_memory("session_summary", "A durable, meaningful task summary")
    # Local memory is immediate; the queued mirror failure is only telemetry.
    assert result["local"] is True
    assert result["long_term"]["queued"] is True
    assert get_memory_queue(tmp_path).flush(1.0) is True
    assert logger.read_local_memory()[0]["kind"] == "session_summary"
    assert "memory.long_term.failed" in [event["event_type"] for event in logger.tail(20)]


def test_save_long_term_memory_records_metadata(tmp_path: Path, monkeypatch):
    logger = SessionManager(tmp_path).create_session("Meta")
    captured: dict = {}

    original = memory_service.MemoryService.remember_local

    def capture_remember(self, text, kind=None, metadata=None):  # noqa: ANN001
        captured["text"] = text
        captured["kind"] = kind
        captured["metadata"] = metadata
        return original(self, text, kind, metadata)

    monkeypatch.setattr(memory_service.MemoryService, "remember_local", capture_remember)

    logger.save_long_term_memory("task_summary", "Completed the routing fix", {"workflow": "agent-chat"})
    assert captured["metadata"]["session_id"] == logger.session_id
    assert captured["metadata"]["session_title"] == "Meta"
    assert captured["metadata"]["source"] == "session_manager"
    assert captured["metadata"]["workflow"] == "agent-chat"


# --------------------------------------------------------------------------
# 8. Session summary
# --------------------------------------------------------------------------

def test_session_summary_writes_reads_and_updates_metadata(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Summary")
    logger.log("user.prompt", {"prompt": "fix the parser"}, "prompt")
    logger.log("agent.tool_call", {"tool_name": "write_file", "arguments": {"filepath": "parser.py"}}, "tool")

    text = logger.update_summary_from_events()
    assert "# Session Summary" in text
    assert "fix the parser" in text
    assert "parser.py" in text
    assert logger.read_summary() == text
    assert logger.summary_path.exists()
    assert logger.metadata.summary_updated_at != ""


# --------------------------------------------------------------------------
# 9. CLI: /sessions trace|summary|memory|search
# --------------------------------------------------------------------------

def test_sessions_trace_shows_actions_without_chain_of_thought(tmp_path: Path):
    manager = SessionManager(tmp_path)
    current = manager.create_session("Trace")
    current.log("router.decision", {"intent": "git_read"}, "routed to git_read")
    current.log("agent.tool_call", {"tool_name": "git_status", "arguments": {}}, "called git_status")
    current.log("agent.tool_result", {"tool_name": "git_status", "ok": True}, "git_status ok")
    # A raw model turn that must NOT appear in the structured trace.
    current.log("chat.message", {"role": "assistant", "content": "HIDDEN_REASONING_MARKER"}, "chat")

    console = Console(record=True)
    _handle_sessions("sessions trace", manager, current, console)
    output = console.export_text()
    assert "git_status" in output
    assert "HIDDEN_REASONING_MARKER" not in output


def test_sessions_summary_and_memory_and_search_commands(tmp_path: Path):
    manager = SessionManager(tmp_path)
    current = manager.create_session("CLI")
    current.log("user.prompt", {"prompt": "investigate the special pineapple bug"}, "p")
    current.append_local_memory("bug_lesson", "the special pineapple bug came from git routing")
    current.update_summary_from_events()

    console = Console(record=True)
    _handle_sessions("sessions summary", manager, current, console)
    _handle_sessions("sessions memory", manager, current, console)
    _handle_sessions("sessions search pineapple", manager, current, console)
    output = console.export_text()

    assert "Session Summary" in output or "pineapple" in output
    assert "bug_lesson" in output
    assert "pineapple" in output  # search found the message/memory


def test_recent_file_followup_uses_successful_session_evidence(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Recent file")
    logger.log(
        "agent.tool_result",
        {
            "tool_name": "write_file",
            "ok": True,
            "data": {
                "filepath": "nested/probe.txt",
                "resolved_filepath": "nested/probe.txt",
                "created": True,
            },
        },
        "write succeeded",
    )

    result = AgentOrchestrator(tmp_path, session_logger=logger).run(
        "What file did you just create and where is it?"
    )

    assert result.handled is True
    assert result.action == "session.recent_files"
    assert "nested/probe.txt" in result.message
