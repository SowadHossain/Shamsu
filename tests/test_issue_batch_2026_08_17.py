"""Fixes for the second reported issue batch (2026-08-17).

Each test names the observed failure rather than the mechanism, so a future
refactor that reintroduces the behaviour fails loudly.
"""
from __future__ import annotations

import tempfile
from io import StringIO
from pathlib import Path

from rich.console import Console

import shamsu.cli.repl as repl
from shamsu.plans.store import PLAN_NO_STEPS_MARKER, parse_plan_steps, plan_has_no_steps
from shamsu.session.memory import is_affirmative, is_negative, strip_filler_prefix


# --- #5: "okay proceed" was answered as a fresh question ----------------------


def test_a_filler_word_no_longer_breaks_a_continuation():
    for reply in ("okay proceed", "alright go ahead", "okay lets continue", "so continue"):
        assert repl._looks_like_prd_slice_execution_reply(reply), reply
        assert is_affirmative(reply), reply


def test_bare_affirmatives_still_work_and_are_not_stripped_to_nothing():
    for reply in ("okay", "ok", "yes", "proceed", "continue", "next"):
        assert is_affirmative(reply), reply


def test_a_real_sentence_starting_with_filler_is_not_a_bare_confirmation():
    assert not is_affirmative("so continue the build and add tests for it")
    assert not repl._looks_like_prd_slice_execution_reply("okay now explain how routing works")


def test_a_filler_prefixed_refusal_is_still_a_refusal():
    assert is_negative("okay no")
    assert is_negative("alright cancel")


def test_strip_filler_prefix_never_empties_the_reply():
    assert strip_filler_prefix("okay") == "okay"
    assert strip_filler_prefix("okay proceed") == "proceed"


# --- #3: the "no steps" placeholder was executed as real work -----------------


def test_an_empty_plan_marker_is_not_parsed_back_out_as_a_step():
    markdown = f"# Plan: X\n\n## Steps\n{PLAN_NO_STEPS_MARKER}\n\n## Verification\nRun tests.\n"

    assert parse_plan_steps(markdown) == []
    assert plan_has_no_steps(markdown)


def test_a_plan_with_real_steps_is_not_flagged_empty():
    markdown = "# Plan: X\n\n## Steps\n1. Add the model\n2. Wire the view\n"

    assert parse_plan_steps(markdown) == ["Add the model", "Wire the view"]
    assert not plan_has_no_steps(markdown)


def test_executing_an_empty_plan_is_refused_not_guessed_at():
    import asyncio

    console = Console(record=True, width=100)
    markdown = f"# Plan: X\n\n## Steps\n{PLAN_NO_STEPS_MARKER}\n"

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(
            repl._execute_plan("do the thing", "code_edit", markdown, [], Path(tmp), console)
        )

    out = console.export_text()
    assert "Nothing To Execute" in out
    assert "did not produce any" in out


# --- #4: an unqualified "implement X" was answered in chat --------------------


def test_naming_project_types_in_a_live_workspace_is_an_edit_not_a_chat_answer(tmp_path):
    (tmp_path / "game.py").write_text("class Asteroid:\n    pass\n", encoding="utf-8")

    assert not repl._looks_like_direct_code_request(
        "Implement Spaceship and Bullet classes", tmp_path
    )


def test_a_self_contained_coding_question_is_still_answered_directly(tmp_path):
    (tmp_path / "game.py").write_text("class Asteroid:\n    pass\n", encoding="utf-8")

    assert repl._looks_like_direct_code_request(
        "write a Python function to reverse a string", tmp_path
    )
    assert repl._looks_like_direct_code_request("implement a binary search function", tmp_path)


# --- #2: planner JSON was discarded with no explanation ----------------------


def test_a_planner_parse_failure_says_why_instead_of_reporting_no_steps():
    from shamsu.agents.plan_mode import _loads_with_reason

    # Every unusable response must carry a reason; "no steps were produced" on
    # its own made a parse failure and an empty plan indistinguishable.
    assert _loads_with_reason("")[1] == "planner returned an empty response"
    assert "expected a JSON object" in _loads_with_reason('"just a string"')[1]
    for unusable in ("I think we should start by...", "```\nnot json\n```", "{{{"):
        data, reason = _loads_with_reason(unusable)
        assert reason, unusable
        assert not (data or {}).get("steps"), unusable


def test_a_bare_array_of_steps_is_recovered_not_discarded():
    from shamsu.agents.plan_mode import _loads_with_reason, _steps_from_data

    data, reason = _loads_with_reason('[{"description": "add the model"}]')

    assert reason == ""
    assert [step.description for step in _steps_from_data(data)] == ["add the model"]


