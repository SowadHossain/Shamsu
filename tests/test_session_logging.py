from __future__ import annotations

import json
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from rich.console import Console

from shamsu.cli.repl import (
    _handle_generate_django,
    _handle_log,
    _handle_sessions,
    parse_args,
)
from shamsu.llm.manager import LLMManager
from shamsu.prd.parser import parse_prd_text
from shamsu.prd.project import build_project_spec
from shamsu.session.manager import SessionManager, sanitize_payload
from shamsu.tools.executor import CommandRunner
from shamsu.types import ContextPack, SearchResult


def test_session_manager_creates_metadata_and_index(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Feature work")

    session_dir = tmp_path / ".shamsu" / "sessions" / logger.session_id

    assert (session_dir / "session.json").exists()
    assert (session_dir / "events.jsonl").exists()
    assert (session_dir / "context").is_dir()
    index = json.loads((tmp_path / ".shamsu" / "sessions" / "index.json").read_text())
    assert index["sessions"][0]["title"] == "Feature work"
    assert logger.metadata.event_count == 1


def test_parallel_session_creation_keeps_index_valid_and_complete(tmp_path: Path):
    # Exercise concurrent index updates after the workspace session store has
    # been initialized, matching parallel headless runs in a real workspace.
    SessionManager(tmp_path).root.mkdir(parents=True, exist_ok=True)

    def create(index: int) -> str:
        return SessionManager(tmp_path).create_session(f"Parallel {index}").session_id

    with ThreadPoolExecutor(max_workers=8) as pool:
        session_ids = list(pool.map(create, range(24)))

    raw = (tmp_path / ".shamsu" / "sessions" / "index.json").read_text(encoding="utf-8")
    index = json.loads(raw)
    indexed_ids = {item["session_id"] for item in index["sessions"]}

    assert len(session_ids) == len(set(session_ids)) == 24
    assert indexed_ids == set(session_ids)


def test_session_list_resume_rename_close_and_export(tmp_path: Path):
    manager = SessionManager(tmp_path)
    first = manager.create_session("Alpha Session")
    second = manager.create_session("Beta Session")

    sessions = manager.list_sessions()
    assert sessions[0].session_id == second.session_id

    resumed = manager.resume_session("Alpha")
    assert resumed.session_id == first.session_id

    renamed = manager.rename_session(first.session_id, "Milestone Two")
    assert renamed.title == "Milestone Two"

    closed = manager.close_session(first.session_id)
    assert closed.status == "closed"

    export_path = manager.export_session(first.session_id)
    assert export_path == tmp_path / ".shamsu" / "sessions" / first.session_id / "exports" / f"{first.session_id}.zip"
    with zipfile.ZipFile(export_path) as archive:
        assert set(archive.namelist()) == {"session.json", "events.jsonl", "summary.md"}


def test_logger_appends_redacted_jsonl_and_truncates_payload(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Debug")
    logger.log(
        "llm.request",
        {
            "secret": 'SECRET_KEY = "django-insecure-secret"',
            "large": "x" * 5000,
            "object": object(),
        },
        "Sending SECRET_KEY = \"django-insecure-secret\" to model",
        workflow_id="qa",
    )

    events = logger.tail(1)

    assert events[0]["event_type"] == "llm.request"
    assert "[REDACTED]" in events[0]["summary"]
    assert "[REDACTED]" in events[0]["payload"]["secret"]
    assert "django-insecure-secret" not in events[0]["payload"]["secret"]
    assert "truncated" in events[0]["payload"]["large"]
    assert events[0]["payload"]["object"].startswith("<object object")


def test_context_pack_logging_uses_metadata_and_short_previews(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Context")
    pack = ContextPack(
        task_id="qa-live",
        step_id=1,
        specialist="qa",
        user_request="explain",
        token_estimate=123,
        snippets=[
            SearchResult(
                file_path="app.py",
                language="python",
                line_start=10,
                line_end=30,
                content="a" * 1000,
                score=0.9,
                symbol_name="build",
            )
        ],
    )

    logger.log_context_pack(pack)

    event = logger.tail(1)[0]
    snippet = event["payload"]["snippets"][0]
    assert event["event_type"] == "context.pack"
    assert snippet["file_path"] == "app.py"
    assert snippet["line_start"] == 10
    assert len(snippet["preview"]) == 600


def test_sanitize_payload_stringifies_non_json_values():
    payload = sanitize_payload({"path": Path("README.md")})

    assert payload == {"path": "README.md"}


def test_cli_session_args_and_commands(tmp_path: Path):
    args = parse_args(["--new-session", "Planning"])
    assert args.new_session == "Planning"
    assert parse_args(["--session", "abc"]).session == "abc"

    manager = SessionManager(tmp_path)
    current = manager.create_session("Current")
    other = manager.create_session("Other")
    console = Console(record=True)

    current = _handle_sessions("sessions resume Other", manager, current, console)
    _handle_sessions(f"sessions rename {other.session_id} Renamed", manager, current, console)
    _handle_sessions("sessions current", manager, current, console)
    _handle_sessions(f"sessions export {other.session_id}", manager, current, console)

    output = console.export_text()
    assert "Resumed session" in output
    assert "Renamed session" in output
    assert "Exported session bundle" in output


def test_log_tail_command_prints_recent_events(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Tail")
    logger.log("workflow.started", {"intent": "qa"}, "Workflow started")
    console = Console(record=True)

    _handle_log("log tail 1", logger, console)

    assert "workflow.started" in console.export_text()


@pytest.mark.asyncio
async def test_llm_manager_logs_route_and_specialist_events(tmp_path: Path):
    class FakeLLM(LLMManager):
        async def _generate(self, model, system, prompt, **kwargs):
            if kwargs.get("json_schema"):
                return '{"intent": "qa", "complexity": "single", "confidence": 0.8}'
            return "answer"

    logger = SessionManager(tmp_path).create_session("LLM")
    llm = FakeLLM(session_logger=logger)
    pack = ContextPack(
        task_id="qa-live",
        step_id=1,
        specialist="qa",
        user_request="what is this",
    )

    decision = await llm.route("what is this", "project")
    response = await llm.run_specialist("qa", pack)

    event_types = [event["event_type"] for event in logger.tail(10)]
    assert decision.intent == "qa"
    assert response.raw == "answer"
    assert "router.decision" in event_types
    assert "context.pack" in event_types
    assert "llm.response" in event_types


def test_command_runner_logs_blocked_command(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Commands")
    runner = CommandRunner(tmp_path, session_logger=logger)

    code, _stdout, _stderr = runner.run("rm -rf /", tmp_path)

    assert code != 0
    assert [event["event_type"] for event in logger.tail(3)][-1] == "command.blocked"


def test_generate_django_logs_prd_plan_and_generated_files(tmp_path: Path):
    prd = tmp_path / "todo.md"
    prd.write_text("# Todo App\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")
    logger = SessionManager(tmp_path).create_session("Generate")
    console = Console(record=True)

    _handle_generate_django(
        "generate-django todo.md",
        tmp_path,
        console,
        approval_func=lambda _request: True,
        session_logger=logger,
    )

    event_types = [event["event_type"] for event in logger.tail(80)]
    assert "prd.parsed" in event_types
    assert "project.planned" in event_types
    assert "project.generated" in event_types
    assert (tmp_path / "manage.py").exists()


def test_session_paths_are_workspace_local(tmp_path: Path):
    manager = SessionManager(tmp_path)
    logger = manager.create_session("Sandbox")

    assert logger.events_path.is_relative_to(tmp_path)


def test_pending_question_set_get_answer_and_clear(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Pending")
    question = {
        "question": "Which file should I read?",
        "options": [{"label": "client/src/App.tsx", "description": "frontend"}],
        "created_from_prompt": "read App.tsx",
        "awaiting": "user_input",
    }

    logger.set_pending_question(question)
    stored = logger.get_pending_question()

    assert stored["question"] == "Which file should I read?"
    assert stored["options"][0]["label"] == "client/src/App.tsx"
    assert (tmp_path / ".shamsu" / "sessions" / logger.session_id / "pending.json").exists()

    logger.clear_pending_question(answered=True, answer="client/src/App.tsx")

    assert logger.get_pending_question() == {}
    event_types = [event["event_type"] for event in logger.tail(10)]
    assert "session.pending_question.set" in event_types
    assert "session.pending_question.answered" in event_types


def test_pending_question_cleared_event_when_not_answered(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Pending")
    logger.set_pending_question({"question": "Pick one"})

    logger.clear_pending_question()

    assert logger.get_pending_question() == {}
    assert "session.pending_question.cleared" in [event["event_type"] for event in logger.tail(10)]


def test_project_spec_helper_still_has_generation_order():
    spec = build_project_spec(
        parse_prd_text(
            "# Todo App\n\n## Entities\n- Task: title (text)\n",
            fallback_title="Todo",
            markdown=True,
        )
    )

    assert spec.generation_order


def test_session_thinking_event_carries_a_pointer_not_raw_reasoning(tmp_path, monkeypatch):
    """The session timeline is read back by summaries and search, so raw
    chain-of-thought must live in the run artifact and only be referenced here."""
    from shamsu.action_ledger.ledger import start_run
    from shamsu.agents.chat_loop import AgentChatLoop

    monkeypatch.setenv("SHAMSU_LOG_LEVEL", "verbose")
    logger = SessionManager(tmp_path).create_session()
    ledger = start_run(tmp_path, "reason about it", session_logger=logger)
    loop = AgentChatLoop.__new__(AgentChatLoop)
    loop.session_logger = logger
    loop.action_ledger = ledger
    loop.model_name = "deepseek-r1:7b"

    loop._log_thinking("a very long private reasoning trace " * 500, round_index=1)

    event = [item for item in logger.tail(20) if item["event_type"] == "llm.thinking"][-1]
    assert "thinking" not in event["payload"]
    assert event["payload"]["thinking_chars"] > 4000
    cot_path = event["payload"]["cot_path"]
    assert cot_path and (ledger.run_dir / cot_path).exists()
