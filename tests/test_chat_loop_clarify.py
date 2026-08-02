from __future__ import annotations

import sys
from pathlib import Path

import pytest

from shamsu.agents.chat_loop import (
    AgentChatLoop,
    _edit_failure_correction,
    _promised_read_tool_call,
    _promised_write_tool_call,
    _proposes_additional_file_write,
)
from shamsu.session.manager import SessionManager
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import LLMResponse
from shamsu.verify.gate import VerifyOutcome


class ScriptedClient:
    """Returns a queued list of model responses, recording the messages it was
    given each round so tests can assert on injected corrections."""

    def __init__(self, responses: list[dict]) -> None:
        self._responses = responses
        self.messages_seen: list[list[dict]] = []

    async def chat(self, model, messages, tools, stream, options):
        self.messages_seen.append([dict(message) for message in messages])
        return self._responses.pop(0)


class NoPlanLLM:
    async def run_specialist(self, specialist, pack):
        return LLMResponse(raw="", model_used="fake")


def test_edit_failure_correction_includes_current_source_excerpt():
    correction = _edit_failure_correction(
        "app.py",
        "old_string not found",
        {"current_excerpt": "VALUE = 1\n"},
        old_string="VALUE = 0",
        new_string="VALUE = 2",
    )

    assert "--- current source ---" in correction
    assert "VALUE = 1" in correction


def test_empty_edit_anchor_correction_offers_both_add_and_replace():
    """Still refuses to ASSUME append intent, but naming only the ambiguity was
    not actionable: live 2026-08-01 a 7B model tried to ADD `AUTH_USER_MODEL`
    with an empty old_string, edit_file refused, and "read the file, then
    decide" left it repeating the identical failing call until the run hit its
    deadlock timeout. Both branches are now concrete single next calls."""
    correction = _edit_failure_correction(
        "app.py",
        "Missing old_string.",
        old_string="",
        new_string="VALUE = 2",
    )

    assert "does not reveal whether you intended to add or replace" in correction
    # Neither branch is presented as the assumed one - both are spelled out.
    assert "to ADD it as new content, call append_file" in correction
    assert "to REPLACE existing content, call read_file" in correction
    assert "Do not retry the empty anchor" in correction


def test_empty_edit_anchor_correction_omits_append_when_it_is_not_registered():
    """A milestone repair narrows the toolset (read_file/file_info/edit_file),
    so the correction must not send the model at a tool it cannot call."""
    correction = _edit_failure_correction(
        "app.py",
        "Missing old_string.",
        old_string="",
        new_string="VALUE = 2",
        append_available=False,
    )

    assert "append_file" not in correction
    assert "to ADD it as new content, call read_file and then edit_file" in correction


def test_add_following_code_to_path_is_a_concrete_write_proposal():
    assert _proposes_additional_file_write(
        "Let's add the following code to `backend/core/views.py`:"
    )


@pytest.mark.asyncio
async def test_outer_workflow_can_disable_inner_verify_claims(tmp_path: Path):
    loop = AgentChatLoop(
        tmp_path,
        client=ScriptedClient([]),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        llm=NoPlanLLM(),
        verify_changes=False,
    )

    assert await loop._maybe_verify("outer verifier owns the verdict", ["missing.py"]) == (
        "outer verifier owns the verdict"
    )


def _tool_call(name: str, arguments: dict) -> dict:
    return {"id": f"call_{name}", "function": {"name": name, "arguments": arguments}}


def _message(content: str = "", tool_calls: list[dict] | None = None) -> dict:
    return {"message": {"content": content, "tool_calls": tool_calls or []}}


def _loop(tmp_path: Path, client, session_logger=None) -> AgentChatLoop:
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True, session_logger=session_logger)
    return AgentChatLoop(
        tmp_path,
        client=client,
        tools=tools,
        llm=NoPlanLLM(),
        session_logger=session_logger,
    )


