"""Tests for the model I/O boundary (shamsu/llm/output.py) and the capability
registry flags that drive it. These are the load-bearing pieces of the
reliability design: they turn messy small-model output into a normalized turn so
the loops never leak raw tool JSON / diff markers to the user."""
from __future__ import annotations

from shamsu.llm.output import ModelTurn, parse_model_turn
from shamsu.runtime.models import (
    model_is_reasoning,
    model_supports_native_tools,
)

REGISTERED = {
    "read_file",
    "write_file",
    "edit_file",
    "run_command",
    "ask_user",
    "search_index",
    "git_status",
}


def _resp(content: str = "", tool_calls=None, thinking: str = "") -> dict:
    message: dict = {"content": content, "tool_calls": tool_calls or []}
    if thinking:
        message["thinking"] = thinking
    return {"message": message}


# ---------------------------------------------------------------------------
# Native tool calls (the happy path — salvaged=False)
# ---------------------------------------------------------------------------


def test_native_tool_call_is_passed_through_not_salvaged():
    resp = _resp(
        content="",
        tool_calls=[{"id": "c1", "function": {"name": "read_file", "arguments": {"filepath": "a.py"}}}],
    )
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.salvaged is False
    assert [c.name for c in turn.tool_calls] == ["read_file"]
    assert turn.tool_calls[0].arguments == {"filepath": "a.py"}
    assert turn.tool_calls[0].id == "c1"


def test_native_string_arguments_are_coerced_to_dict():
    resp = _resp(
        tool_calls=[{"function": {"name": "run_command", "arguments": '{"command": "pytest -q"}'}}],
    )
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.tool_calls[0].arguments == {"command": "pytest -q"}


def test_native_unregistered_tool_is_not_dropped():
    # Native calls are trusted through so the loop can report an honest
    # "unknown tool" result rather than silently treating prose as the answer.
    resp = _resp(tool_calls=[{"function": {"name": "totally_made_up", "arguments": {}}}])
    turn = parse_model_turn(resp, REGISTERED)
    assert [c.name for c in turn.tool_calls] == ["totally_made_up"]


# ---------------------------------------------------------------------------
# G1: embedded-JSON salvage (the {"name":"ask_user",...} leak)
# ---------------------------------------------------------------------------


def test_salvages_ask_user_json_from_plain_text():
    resp = _resp('{"name": "ask_user", "arguments": {"question": "Which file?"}}')
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.salvaged is True
    assert turn.tool_calls[0].name == "ask_user"
    assert turn.tool_calls[0].arguments == {"question": "Which file?"}
    # The raw JSON must NOT survive into the visible answer.
    assert "ask_user" not in turn.text
    assert "{" not in turn.text


def test_salvages_json_embedded_in_prose_and_fences():
    content = (
        "Sure, let me open that file for you.\n"
        "```json\n"
        '{"action": "read_file", "parameters": {"filepath": "src/App.tsx"}}\n'
        "```\n"
    )
    turn = parse_model_turn(resp := _resp(content), REGISTERED)
    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"filepath": "src/App.tsx"}
    assert "read_file" not in turn.text
    # The now-empty json fence is cleaned away.
    assert "```" not in turn.text
    assert "let me open that file" in turn.text


def test_repairs_malformed_json_tool_call():
    # Trailing comma + single missing brace — the near-miss small models emit.
    resp = _resp('{"name": "run_command", "arguments": {"command": "ls",}}')
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.tool_calls[0].name == "run_command"
    assert turn.tool_calls[0].arguments == {"command": "ls"}


def test_unregistered_json_is_left_in_text_not_salvaged():
    resp = _resp('Here is an example config: {"name": "not_a_tool", "arguments": {"x": 1}}')
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.tool_calls == []
    assert turn.salvaged is False
    # Not our tool -> we neither execute nor strip it.
    assert "not_a_tool" in turn.text


def test_plain_json_object_without_tool_name_is_ignored():
    resp = _resp('The response body is {"status": "ok", "count": 3}.')
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.tool_calls == []
    assert "status" in turn.text


def test_bare_no_arg_tool_object_is_salvaged():
    resp = _resp('{"name": "git_status"}')
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.tool_calls[0].name == "git_status"
    assert turn.tool_calls[0].arguments == {}