def test_steps_under_an_alternate_key_are_recovered():
    """A small model answering with `tasks` or `plan` lost the whole plan."""
    from shamsu.agents.plan_mode import _loads_with_reason, _steps_from_data

    for key in ("tasks", "plan", "actions", "plan_steps"):
        data, _ = _loads_with_reason('{"%s": [{"description": "wire the view"}]}' % key)
        assert [s.description for s in _steps_from_data(data)] == ["wire the view"], key


# --- #6: the model declared symbols it had already written to be missing -----


def test_existing_project_symbols_are_named_in_the_frame(tmp_path):
    from shamsu.context.compiler import _defined_symbols

    (tmp_path / "game.py").write_text(
        "class Asteroid:\n    pass\n\nclass Bullet:\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "ui.py").write_text("def render():\n    pass\n", encoding="utf-8")

    brief = _defined_symbols(tmp_path)

    assert "Asteroid" in brief and "Bullet" in brief
    assert "game.py" in brief


def test_files_already_quoted_in_full_are_not_repeated_in_the_symbol_list(tmp_path):
    """RELEVANT SOURCE CODE already carries those files verbatim."""
    from shamsu.context.compiler import _defined_symbols

    (tmp_path / "game.py").write_text("class Asteroid:\n    pass\n", encoding="utf-8")
    (tmp_path / "ui.py").write_text("def render():\n    pass\n", encoding="utf-8")

    brief = _defined_symbols(tmp_path, exclude=("game.py",))

    assert "Asteroid" not in brief
    assert "render" in brief


def test_the_author_phase_frame_carries_the_symbol_section():
    from shamsu.context.compiler import ContextFrame

    frame = ContextFrame(phase="AUTHOR", defined_symbols="game.py: Asteroid, Bullet")

    rendered = frame.render([])

    assert "[ALREADY DEFINED IN THIS PROJECT]" in rendered
    assert "Asteroid" in rendered


# --- context loss: the agent lost the thread after ~6 prompts ----------------


class _NoPlan:
    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        from shamsu.types import LLMResponse

        return LLMResponse(raw="", model_used="fake")


class _Silent:
    async def chat(self, model, messages, tools, stream, options):  # noqa: ANN001
        return {"message": {"content": "ok", "tool_calls": []}}


def _chat_loop(workspace: Path, session_logger):
    from shamsu.agents.chat_loop import AgentChatLoop
    from shamsu.tools.agent_tools import AgentToolRegistry

    return AgentChatLoop(
        workspace,
        client=_Silent(),
        tools=AgentToolRegistry(
            workspace, approval_func=lambda _r: True, session_logger=session_logger
        ),
        llm=_NoPlan(),
        session_logger=session_logger,
    )


def test_the_session_thread_survives_many_prompts(tmp_path):
    """After seven prompts the agent must still know what it has been doing.

    The frame is compiled per model call from a runtime task that is recreated
    every prompt, so without a digest nothing carried intent across turns and
    the thread was lost after a handful of exchanges.
    """
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session("Thread")
    loop = _chat_loop(tmp_path, logger)
    for index in range(7):
        loop.state.append_user(f"step {index}: build part {index}")
        loop.state.append_assistant(f"done part {index}")

    digest = loop._session_history_digest()

    assert "build part 6" in digest
    # Older turns survive too - this is the thread, not just the last exchange.
    assert "build part 2" in digest
    assert "done part 2" in digest


def test_the_first_request_has_no_thread_to_report(tmp_path):
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session("First")
    loop = _chat_loop(tmp_path, logger)
    loop.state.append_user("build a game")

    assert loop._session_history_digest() == ""


def test_the_digest_never_replays_state_frames(tmp_path):
    """Frames are compiled context, not conversation; replaying them is the
    transcript bloat this design replaced."""
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session("Frames")
    loop = _chat_loop(tmp_path, logger)
    loop.state.append_user("real request")
    loop.state.append_assistant("real answer")
    loop.state.append_user("[PHASE]\nAUTHOR\n\n[CURRENT TASK]\ncompiled context, not conversation")
    loop.state.append_user("second real request")

    digest = loop._session_history_digest()

    assert "[PHASE]" not in digest
    assert "compiled context" not in digest
    assert "real request" in digest
    assert "second real request" in digest


def test_the_frame_carries_the_conversation_section():
    from shamsu.context.compiler import ContextFrame

    frame = ContextFrame(phase="AUTHOR", history_digest="- asked: build a game\n  result: done")

    rendered = frame.render([])

    assert "[CONVERSATION SO FAR]" in rendered
    assert "build a game" in rendered


def test_a_larger_window_delivers_whole_files_instead_of_truncated_ones(tmp_path):
    """A file cut mid-construct is how settings.py used BASE_DIR without defining it."""
    from shamsu.context.compiler import ContextCompiler

    (tmp_path / "big.py").write_text("X = 1\n" * 4000, encoding="utf-8")
    compiler = ContextCompiler(workspace_root=tmp_path)

    small = compiler._source_sections(["big.py"], 8192)[0]
    large = compiler._source_sections(["big.py"], 32768)[0]

    assert "[truncated]" in small
    assert "[truncated]" not in large


# --- a git mutation must never become a read --------------------------------


def test_a_push_is_not_silently_turned_into_an_inspect(tmp_path):
    """The git_* catch-all aliased every unmapped git tool to git.inspect, and
    unknown modes default to "overview" - so a push ran a read and reported ok."""
    from shamsu.tools.agent_tools import AgentToolRegistry
    from shamsu.tools.logical import logical_target

    registry = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    registry.use_logical_tools(True)

    for mutation in ("git_push", "git_pull", "git_fetch", "git_checkout", "git_restore"):
        assert registry._logical_tools.alias(mutation, {}) is None, mutation
        assert logical_target(mutation) == "", mutation


def test_git_reads_and_checkpoints_still_alias(tmp_path):
    from shamsu.tools.agent_tools import AgentToolRegistry

    registry = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    registry.use_logical_tools(True)

    assert registry._logical_tools.alias("git_status", {})[0] == "git.inspect"
    assert registry._logical_tools.alias("git_log", {})[0] == "git.inspect"
    assert registry._logical_tools.alias("git_commit", {})[0] == "git.checkpoint"


# --- #1: Ollama's own timings ------------------------------------------------


def test_ollama_timings_are_captured_and_converted_from_nanoseconds():
    from shamsu.agents.chat_loop import _format_ollama_timings, _ollama_timings

    timings = _ollama_timings(
        {
            "done": True,
            "total_duration": 5_200_000_000,
            "load_duration": 1_100_000_000,
            "prompt_eval_duration": 2_400_000_000,
            "eval_duration": 1_600_000_000,
            "prompt_eval_count": 3120,
            "eval_count": 180,
        }
    )

    assert timings["load_duration_ms"] == 1100.0
    assert timings["prompt_eval_duration_ms"] == 2400.0
    assert timings["eval_count"] == 180
    # Separating load / prefill / generate is the whole point: it turns "slower
    # than plain ollama" into a number, and prefill is what grows with context.
    readout = _format_ollama_timings(timings)
    assert "load 1100ms" in readout
    assert "prefill 2400ms" in readout
    assert "tok/s" in readout


def test_a_chunk_without_timings_reports_nothing_rather_than_zeros():
    from shamsu.agents.chat_loop import _ollama_timings

    assert _ollama_timings({"done": True}) == {}


# --- #3: a plan must build the thing that was asked for ----------------------


def test_a_plan_that_drifts_to_another_stack_is_rejected():
    from shamsu.cli.repl import _architecture_conformance_errors

    drifted = {
        "stack": ["python", "django"],
        "milestones": [{"files": ["backend/core/forms.py", "backend/core/urls.py"]}],
    }

    errors = _architecture_conformance_errors(drifted, "build a python asteroid shooter")

    assert errors
    assert "forms.py" in errors[0]


def test_a_plan_matching_the_request_passes():
    from shamsu.cli.repl import _architecture_conformance_errors

    game = {"stack": ["python", "pygame"], "milestones": [{"files": ["game.py", "sprites.py"]}]}

    assert _architecture_conformance_errors(game, "build a python asteroid shooter") == []


def test_a_web_request_may_legitimately_propose_web_files():
    """The guard is about drift, not about banning Django."""
    from shamsu.cli.repl import _architecture_conformance_errors

    web = {
        "stack": ["python", "django"],
        "milestones": [{"files": ["backend/core/urls.py", "backend/core/forms.py"]}],
    }

    assert _architecture_conformance_errors(web, "build a django web app with an admin") == []


def test_a_drifting_plan_fails_validation_outright():
    from shamsu.cli.repl import _validate_prd_development_plan

    candidate = {
        "plan_summary": "Build it",
        "stack": ["django"],
        "milestones": [
            {"id": "M-001", "title": "Forms", "goal": "Add forms", "files": ["app/forms.py"]}
        ],
    }

    try:
        _validate_prd_development_plan(candidate, "build a python asteroid shooter")
    except ValueError as exc:
        assert "never asks for that" in str(exc)
    else:
        raise AssertionError("a drifting plan must not validate")


# --- the planner could not finish in the time it was given -------------------


def test_a_reasoning_planner_gets_time_to_think(monkeypatch):
    """30s predates reasoning planners and was only survivable while a timeout
    fell back to compiled milestones. With the fallback correctly gone, the same
    30s turns a slow-but-working planner into a hard failure."""
    from shamsu.cli.repl import _prd_plan_num_predict, _prd_plan_timeout_seconds

    monkeypatch.delenv("SHAMSU_PRD_PLAN_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("SHAMSU_PRD_PLAN_NUM_PREDICT", raising=False)

    # A reasoning 9B can spend 15-20s reaching first token, then think.
    assert _prd_plan_timeout_seconds(True) >= 300
    assert _prd_plan_timeout_seconds(False) >= 120
    # Thinking tokens are generated tokens: budget for the chain of thought AND
    # the JSON, or the model runs out before it can emit the answer.
    assert _prd_plan_num_predict(True) > _prd_plan_num_predict(False)


def test_the_planner_timeout_is_still_tunable(monkeypatch):
    from shamsu.cli.repl import _prd_plan_timeout_seconds

    monkeypatch.setenv("SHAMSU_PRD_PLAN_TIMEOUT_SECONDS", "45")
    assert _prd_plan_timeout_seconds(True) == 45.0

    monkeypatch.setenv("SHAMSU_PRD_PLAN_TIMEOUT_SECONDS", "not a number")
    assert _prd_plan_timeout_seconds(True) >= 300


def test_planner_selection_matches_what_the_run_will_actually_do():
    """The budget is chosen from whether the planner THINKS, so it has to agree
    with the gate the model call itself uses."""
    from shamsu.runtime.models import model_for_role, role_should_think

    planner = model_for_role("planner")
    assert isinstance(role_should_think("planner", planner), bool)


# --- one session per workspace, forever ---------------------------------------


def test_a_restart_resumes_a_recent_session(tmp_path):
    from shamsu.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    first, _ = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    again, reason = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)

    assert again.session_id == first.session_id
    assert reason == "resumed"


def test_an_old_session_is_resumed_but_its_age_is_announced(tmp_path):
    """Superseded 2026-08-18. The original rule forked a NEW session past 8
    hours, because "a stale session feeds unrelated work into every prompt".
    That concern is real but the cure was worse: coming back the next morning
    read as "everything I did yesterday is gone", though the transcript was
    intact on disk the whole time.

    It is now answered differently - resume, and SAY how old it is, so the
    choice to carry on is the user's and never silent. Staleness of file
    CONTENT is handled by re-grounding (`files_changed_since_last_activity`),
    and length by compaction, which now persists.
    """
    from shamsu.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    first, _ = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    # Age the session deterministically rather than racing a real clock.
    first.metadata.updated_at = "2020-01-01T00:00:00+00:00"
    manager._write_metadata(first.metadata)
    manager._upsert_index(first.metadata)

    again, reason = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)

    assert again.session_id == first.session_id, "an old thread must still be resumable"
    assert "days ago" in reason, f"its age must be announced, got {reason!r}"


