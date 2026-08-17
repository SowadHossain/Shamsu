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


def test_a_restart_does_not_resume_a_stale_session(tmp_path):
    """`get_or_create_latest` had no age bound, so every restart landed back in
    the same session forever - and the compiled frame now digests that
    transcript, so a stale session feeds unrelated work into every prompt."""
    from shamsu.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    first, _ = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    # Age the session deterministically rather than racing a real clock.
    first.metadata.updated_at = "2020-01-01T00:00:00+00:00"
    manager._write_metadata(first.metadata)
    manager._upsert_index(first.metadata)

    fresh, reason = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)

    assert fresh.session_id != first.session_id
    assert "stale" in reason


def test_a_restart_does_not_resume_an_overlong_session(tmp_path):
    from shamsu.session.manager import SessionManager

    manager = SessionManager(tmp_path)
    first, _ = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    for index in range(5):
        first.log("chat.message", {"role": "user", "content": f"turn {index}"}, "turn")

    fresh, reason = manager.resume_or_start(max_age_seconds=8 * 3600, max_messages=3)

    assert fresh.session_id != first.session_id
    assert "messages" in reason


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


# --- The planner answered in its reasoning channel and the plan was discarded --
#
# Live 2026-08-17: `qwen3.5:9b-q4_K_M` was handed `think: true` AND a `format`
# schema. Ollama constrains only the `response` stream, so the model wrote the
# whole plan into `thinking`, emitted an empty `response`, and the run died on
# "planner did not return a JSON object" - describing as a failure a model that
# had produced a complete, valid plan.


_PLAN_JSON = (
    '{"plan_summary": "Browser-Based 3D Asteroid Shooter", '
    '"stack": ["node", "three.js"], '
    '"milestones": [{"id": "M-002", "title": "Scaffold", "goal": "Create the project"}]}'
)


def _manager():
    from shamsu.llm.manager import LLMManager

    return LLMManager(base_url="http://localhost:11434")


def _structured(manager, streams):
    """Run generate_structured against a scripted list of (text, thinking) pairs."""
    import asyncio

    calls: list[dict] = []

    async def fake_stream(model, payload, on_token=None, on_progress=None, role="", force_no_think=False):
        calls.append({"payload": payload, "force_no_think": force_no_think})
        text, thinking = streams[len(calls) - 1]
        if thinking and on_progress:
            on_progress("thinking")
        if text and on_token:
            on_token(text)
        return text, thinking, 0

    manager._stream_completion = fake_stream  # type: ignore[assignment]
    raw = asyncio.run(
        manager.generate_structured(
            "planner", "SYSTEM", "PROMPT", {"type": "object"}, num_predict=3072
        )
    )
    return raw, calls


def test_a_plan_written_to_the_reasoning_channel_is_recovered():
    import json

    raw, calls = _structured(_manager(), [("", _PLAN_JSON)])

    assert json.loads(raw)["plan_summary"] == "Browser-Based 3D Asteroid Shooter"
    assert len(calls) == 1, "salvage must not cost a second generation"


def test_reasoning_prose_around_the_object_does_not_defeat_recovery():
    import json

    thinking = f"Okay, the user wants a game. Let me plan.\n{_PLAN_JSON}\nThat covers it."
    raw, _ = _structured(_manager(), [("", thinking)])

    assert json.loads(raw)["stack"] == ["node", "three.js"]


def test_an_empty_answer_with_no_salvageable_reasoning_retries_without_thinking():
    """Thinking is what starved the answer channel, so the retry must drop it."""
    import json

    raw, calls = _structured(
        _manager(),
        [("", "I am still considering the options."), (_PLAN_JSON, "")],
    )

    assert json.loads(raw)["plan_summary"] == "Browser-Based 3D Asteroid Shooter"
    assert len(calls) == 2
    assert calls[0]["force_no_think"] is False
    assert calls[1]["force_no_think"] is True


def test_a_good_answer_channel_is_never_second_guessed():
    raw, calls = _structured(_manager(), [(_PLAN_JSON, "some stray reasoning")])

    assert raw == _PLAN_JSON
    assert len(calls) == 1


def test_a_structured_prompt_gets_a_context_window_that_fits_it():
    """8192 was hardcoded, so a big PRD payload was truncated - and Ollama drops
    the OLDEST tokens, which is the system prompt telling it to answer the schema."""
    from shamsu.llm.manager import STRUCTURED_MAX_CTX, _structured_num_ctx

    small = _structured_num_ctx("hello", 1400)
    large = _structured_num_ctx("x" * 200_000, 3072)

    assert small == 8192, "a tiny prompt must not pay for a 32k KV cache"
    assert large > 8192
    assert large <= STRUCTURED_MAX_CTX