# ---------------------------------------------------------------------------
# G5: SEARCH/REPLACE salvage (diff-leak path)
# ---------------------------------------------------------------------------


def test_salvages_search_replace_block_into_edit_file():
    content = (
        "I'll update the greeting.\n"
        "src/app.py\n"
        "<<<<<<< SEARCH\n"
        "print('hi')\n"
        "=======\n"
        "print('hello')\n"
        ">>>>>>> REPLACE\n"
    )
    turn = parse_model_turn(_resp(content), REGISTERED)
    assert turn.salvaged is True
    call = turn.tool_calls[0]
    assert call.name == "edit_file"
    assert call.arguments["filepath"] == "src/app.py"
    assert call.arguments["old_string"] == "print('hi')"
    assert call.arguments["new_string"] == "print('hello')"
    assert "<<<<<<<" not in turn.text
    assert ">>>>>>>" not in turn.text


def test_search_replace_without_path_is_not_salvaged():
    content = "<<<<<<< SEARCH\nfoo\n=======\nbar\n>>>>>>> REPLACE\n"
    turn = parse_model_turn(_resp(content), REGISTERED)
    # No preceding path to anchor the edit -> we don't guess a target.
    assert turn.tool_calls == []


# ---------------------------------------------------------------------------
# XML-ish tool call
# ---------------------------------------------------------------------------


def test_salvages_xml_tool_call_wrapper():
    content = 'Working on it. <tool_call>{"name": "read_file", "arguments": {"filepath": "x.py"}}</tool_call>'
    turn = parse_model_turn(_resp(content), REGISTERED)
    assert turn.tool_calls[0].name == "read_file"
    assert "<tool_call>" not in turn.text
    assert "read_file" not in turn.text


# ---------------------------------------------------------------------------
# G8: thinking separation
# ---------------------------------------------------------------------------


def test_inline_think_tags_are_split_out_of_answer():
    resp = _resp("<think>The user wants X, so I should Y.</think>Here is the answer.")
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.text == "Here is the answer."
    assert "The user wants X" in turn.thinking
    assert "<think>" not in turn.text


def test_dangling_unclosed_think_is_stripped():
    resp = _resp("Answer first.\n<think>still reasoning with no close tag")
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.text == "Answer first."
    assert "still reasoning" in turn.thinking


def test_native_thinking_field_is_captured():
    resp = _resp(content="Done.", thinking="chain of thought here")
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.text == "Done."
    assert turn.thinking == "chain of thought here"


# ---------------------------------------------------------------------------
# Misc / safety
# ---------------------------------------------------------------------------


def test_plain_prose_is_unchanged():
    resp = _resp("I inspected the candidates and reported back.")
    turn = parse_model_turn(resp, REGISTERED)
    assert turn.text == "I inspected the candidates and reported back."
    assert turn.tool_calls == []


def test_empty_response_yields_empty_turn():
    turn = parse_model_turn(_resp(""), REGISTERED)
    assert turn == ModelTurn(text="", thinking="", tool_calls=[], salvaged=False)


def test_allow_salvage_false_disables_content_parsing():
    resp = _resp('{"name": "ask_user", "arguments": {"question": "?"}}')
    turn = parse_model_turn(resp, REGISTERED, allow_salvage=False)
    assert turn.tool_calls == []


def test_object_response_shape_is_supported():
    class _Msg:
        content = '{"name": "read_file", "arguments": {"filepath": "a"}}'
        tool_calls: list = []
        thinking = ""

    class _Resp:
        message = _Msg()

    turn = parse_model_turn(_Resp(), REGISTERED)
    assert turn.tool_calls[0].name == "read_file"


# ---------------------------------------------------------------------------
# Capability registry
# ---------------------------------------------------------------------------


def test_capability_flags_for_known_models():
    assert model_supports_native_tools("qwen2.5-coder:7b-instruct") is True
    assert model_supports_native_tools("deepseek-r1:7b") is False
    assert model_is_reasoning("deepseek-r1:7b") is True
    assert model_is_reasoning("qwen2.5-coder:7b-instruct") is False


def test_unknown_model_defaults_to_tool_capable_non_reasoning():
    assert model_supports_native_tools("some-custom-model:latest") is True
    assert model_is_reasoning("some-custom-model:latest") is False