def test_a_long_session_is_not_cut_off(tmp_path):
    """Length is handled by compaction, not by abandoning the thread."""
    from shamsu.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    first, _ = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    for index in range(5):
        first.log("chat.message", {"role": "user", "content": f"turn {index}"}, "turn")

    again, reason = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=3)

    assert again.session_id == first.session_id
    assert reason == "resumed"


def test_an_empty_session_is_not_treated_as_overlong(tmp_path):
    from shamsu.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    first, _ = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    again, reason = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=1)

    assert again.session_id == first.session_id
    assert reason == "resumed"


def test_a_new_session_can_start_without_ending_the_current_one(tmp_path):
    """`close` was the only route to a fresh session from inside the REPL, so
    starting new work meant ending work you wanted to keep."""
    from shamsu.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    current = manager.create_session("Asteroid build")
    current.log("chat.message", {"role": "user", "content": "keep me"}, "turn")
    console = Console(record=True, width=100)

    fresh = repl._handle_sessions("/sessions new Refactor pass", manager, current, console)

    assert fresh.session_id != current.session_id
    assert fresh.metadata.title == "Refactor pass"
    # The point: the previous session is still there and still open.
    statuses = {item.session_id: item.status for item in manager.list_sessions()}
    assert statuses[current.session_id] == "active"
    assert len(statuses) == 2
    # And the user is told how to get back to it.
    assert current.session_id in console.export_text()


def test_the_previous_session_can_be_resumed_after_starting_a_new_one(tmp_path):
    from shamsu.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    current = manager.create_session("First")
    console = Console(record=True, width=100)

    fresh = repl._handle_sessions("/sessions new Second", manager, current, console)
    back = repl._handle_sessions(
        f"/sessions resume {current.session_id}", manager, fresh, console
    )

    assert back.session_id == current.session_id