def test_the_structured_context_window_reaches_the_prompt_it_is_given():
    _, calls = _structured(_manager(), [(_PLAN_JSON, "")])

    assert calls[0]["payload"]["options"]["num_ctx"] >= 8192


def test_the_failure_message_no_longer_blames_the_model_for_bad_json():
    """When the model truly produces nothing, the panel must say THAT - the old
    "did not return a JSON object" sent the user hunting a parsing bug."""
    import inspect

    source = inspect.getsource(repl._prepare_prd_development_plan)

    assert "produced no JSON at all" in source
    assert "empty answer channel" in source


def test_json_object_from_text_refuses_to_invent_an_object_from_prose():
    from shamsu.llm.output import json_object_from_text

    assert json_object_from_text("The plan could not be built.") == ""
    assert json_object_from_text("") == ""
    assert json_object_from_text("{}") == ""


def test_json_object_from_text_prefers_the_largest_object():
    """Reasoning text carries fragments alongside the real answer."""
    from shamsu.llm.output import json_object_from_text

    got = json_object_from_text('{"id": "M-1"} ... and finally ' + _PLAN_JSON)

    assert "plan_summary" in got


def test_a_truncated_answer_also_earns_the_no_thinking_retry():
    """A chain of thought that eats the num_predict budget cuts the JSON off
    mid-object as often as it starves it entirely; both are the same failure."""
    import json

    truncated = '{"plan_summary": "Browser-Based 3D Aste'
    raw, calls = _structured(
        _manager(), [(truncated, "long reasoning, no object"), (_PLAN_JSON, "")]
    )

    assert json.loads(raw)["plan_summary"] == "Browser-Based 3D Asteroid Shooter"
    assert len(calls) == 2
    assert calls[1]["force_no_think"] is True


# --- The agent stopped creating files (live trace, 2026-08-17 evening) --------
#
# "yes please proceed and make these base files first" ended with
# "Agent stopped before completing all requested work" and nothing written. Four
# separate defects conspired; each gets a test so none can come back quietly.


def test_a_model_the_cookbook_never_heard_of_still_gets_its_real_context_window():
    """`qwen3.5:9b` matched no cookbook entry and fell back to 8192, so the agent
    ran at "ctx chat 3.8k/8.2k 100%" and could not hold the plan or the spec."""
    from shamsu.context.budget import ctx_window_for_model

    assert ctx_window_for_model("qwen3.5:9b-q4_K_M") == 32_768
    assert ctx_window_for_model("qwen3:8b") == 32_768
    assert ctx_window_for_model("gemma3:12b") == 131_072
    # An unrecognised family stays conservative - this is a rescue, not a blanket raise.
    assert ctx_window_for_model("some-vendor-model:1b") == 8_192


def test_read_only_git_no_longer_interrupts_the_agent_for_approval():
    """project.inspect runs `git branch --show-current`; it was classified
    "medium risk or unknown" and stopped the run for a manual y/n. Twice."""
    from shamsu.types import CommandRisk
    from shamsu.safety.commands import classify_command

    for command in (
        "git branch --show-current",
        "git rev-parse --is-inside-work-tree",
        "git status --short",
        "git remote -v",
        "git config --get user.name",
        "git blame src/app.py",
    ):
        assert classify_command(command) == CommandRisk.SAFE, command


def test_git_commands_that_change_the_repo_still_need_approval():
    from shamsu.types import CommandRisk
    from shamsu.safety.commands import classify_command

    for command in (
        "git branch -D main",        # deletes a branch
        "git branch feature",        # creates one
        "git remote add origin url",
        "git config user.name bob",  # writes config
        "git checkout -b topic",
        "git tag v1.0",
    ):
        assert classify_command(command) != CommandRisk.SAFE, command


def test_the_same_call_with_flipped_path_separators_counts_as_a_repeat():
    r"""The agent ran project.inspect twice on one path - `F:/Work/asteroid` then
    `F:\Work\asteroid` - and repeat detection saw two different calls."""
    from shamsu.agents.chat_loop import _call_signature

    forward = _call_signature("project.inspect", {"path": "F:/Work/asteroid", "include_git": True})
    backward = _call_signature("project.inspect", {"path": r"F:\Work\asteroid", "include_git": True})
    trailing = _call_signature("project.inspect", {"path": "F:/Work/asteroid/", "include_git": True})
    other = _call_signature("project.inspect", {"path": "F:/Work/other", "include_git": True})

    assert forward == backward
    assert forward == trailing
    assert forward != other, "genuinely different targets must stay distinguishable"