@pytest.mark.asyncio
async def test_approval_denial_stops_without_retrying_the_same_mutation(tmp_path: Path):
    client = ScriptedClient(
        [
            _message(
                tool_calls=[
                    _tool_call(
                        "write_file",
                        {"filepath": "denied.txt", "content": "not written"},
                    )
                ]
            )
        ]
    )
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: False)
    loop = AgentChatLoop(tmp_path, client=client, tools=tools, llm=NoPlanLLM())

    result = await loop.run("create denied.txt")

    assert result.stopped is True
    assert "approval was denied" in result.final
    assert len(client.messages_seen) == 1
    assert (tmp_path / "denied.txt").exists() is False


@pytest.mark.asyncio
async def test_successful_candidate_write_clears_stale_failed_path(tmp_path: Path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config/settings.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedClient(
        [
            _message(
                tool_calls=[
                    _tool_call(
                        "append_file",
                        {"filepath": "settings.py", "content": "VALUE = 2\n"},
                    )
                ]
            ),
            _message(
                tool_calls=[
                    _tool_call(
                        "append_file",
                        {"filepath": "config/settings.py", "content": "VALUE = 2\n"},
                    )
                ]
            ),
            _message(content="Done."),
        ]
    )
    loop = _loop(tmp_path, client)

    result = await loop.run("append the setting to config/settings.py")

    assert result.final.startswith("Done.")
    assert "could not confirm" not in result.final.lower()
    assert (tmp_path / "config/settings.py").read_text(encoding="utf-8").endswith(
        "VALUE = 2\n"
    )


@pytest.mark.asyncio
async def test_successful_command_codegen_counts_as_workspace_mutation(tmp_path: Path):
    command = (
        f'"{sys.executable}" -c "from pathlib import Path; '
        "Path('generated.py').write_text('VALUE = 1\\n')\""
    )
    client = ScriptedClient(
        [
            _message(tool_calls=[_tool_call("run_command", {"command": command})]),
            _message(content="Generated the file."),
        ]
    )
    loop = _loop(tmp_path, client)

    result = await loop.run("create generated.py using the project generator")

    assert result.final.startswith("Generated the file.")
    assert result.changed_files == ("generated.py",)


# ---------------------------------------------------------------------------
# Gap J2: the stall guards must ASK for the missing decision, not give up.
# `safety/clarify.py` was built for exactly this and was never wired - the
# loop always ended on a dead-end message the user couldn't act on.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_identical_call_asks_the_user_instead_of_giving_up(tmp_path: Path):
    """Repeating one call means a missing DECISION, not missing effort."""
    logger = SessionManager(tmp_path).create_session("Repeat")
    same_call = _message(tool_calls=[_tool_call("read_file", {"filepath": "ghost.py"})])
    client = ScriptedClient([same_call, same_call, same_call, same_call])
    loop = _loop(tmp_path, client, session_logger=logger)

    result = await loop.run("fix the bug in ghost.py")

    # It ends the turn as a QUESTION the user can answer...
    assert result.awaiting_user is True
    assert result.stopped is True
    assert "read_file" in result.final
    assert "?" in result.final
    # ...and the question survives the turn, so the next reply resumes the work.
    pending = logger.get_pending_question()
    assert pending.get("question")
    assert pending["created_from_prompt"] == "fix the bug in ghost.py"
    assert pending["source"] == "stall_guard"


@pytest.mark.asyncio
async def test_repeated_successful_read_recovers_without_asking_user(tmp_path: Path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    same_read = _message(tool_calls=[_tool_call("read_file", {"filepath": "app.py"})])
    write = _message(
        tool_calls=[
            _tool_call(
                "write_file",
                {"filepath": "app.py", "content": "VALUE = 2\n"},
            )
        ]
    )
    client = ScriptedClient([same_read, same_read, same_read, write, _message("Done.")])
    loop = _loop(tmp_path, client)

    result = await loop.run("fix app.py")

    assert result.awaiting_user is False
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    recovery_messages = [
        message.get("content", "")
        for round_messages in client.messages_seen
        for message in round_messages
        if message.get("role") == "user"
    ]
    assert any("DIFFERENT concrete action" in message for message in recovery_messages)


@pytest.mark.asyncio
async def test_exhausted_read_recovery_asks_with_candidates_as_options(tmp_path: Path):
    """After recoveries run out, the user knows the right path even when the
    model doesn't - so offer the candidates rather than dead-ending."""
    (tmp_path / "client").mkdir()
    (tmp_path / "admin").mkdir()
    (tmp_path / "client" / "App.tsx").write_text("x", encoding="utf-8")
    (tmp_path / "admin" / "App.tsx").write_text("y", encoding="utf-8")

    logger = SessionManager(tmp_path).create_session("ReadStall")
    failed_read = _message(tool_calls=[_tool_call("read_file", {"filepath": "App.tsx"})])
    stall = _message(content="I will read App.tsx next.")
    # One failed read, then prose-only stalls until recoveries are exhausted.
    client = ScriptedClient([failed_read] + [stall] * 6)
    loop = _loop(tmp_path, client, session_logger=logger)

    result = await loop.run("read App.tsx")

    assert result.awaiting_user is True
    pending = logger.get_pending_question()
    assert pending.get("question")
    labels = [option["label"] for option in pending.get("options", [])]
    assert any("App.tsx" in label for label in labels), labels


def test_scoped_missing_write_target_wins_over_same_name_candidates(tmp_path: Path):
    (tmp_path / "demo/backend/config").mkdir(parents=True)
    (tmp_path / "demo/backend/core").mkdir(parents=True)
    (tmp_path / "demo/backend/config/__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "demo/backend/core/__init__.py").write_text("", encoding="utf-8")
    loop = _loop(tmp_path, ScriptedClient([]))
    loop.tools.set_allowed_write_paths(
        ("demo/backend/core/migrations/__init__.py",)
    )

    correction = loop._read_failure_correction(
        "backend/core/migrations/__init__.py",
        "Not a file.",
    )

    assert "call write_file now" in correction
    assert "ask_user" not in correction


def test_promised_read_salvage_uses_final_intended_file():
    call = _promised_read_tool_call(
        "I've read `settings.py`. Let's read `urls.py` next."
    )

    assert call is not None
    assert call["function"]["arguments"] == {"filepath": "urls.py"}


@pytest.mark.asyncio
async def test_stall_ask_is_logged_as_agent_stuck(tmp_path: Path):
    """The `agent.stuck` telemetry from the old give-up path is preserved."""
    import json

    logger = SessionManager(tmp_path).create_session("Telemetry")
    same_call = _message(tool_calls=[_tool_call("read_file", {"filepath": "ghost.py"})])
    client = ScriptedClient([same_call] * 4)
    loop = _loop(tmp_path, client, session_logger=logger)

    await loop.run("fix ghost.py")

    lines = logger.events_path.read_text(encoding="utf-8").splitlines()
    stuck = [json.loads(line) for line in lines if "agent.stuck" in line]
    assert stuck
    assert stuck[0]["payload"]["asked"] is True


@pytest.mark.asyncio
async def test_ask_user_ends_turn_and_persists_pending_question(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Ask")
    client = ScriptedClient([
        _message(tool_calls=[_tool_call("ask_user", {
            "question": "Which file should I read?",
            "options": [
                {"label": "client/src/App.tsx", "description": "frontend"},
                {"label": "admin/src/App.tsx", "description": "admin"},
            ],
            "allow_free_text": True,
        })]),
    ])
    loop = _loop(tmp_path, client, session_logger=logger)

    result = await loop.run("read the file src/App.tsx")

    assert result.awaiting_user is True
    assert result.stopped is True
    assert "Which file should I read?" in result.final
    assert "1. client/src/App.tsx" in result.final

    pending = logger.get_pending_question()
    assert pending["question"] == "Which file should I read?"
    assert [option["label"] for option in pending["options"]] == [
        "client/src/App.tsx",
        "admin/src/App.tsx",
    ]
    assert pending["created_from_prompt"] == "read the file src/App.tsx"


@pytest.mark.asyncio
async def test_failed_read_with_multiple_candidates_surfaces_choice(tmp_path: Path):
    (tmp_path / "client" / "src").mkdir(parents=True)
    (tmp_path / "admin" / "src").mkdir(parents=True)
    (tmp_path / "client" / "src" / "App.tsx").write_text("1\n", encoding="utf-8")
    (tmp_path / "admin" / "src" / "App.tsx").write_text("2\n", encoding="utf-8")

    client = ScriptedClient([
        _message(tool_calls=[_tool_call("read_file", {"filepath": "src/App.tsx"})]),
        _message(content="Both files could match.", tool_calls=[_tool_call("ask_user", {
            "question": "Which App.tsx?",
            "options": [{"label": "client/src/App.tsx"}, {"label": "admin/src/App.tsx"}],
        })]),
    ])
    loop = _loop(tmp_path, client)

    result = await loop.run("read the file src/App.tsx")

    assert result.awaiting_user is True
    # The correction injected after the failed read named both candidates and
    # told the model to ask the user rather than guess.
    second_round_messages = "\n".join(str(message.get("content", "")) for message in client.messages_seen[1])
    assert "client/src/App.tsx" in second_round_messages
    assert "admin/src/App.tsx" in second_round_messages
    assert "ask_user" in second_round_messages


@pytest.mark.asyncio
async def test_failed_read_with_single_candidate_suggests_exact_path(tmp_path: Path):
    (tmp_path / "client" / "src").mkdir(parents=True)
    (tmp_path / "client" / "src" / "App.tsx").write_text("export default 1\n", encoding="utf-8")

    client = ScriptedClient([
        _message(tool_calls=[_tool_call("read_file", {"filepath": "src/App.tsx"})]),
        _message(tool_calls=[_tool_call("read_file", {"filepath": "client/src/App.tsx"})]),
        _message(content="Read the file.", tool_calls=[]),
    ])
    loop = _loop(tmp_path, client)

    result = await loop.run("read the file src/App.tsx")

    assert result.final == "Read the file."
    correction = "\n".join(str(message.get("content", "")) for message in client.messages_seen[1])
    assert "client/src/App.tsx" in correction
    assert "read_file" in correction


@pytest.mark.asyncio
async def test_prose_only_promise_does_not_end_turn(tmp_path: Path):
    client = ScriptedClient([
        _message(content="I will read app.py next."),
        _message(content="Done."),
    ])
    loop = _loop(tmp_path, client)

    result = await loop.run("look at app.py")

    # The empty promise must not be the final answer; the loop injects a
    # correction and runs one more round.
    assert result.final == "Done."
    assert len(client.messages_seen) == 2
    correction = "\n".join(str(message.get("content", "")) for message in client.messages_seen[1])
    assert "did not call a tool" in correction


@pytest.mark.asyncio
async def test_failed_edit_promise_uses_grounded_mutation_recovery_first(tmp_path: Path):
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedClient([
        _message(
            tool_calls=[
                _tool_call(
                    "edit_file",
                    {"filepath": "app.py", "old_string": "", "new_string": "VALUE = 2\n"},
                )
            ]
        ),
        _message(content="I will correct app.py now."),
        _message(
            tool_calls=[
                _tool_call(
                    "write_file",
                    {"filepath": "app.py", "content": "VALUE = 2\n"},
                )
            ]
        ),
        _message(content="Fixed and verified."),
    ])
    loop = _loop(tmp_path, client)

    result = await loop.run("fix app.py so VALUE is 2")

    assert result.final.startswith("Fixed and verified.")
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    recovery = "\n".join(
        str(message.get("content", "")) for message in client.messages_seen[2]
    )
    assert "required file mutation is still unconfirmed" in recovery
    assert "app.py" in recovery


@pytest.mark.asyncio
async def test_long_running_loop_allows_diagnostic_reads_before_required_edit(tmp_path: Path):
    (tmp_path / "test_app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("EXPECTED = 2\n", encoding="utf-8")
    client = ScriptedClient([
        _message(tool_calls=[_tool_call("read_file", {"filepath": "test_app.py"})]),
        _message(content="The assertion is stale; I need to inspect the source."),
        _message(tool_calls=[_tool_call("read_file", {"filepath": "app.py"})]),
        _message(content="The source confirms the expected value should be 2."),
        _message(tool_calls=[_tool_call("read_file", {"filepath": "app.py"})]),
        _message(
            tool_calls=[
                _tool_call(
                    "edit_file",
                    {
                        "filepath": "test_app.py",
                        "old_string": "VALUE = 1",
                        "new_string": "VALUE = 2",
                    },
                )
            ]
        ),
        _message(content="Updated the focused test."),
    ])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=tools,
        llm=NoPlanLLM(),
        long_running=True,
        verify_changes=False,
    )

    result = await loop.run("fix test_app.py using app.py through the ReAct tool loop")

    assert result.stopped is False
    assert result.changed_files == ("test_app.py",)
    assert (tmp_path / "test_app.py").read_text(encoding="utf-8") == "VALUE = 2\n"


@pytest.mark.asyncio
async def test_scoped_repair_handoff_recovers_after_reads_without_mutation(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "test_app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("EXPECTED = 2\n", encoding="utf-8")
    client = ScriptedClient([
        _message(tool_calls=[_tool_call("read_file", {"filepath": "test_app.py"})]),
        _message(content=""),
    ])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    tools.set_allowed_write_paths(["test_app.py"])
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=tools,
        llm=NoPlanLLM(),
        long_running=True,
        verify_changes=False,
    )
    repair_calls: list[list[str]] = []

    async def _repair(targets: list[str]):
        repair_calls.append(targets)
        return VerifyOutcome(
            verified=True,
            exit_code=0,
            command="pytest",
            summary="Verification passed: `pytest` (exit 0).",
        )

    monkeypatch.setattr(loop, "_attempt_repair", _repair)

    result = await loop.run(
        "Fix the failing test_app.py using app.py and rerun the tests through the ReAct loop"
    )

    assert repair_calls == [["test_app.py"]]
    assert result.stopped is False
    assert result.changed_files == ("test_app.py",)
    assert "verified after repair" in result.final


@pytest.mark.asyncio
async def test_repeated_read_in_scoped_repair_hands_off_instead_of_looping(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "test_app.py").write_text("VALUE = 1\n", encoding="utf-8")
    client = ScriptedClient([
        _message(tool_calls=[_tool_call("read_file", {"filepath": "test_app.py"})]),
        _message(tool_calls=[_tool_call("read_file", {"filepath": "test_app.py"})]),
    ])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    tools.set_allowed_write_paths(["test_app.py"])
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=tools,
        llm=NoPlanLLM(),
        long_running=True,
        verify_changes=False,
    )

    async def _repair(targets: list[str]):
        return VerifyOutcome(
            verified=True,
            exit_code=0,
            command="pytest",
            summary="Verification passed.",
        )

    monkeypatch.setattr(loop, "_attempt_repair", _repair)

    result = await loop.run("Fix the failing test_app.py and rerun tests")

    assert result.stopped is False
    assert result.changed_files == ("test_app.py",)
    assert len(client.messages_seen) == 2


@pytest.mark.asyncio
async def test_trace_callback_receives_clarification_event(tmp_path: Path):
    events: list[tuple[str, str]] = []

    def on_trace(event_type, message, payload=None, level="normal"):
        events.append((event_type, message))

    client = ScriptedClient([
        _message(tool_calls=[_tool_call("ask_user", {"question": "Which one?"})]),
    ])
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    loop = AgentChatLoop(tmp_path, client=client, tools=tools, llm=NoPlanLLM(), on_trace=on_trace)

    await loop.run("do the thing")

    assert any(event_type == "clarification.needed" for event_type, _ in events)


# ---------------------------------------------------------------------------
# 2026-08-01 dogfood: 7B coders promise an edit, show the finished file in a
# fence, and never emit the tool call. Reads had a salvager; mutations didn't.
# ---------------------------------------------------------------------------

def test_promised_write_salvage_creates_a_missing_file(tmp_path: Path):
    call = _promised_write_tool_call(
        "I will now create `core/models.py` with the Board model:\n"
        "```python\nfrom django.db import models\n\n"
        "class Board(models.Model):\n    name = models.CharField(max_length=80)\n```",
        tmp_path,
    )

    assert call is not None
    assert call["function"]["name"] == "write_file"
    assert call["function"]["arguments"]["filepath"] == "core/models.py"
    assert "class Board" in call["function"]["arguments"]["content"]


def test_promised_write_salvage_accepts_a_full_rewrite_of_an_existing_file(tmp_path: Path):
    target = tmp_path / "models.py"
    target.write_text(
        "from django.db import models\n\n# Define your models here.\n", encoding="utf-8"
    )

    call = _promised_write_tool_call(
        "I'll update `models.py` with the required entities:\n"
        "```python\nfrom django.db import models\n\n"
        "class Board(models.Model):\n    name = models.CharField(max_length=80)\n\n"
        "class Card(models.Model):\n    title = models.CharField(max_length=200)\n```",
        tmp_path,
    )

    assert call is not None
    assert call["function"]["arguments"]["filepath"] == "models.py"


def test_promised_write_salvage_rejects_a_snippet_for_an_existing_file(tmp_path: Path):
    target = tmp_path / "models.py"
    target.write_text(
        "from django.db import models\n\nclass Board(models.Model):\n"
        "    name = models.CharField(max_length=80)\n\nclass Card(models.Model):\n"
        "    title = models.CharField(max_length=200)\n",
        encoding="utf-8",
    )

    call = _promised_write_tool_call(
        "I'll edit `models.py` to add the field:\n"
        "```python\n    due_date = models.DateTimeField(null=True)\n```",
        tmp_path,
    )

    assert call is None


def test_promised_write_salvage_rejects_ambiguous_multi_file_promises(tmp_path: Path):
    call = _promised_write_tool_call(
        "I'll update `models.py` and `views.py`:\n```python\nx = 1\n```",
        tmp_path,
    )

    assert call is None


@pytest.mark.asyncio
async def test_prose_only_write_promise_is_salvaged_into_a_real_write(tmp_path: Path):
    client = ScriptedClient(
        [
            _message(
                content=(
                    "I will create `notes.py` now:\n```python\nVALUE = 1\n```"
                )
            ),
            _message(content="Done."),
        ]
    )
    loop = _loop(tmp_path, client)

    result = await loop.run("create notes.py with VALUE = 1")

    assert (tmp_path / "notes.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert result.changed_files == ("notes.py",)


@pytest.mark.asyncio
async def test_pending_question_resumes_from_the_clean_user_request(tmp_path: Path):
    """When the loop runs under an internal contract (composite step, PRD
    repair), the pending question must anchor to the CLEAN user request - the
    wrapper text re-routed as a fresh prompt loses the mutation intent."""
    logger = SessionManager(tmp_path).create_session("Ask")
    client = ScriptedClient([
        _message(tool_calls=[_tool_call("ask_user", {"question": "Which port?"})]),
    ])
    tools = AgentToolRegistry(
        tmp_path, approval_func=lambda _request: True, session_logger=logger
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=tools,
        llm=NoPlanLLM(),
        session_logger=logger,
        original_user_request="fix the server port in app.py",
    )

    result = await loop.run("Original request: fix the server port\n\nDo ONLY step 2 now.")

    assert result.awaiting_user is True
    pending = logger.get_pending_question()
    assert pending["created_from_prompt"] == "fix the server port in app.py"


def test_planner_question_about_a_workspace_document_is_not_asked(tmp_path: Path):
    """Live 2026-08-02: a plan request ended with ZERO tool calls because the
    planner asked "What is the main purpose of canvas lite.pdf?" - about a PDF
    sitting in the workspace. That is research the agent does with one
    read-only call, not a decision only the user can make."""
    from shamsu.agents.chat_loop import _question_is_answerable_by_reading

    (tmp_path / "canvas lite.pdf").write_bytes(b"%PDF-1.4 fixture")

    assert _question_is_answerable_by_reading(
        "What is the main purpose of the canvas lite.pdf document?", tmp_path
    ) is True


def test_a_genuine_user_decision_is_still_asked(tmp_path: Path):
    from shamsu.agents.chat_loop import _question_is_answerable_by_reading

    (tmp_path / "canvas lite.pdf").write_bytes(b"%PDF-1.4 fixture")

    # A choice between alternatives stays the user's call even when it names a file.
    assert _question_is_answerable_by_reading(
        "Should I use session auth or JWT for canvas lite.pdf's login flow?", tmp_path
    ) is False
    # A question about a file that does not exist cannot be answered by reading.
    assert _question_is_answerable_by_reading(
        "What is the purpose of missing-spec.pdf?", tmp_path
    ) is False
    # A pure product decision with no document reference.
    assert _question_is_answerable_by_reading(
        "What should the grading scale be?", tmp_path
    ) is False


def test_content_question_is_suppressed_when_the_request_names_the_document(tmp_path: Path):
    """The document is often named only in the REQUEST, not the question: live
    2026-08-02 the planner asked "What is the primary purpose of the Canvas LMS
    Lite app?" for a request pointing at canvas lite.pdf, and the turn ended
    with zero tool calls."""
    from shamsu.agents.chat_loop import _question_is_answerable_by_reading

    (tmp_path / "canvas lite.pdf").write_bytes(b"%PDF-1.4 fixture")

    assert _question_is_answerable_by_reading(
        "What is the primary purpose of the Canvas LMS Lite app?",
        tmp_path,
        "plan a Canvas LMS Lite app based on @canvas lite.pdf",
    ) is True
    # With no document anywhere, it remains a real question for the user.
    assert _question_is_answerable_by_reading(
        "What is the primary purpose of the Canvas LMS Lite app?",
        tmp_path,
        "plan a Canvas LMS Lite app",
    ) is False


def test_permission_question_for_an_explicitly_requested_file_is_declined():
    """Live 2026-08-02: "Create the file canvas_lms_lite/core/models.py..." got
    "The target file does not exist. Should I create it now?" and the turn ended
    in 2.8s having written nothing. The request already answered that."""
    from shamsu.agents.chat_loop import _asks_permission_already_granted

    request = "Create the file canvas_lms_lite/core/models.py with a custom user model."

    assert _asks_permission_already_granted(
        "The target file 'canvas_lms_lite/core/models.py' does not exist. Should I create it now?",
        request,
    ) is True


def test_a_real_design_choice_is_still_asked():
    from shamsu.agents.chat_loop import _asks_permission_already_granted

    request = "Create the file core/auth.py implementing login."

    # A genuine either/or is not a permission question.
    assert _asks_permission_already_granted(
        "Should I use session authentication or JWT for core/auth.py?", request
    ) is False
    # Permission about a file the request never named stays a real question.
    assert _asks_permission_already_granted(
        "Should I create config/production_settings.py?", request
    ) is False


def test_location_question_for_an_explicit_path_is_declined():
    """Live 2026-08-02: "Create canvas_lms_lite/config/settings.py..." got
    "Where should the settings.py file be created?" and the turn ended in 3.4s
    having written nothing. The path is in the request."""
    from shamsu.agents.chat_loop import _asks_permission_already_granted

    request = "Create canvas_lms_lite/config/settings.py for the Django project."

    assert _asks_permission_already_granted(
        "Where should the `settings.py` file be created?", request
    ) is True
    assert _asks_permission_already_granted(
        "Which directory should settings.py go in?", request
    ) is True


def test_location_question_for_a_bare_filename_is_still_asked():
    """With no directory stated, where the file goes is genuinely unknown."""
    from shamsu.agents.chat_loop import _asks_permission_already_granted

    assert _asks_permission_already_granted(
        "Where should settings.py be created?", "Create settings.py for the project."
    ) is False


def test_truncated_write_is_detected_and_asks_for_the_remainder(tmp_path: Path):
    """A successful write is not a finished file: live 2026-08-02 a 7B produced
    a manage.py with an unterminated string literal and the turn ended looking
    like success."""
    from shamsu.agents.chat_loop import _truncated_write_correction

    (tmp_path / "manage.py").write_text(
        '#!/usr/bin/env python\nimport os\n\n\ndef main():\n'
        '    raise ImportError(\n        "Could not import Django. Are you sure\n',
        encoding="utf-8",
    )

    correction = _truncated_write_correction(tmp_path, "manage.py")

    assert "STOPS PART-WAY THROUGH" in correction
    assert "append_file" in correction
    assert "Do not re-send the whole file" in correction
    assert "current end of file" in correction


def test_a_complete_file_is_not_treated_as_truncated(tmp_path: Path):
    from shamsu.agents.chat_loop import _truncated_write_correction

    (tmp_path / "models.py").write_text(
        "from django.db import models\n\n\nclass User(models.Model):\n    name = models.CharField(max_length=20)\n",
        encoding="utf-8",
    )

    assert _truncated_write_correction(tmp_path, "models.py") == ""


def test_an_ordinary_syntax_typo_is_not_treated_as_truncation(tmp_path: Path):
    """Only cut-off-shaped errors qualify; a plain typo is a different problem
    and already has its own correction path."""
    from shamsu.agents.chat_loop import _truncated_write_correction

    (tmp_path / "bad.py").write_text("def f(:\n    pass\n", encoding="utf-8")

    assert _truncated_write_correction(tmp_path, "bad.py") == ""


def test_non_python_and_missing_files_are_skipped(tmp_path: Path):
    from shamsu.agents.chat_loop import _truncated_write_correction

    (tmp_path / "page.html").write_text("<html><body>", encoding="utf-8")

    assert _truncated_write_correction(tmp_path, "page.html") == ""
    assert _truncated_write_correction(tmp_path, "missing.py") == ""
    assert _truncated_write_correction(tmp_path, "") == ""


@pytest.mark.asyncio
async def test_loop_recovers_a_truncated_write_instead_of_ending_on_it(tmp_path: Path):
    """End to end: the write succeeds but the file is cut off, so the loop must
    ask for the remainder rather than treating the turn as done."""
    truncated = 'import os\n\n\ndef main():\n    raise ImportError(\n        "cut off here\n'
    remainder = '        )\n\n\nif __name__ == "__main__":\n    main()\n'
    client = ScriptedClient(
        [
            _message(tool_calls=[_tool_call("write_file", {"filepath": "manage.py", "content": truncated})]),
            _message(tool_calls=[_tool_call("append_file", {"filepath": "manage.py", "content": remainder})]),
            _message(content="Finished manage.py."),
        ]
    )
    loop = _loop(tmp_path, client)

    result = await loop.run("create manage.py")

    # The loop pushed a correction round rather than stopping at the truncation.
    corrections = [
        message.content
        for message in loop.state.all_messages
        if message.role == "user" and "STOPS PART-WAY THROUGH" in str(message.content)
    ]
    assert corrections, "expected a truncation correction to be injected"
    assert result.final.startswith("Finished manage.py.")
