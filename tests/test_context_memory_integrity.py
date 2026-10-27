"""Regressions for the audit's context and durable-memory failures."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from shamsu.agents.chat_state import ChatState
from shamsu.agents.simple_chat import SimpleChatLoop, _budgeted, tool_result_budget
from shamsu.agents.simple_memory import MemoryStore, remember, render_memory
from shamsu.context.budget import count_tokens, fit_token_budget, messages_tokens, tool_schema_tokens
from shamsu.memory.service import MemoryService
from shamsu.memory.sqlite_store import SQLiteMemoryStore
from shamsu.tools.agent_tools import AgentToolRegistry


def loop_for(workspace):
    return SimpleChatLoop(
        workspace, client=object(), tools=AgentToolRegistry(workspace, approval_func=lambda _: False),
        model_name="qwen2.5-coder:3b-instruct",
    )


def test_small_history_budget_keeps_the_tool_interaction_together():
    state = ChatState("system", hydrate=False)
    state.append_user("Create the report.")
    state.append_assistant("", tool_calls=[{"id": "call-1", "function": {
        "name": "write_file", "arguments": {"filepath": "report.txt", "content": "large " * 1000},
    }}])
    state.append_tool("call-1", "write_file", "Created report.txt")
    tail, start = state.select_for_budget(20, count_tokens)
    assert start == 1
    assert [message.role for message in tail] == ["user", "assistant", "tool"]
    assert tail[1].tool_calls[0]["id"] == tail[2].tool_call_id


@pytest.mark.parametrize("body", ["字" * 10000, "😀\\\"" * 6000, "const x={a:[1,2,3]};\n" * 4000],
                         ids=["cjk", "escaped-emoji", "dense-code"])
@pytest.mark.parametrize("envelope", [False, True])
def test_tool_caps_apply_to_the_final_unicode_and_json_output(body, envelope):
    payload = json.dumps({"ok": True, "data": {"content": body, "total_lines": 4000}}, ensure_ascii=True) if envelope else body
    result = _budgeted(payload, 8192)
    assert count_tokens(result) <= tool_result_budget(8192)
    parsed = json.loads(result)
    assert parsed.get("truncated") or parsed.get("data", {}).get("content_truncated")


def test_oversized_json_metadata_is_not_cut_into_invalid_json():
    result = _budgeted(json.dumps({"ok": False, "error": "字" * 10000}), 8192)
    assert json.loads(result)["ok"] is False
    assert count_tokens(result) <= tool_result_budget(8192)


def test_token_clipping_counts_its_marker():
    result = fit_token_budget("字" * 10000, 70, " [partial]")
    assert result.endswith(" [partial]")
    assert count_tokens(result) <= 70


def test_project_and_standing_plan_are_charged_to_the_same_grounding(tmp_path, monkeypatch):
    loop = loop_for(tmp_path)
    before = loop._fixed_overhead()
    loop._project = "Project fact. " * 200
    monkeypatch.setattr(loop, "_standing_plan", lambda: "Required plan step. " * 200)
    grounding = loop._grounding()
    assert "Project fact" in grounding and "Required plan step" in grounding
    assert loop._fixed_overhead() > before + 500
    assert loop._fixed_overhead() == count_tokens(grounding) + 8 + tool_schema_tokens(loop._sent_schemas())


def test_grounding_is_snapshotted_once_per_assembled_request(tmp_path, monkeypatch):
    loop = loop_for(tmp_path)
    loop.state.append_user("hello")
    calls = []

    def grounding():
        calls.append(True)
        return "The current project context."

    monkeypatch.setattr(loop, "_grounding", grounding)
    loop._messages()
    assert len(calls) == 1


def test_a_prompt_that_cannot_fit_is_rejected_before_a_model_call(tmp_path, monkeypatch):
    loop = loop_for(tmp_path)
    monkeypatch.setattr(loop, "_ceiling", lambda: 4096)
    loop.state.append_user("字" * 10000)
    with pytest.raises(ValueError, match="No model request was sent"):
        loop._messages()


def test_summary_growth_rebudgets_the_transmitted_prompt(tmp_path, monkeypatch):
    from shamsu.agents.simple_chat import SAFETY_MARGIN_TOKENS, output_reserve

    loop = loop_for(tmp_path)
    monkeypatch.setattr(loop, "_ceiling", lambda: 8192)
    monkeypatch.setattr(loop, "_sent_schemas", lambda: [])
    loop.state = ChatState("system", hydrate=False)
    loop.state.append_user("The founding decision is port 8123.")
    for number in range(6):
        loop.state.append_assistant("word " * 2000)
        loop.state.append_user(f"Next turn {number}")
    messages = loop._messages()
    assert any("8123" in message.get("content", "") for message in messages)
    assert messages_tokens(messages) <= 8192 - output_reserve(8192) - SAFETY_MARGIN_TOKENS


def test_oversized_tool_payload_can_be_elided_without_dropping_its_call(tmp_path, monkeypatch):
    loop = loop_for(tmp_path)
    monkeypatch.setattr(loop, "_ceiling", lambda: 8192)
    monkeypatch.setattr(loop, "_sent_schemas", lambda: [])
    loop.state = ChatState("system", hydrate=False)
    loop.state.append_user("Create x.py")
    loop.state.append_assistant("", tool_calls=[{"id": "write-1", "function": {
        "name": "write_file", "arguments": {"filepath": "x.py", "content": "print(1)\n" * 5000},
    }}])
    loop.state.append_tool("write-1", "write_file", '{"ok":true,"message":"Created x.py"}')
    messages = loop._messages()
    assert messages_tokens(messages) < 8192
    assert any(message.get("tool_calls", [{}])[0].get("id") == "write-1" for message in messages)
    assert any(message.get("tool_call_id") == "write-1" for message in messages)


def test_outlines_and_brief_are_invalidated_even_with_unchanged_timestamps(tmp_path):
    path = tmp_path / "a.py"
    path.write_text("def old_name():\n    return 1\n")
    loop = loop_for(tmp_path)
    loop._files = ["a.py"]
    before = loop._skeletons()
    loop._brief = "Stale graph fact: old_name"
    stamp = path.stat()
    path.write_text("def new_name():\n    return 2\n")
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    after = loop._grounding()
    assert "old_name" in before
    assert "new_name" in after
    assert "old_name" not in after


@pytest.mark.parametrize("request_text", ["Implement the authentication feature", "实现登录功能", "লগইন যোগ করো"],
                         ids=["english", "chinese", "bengali"])
def test_cli_preferences_reach_the_default_loop_without_keyword_overlap(tmp_path, request_text):
    assert MemoryService(tmp_path).remember_local(
        "Always use PostgreSQL for this project", "user_preference", {"explicit": True},
    )["ok"]
    assert "PostgreSQL" in render_memory(tmp_path, request_text)


def test_model_notes_and_cli_forget_use_the_same_record(tmp_path):
    assert remember(tmp_path, "workflow", "tests", "Run pytest from the project root.")[0]
    service = MemoryService(tmp_path)
    record = next(memory for memory in service.get_relevant("pytest") if "pytest" in memory.text)
    assert service.forget(record.memory_id)["ok"]
    assert "pytest" not in render_memory(tmp_path, "pytest tests")


def test_default_forget_also_removes_a_cli_memory(tmp_path):
    result = MemoryService(tmp_path).remember_local("Use port 8123", "user_preference")
    assert MemoryStore(tmp_path).forget(result["memory_id"])[0]
    assert not MemoryService(tmp_path).get_relevant("8123")


def test_two_existing_store_instances_do_not_overwrite_each_others_notes(tmp_path):
    first, second = MemoryStore(tmp_path), MemoryStore(tmp_path)
    assert first.remember("decision", "first", "First durable fact.")[0]
    assert second.remember("decision", "second", "Second durable fact.")[0]
    assert {note.title for note in first.all_notes()} == {"first", "second"}


def test_concurrent_writers_preserve_distinct_facts_and_deduplicate_same_fact(tmp_path):
    barrier = Barrier(6)

    def save(number):
        store = MemoryStore(tmp_path)
        barrier.wait(timeout=10)
        assert store.remember("context", f"topic {number}", f"Fact number {number}.")[0]
        assert store.remember("decision", "shared", "The shared database is PostgreSQL.")[0]

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(save, range(6)))
    notes = MemoryStore(tmp_path).all_notes()
    assert len(notes) == 7
    assert sum(note.title == "shared" for note in notes) == 1


def test_separate_process_writers_share_the_same_store(tmp_path):
    script = (
        "from pathlib import Path; import sys; "
        "from shamsu.agents.simple_memory import remember; "
        "assert remember(Path(sys.argv[1]), 'context', sys.argv[2], 'A durable process fact.')[0]"
    )
    processes = [subprocess.Popen([sys.executable, "-c", script, str(tmp_path), name],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for name in ["one", "two"]]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, (stdout, stderr)
    assert {note.title for note in MemoryStore(tmp_path).all_notes()} == {"one", "two"}


def test_same_type_and_title_updates_the_record_in_place(tmp_path):
    store = MemoryStore(tmp_path)
    assert store.remember("decision", "database", "Use SQLite.")[0]
    original = store.all_notes()[0]
    assert store.remember("decision", "database", "Use PostgreSQL.")[0]
    updated = store.all_notes()
    assert len(updated) == 1
    assert updated[0].id == original.id
    assert updated[0].created_at == original.created_at
    assert "PostgreSQL" in updated[0].content


def test_failed_database_write_reports_failure(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("disk is full")

    monkeypatch.setattr(store.service.fallback, "remember", fail)
    ok, message = store.remember("context", "not saved", "This cannot be committed.")
    assert not ok
    assert "disk is full" in message
    assert not MemoryStore(tmp_path).all_notes()


def test_migration_preserves_ids_and_does_not_resurrect_forgotten_notes(tmp_path):
    root = tmp_path / ".shamsu" / "memory" / "notes"
    root.mkdir(parents=True)
    index = root / "index.json"
    raw = json.dumps({"notes": [{"id": "old-id", "type": "decision", "title": "database",
                               "content": "Use PostgreSQL.", "tags": ["storage"]}]})
    index.write_text(raw)
    service = MemoryService(tmp_path)
    assert service.list_local()[0].memory_id == "old-id"
    assert "PostgreSQL" in render_memory(tmp_path, "database")
    assert service.forget("old-id")["ok"]
    assert not MemoryStore(tmp_path).all_notes()
    assert index.read_text() == raw


def test_recall_preserves_edited_exports_and_legacy_markdown(tmp_path):
    store = MemoryStore(tmp_path)
    assert store.remember("decision", "database", "Use SQLite.")[0]
    exported = next((store.root / "exports").glob("decision-*.md"))
    exported.write_text("An edited generated view, preserved by recall.")
    legacy = store.root / "my-manual-note.md"
    legacy.write_text("An original manual note.")
    assert "SQLite" in render_memory(tmp_path, "database")
    assert exported.read_text().startswith("An edited")
    assert legacy.read_text() == "An original manual note."


def test_forget_markers_survive_overlapping_writers(tmp_path):
    services = [MemoryService(tmp_path), MemoryService(tmp_path)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda pair: pair[0]._add_tombstone(pair[1]),
                      zip(services, ["first forbidden phrase", "second forbidden phrase"])))
    assert len(MemoryService(tmp_path)._load_tombstones()["ids"]) == 2


def test_a_stale_snapshot_cannot_overwrite_an_updated_fact(tmp_path):
    assert remember(tmp_path, "decision", "database", "Use SQLite.")[0]
    stale = MemoryStore(tmp_path)
    assert remember(tmp_path, "decision", "database", "Use PostgreSQL.")[0]
    for note in stale.notes.values():
        note.tier = "archive"
    stale._save()
    current = MemoryStore(tmp_path).all_notes()[0]
    assert "PostgreSQL" in current.content
    assert current.tier == "hot"


def test_shared_database_makes_memory_tools_available(tmp_path):
    from shamsu.agents.simple_chat import available_tool_families

    assert MemoryService(tmp_path).remember_local("Prefer port 8123", "user_preference")["ok"]
    assert "memory" in available_tool_families(tmp_path)


def test_original_sqlite_schema_is_upgraded_before_creating_new_indexes(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE memories (id TEXT PRIMARY KEY, kind TEXT, text TEXT, metadata TEXT, created_at REAL)")
        connection.execute("INSERT INTO memories VALUES ('old', 'project_fact', 'Use port 8123', '{}', 1)")
    store = SQLiteMemoryStore(path)
    assert store.list_active()[0].text == "Use port 8123"
    assert store.remember("Use Python for scripts", "workflow_rule")["ok"]


def test_failed_write_rolls_back_the_tombstone_change(tmp_path):
    store = SQLiteMemoryStore(tmp_path / "memory.db")
    store.add_tombstone("A forgotten durable fact")
    with pytest.raises(TypeError):
        store.remember("A forgotten durable fact", "project_fact", {"invalid_metadata": {object()}})
    assert "A forgotten durable fact" in store.load_tombstones()["ids"]
    assert not store.list_active()


def test_legacy_forget_markers_are_migrated_once(tmp_path):
    directory = tmp_path / ".shamsu" / "memory"
    directory.mkdir(parents=True)
    legacy = directory / "tombstones.json"
    original = json.dumps({"ids": ["a distinctive forgotten fact"], "texts": []})
    legacy.write_text(original)
    service = MemoryService(tmp_path)
    assert "a distinctive forgotten fact" in service._load_tombstones()["ids"]
    service._remove_tombstone("a distinctive forgotten fact")
    assert not MemoryService(tmp_path)._load_tombstones()["ids"]
    assert legacy.read_text() == original


def test_unicode_preferences_fit_the_complete_memory_block_budget(tmp_path):
    from shamsu.agents.simple_memory import MAX_MEMORY_TOKENS

    assert MemoryService(tmp_path).remember_local("字" * 1000, "user_preference")["ok"]
    rendered = render_memory(tmp_path, "Implement a feature")
    assert "字" in rendered
    assert count_tokens(rendered) <= MAX_MEMORY_TOKENS


def test_secret_note_is_rejected_by_the_shared_policy(tmp_path):
    ok, _ = remember(tmp_path, "context", "credentials", "api_key=private-example-value")
    assert not ok
    assert not MemoryService(tmp_path).list_local()


def test_cleanup_failure_is_distinct_from_the_successful_commit(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path)

    def fail():
        raise OSError("maintenance unavailable")

    monkeypatch.setattr(store, "tidy", fail)
    ok, message = store.remember("context", "saved fact", "This is committed before cleanup.")
    assert ok
    assert "cleanup unavailable" in message
    assert MemoryStore(tmp_path).all_notes()[0].title == "saved fact"


def test_export_failure_does_not_misreport_a_committed_fact(tmp_path):
    store = MemoryStore(tmp_path)
    (store.root / "exports").write_text("A non-directory blocks the optional export.")
    ok, message = store.remember("context", "saved fact", "This is committed without an export.")
    assert ok
    assert "export unavailable" in message
    assert MemoryStore(tmp_path).all_notes()[0].title == "saved fact"