def test_a_creation_request_is_not_a_repair_request():
    """The read-saturation guard handed "make these base files" to the strict
    REPAIR loop, which had nothing to repair, found no verifier for a markdown
    spec, and ended the run UNCONFIRMED with no file written."""
    from shamsu.agents.chat_loop import _request_is_verification_repair

    assert not _request_is_verification_repair("yes please proceed and make these base files first")
    assert not _request_is_verification_repair("create the project structure")


def test_the_read_saturation_guard_only_hands_off_a_repair_request():
    import inspect

    from shamsu.agents.chat_loop import AgentChatLoop

    source = inspect.getsource(AgentChatLoop)
    guard = source.split("Skipped redundant read_file")[1][:1200]

    assert "_request_is_verification_repair" in guard, (
        "the creation path must fall through to the mutation instruction, "
        "not into the repair loop"
    )


def test_an_approval_inside_the_agent_loop_does_not_orphan_a_coroutine():
    """"RuntimeWarning: coroutine 'Application.run_async' was never awaited" fired
    on every approval raised from the running agent loop."""
    import asyncio
    import warnings

    from shamsu.safety.approval import _prompt_toolkit_answer

    async def ask_while_a_loop_is_running():
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            answer = _prompt_toolkit_answer()
        return answer, [str(w.message) for w in caught]

    answer, messages = asyncio.run(ask_while_a_loop_is_running())

    assert answer is None, "must decline, so the caller falls back to a usable reader"
    assert not any("never awaited" in message for message in messages), messages


# --- Context window and response limit raised ---------------------------------


def test_the_real_context_window_is_read_from_the_model_not_a_hardcoded_table():
    """The cookbook said qwen3:8b was 32768; the model itself declares 40960.
    A table can only ever be stale, so ground truth wins when it is reachable."""
    from shamsu.context import budget

    probed: list[str] = []

    def fake_probe(model_name: str) -> int:
        probed.append(model_name)
        return 262_144

    original = budget._declared_ctx_window
    budget._declared_ctx_window = fake_probe
    try:
        assert budget.ctx_window_for_model("qwen3.5:9b-q4_K_M") == 262_144
        # ...even for a model the cookbook DOES list with a smaller number.
        assert budget.ctx_window_for_model("qwen3:8b") == 262_144
    finally:
        budget._declared_ctx_window = original

    assert probed, "the model must actually be asked"


def test_an_unreachable_ollama_falls_back_to_the_static_table():
    """The probe returns 0 offline; sizing must not collapse to the 8192 default
    for a model the cookbook or a family pattern already covers."""
    from shamsu.context import budget

    original = budget._declared_ctx_window
    budget._declared_ctx_window = lambda _name: 0
    try:
        assert budget.ctx_window_for_model("qwen3:8b") == 32_768
        assert budget.ctx_window_for_model("qwen3.5:9b-q4_K_M") == 32_768
        assert budget.ctx_window_for_model("nothing-known:1b") == 8_192
    finally:
        budget._declared_ctx_window = original


def test_an_explicit_override_beats_everything():
    from shamsu.context import budget

    original = budget._declared_ctx_window
    budget._declared_ctx_window = lambda _name: 262_144
    try:
        with_env = {"SHAMSU_MODEL_CTX_WINDOW": "12288"}
        import os

        os.environ.update(with_env)
        try:
            assert budget.ctx_window_for_model("qwen3:8b") == 12_288
        finally:
            os.environ.pop("SHAMSU_MODEL_CTX_WINDOW", None)
    finally:
        budget._declared_ctx_window = original


def test_the_probe_is_disabled_in_tests_so_sizing_is_deterministic():
    """Otherwise the suite's results depend on which models the machine has."""
    from shamsu.runtime.ollama import declared_context_length

    assert declared_context_length("qwen3:8b") == 0


def test_the_window_and_response_reserve_were_actually_raised():
    from shamsu.agents.chat_loop import _CHAT_MAX_CTX
    from shamsu.context.budget import RESERVE_OUTPUT_TOKENS
    from shamsu.llm.manager import STRUCTURED_MAX_CTX

    assert _CHAT_MAX_CTX >= 32_768
    assert STRUCTURED_MAX_CTX >= 32_768
    # The response reserve IS the response limit for chat: no num_predict is set,
    # so what the model may emit is whatever the prompt did not consume.
    assert RESERVE_OUTPUT_TOKENS >= 8_192
