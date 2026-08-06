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


def test_truncated_code_fence_does_not_swallow_a_later_tool_call():
    """The 2026-08-01 live blocker, pinned. A 7B repair reply held a cut-off
    Python fence whose `{` never closed, followed by a valid run_command call.
    The single-pass brace scan gave up at the unbalanced brace and lost the
    call, so the loop saw NO tool calls, nagged, and the milestone failed with
    the correct fix sitting unused in the reply."""
    content = (
        "The error is due to the missing `BASE_DIR` variable in `settings.py`.\n\n"
        "```python\n"
        "import os\n"
        "BASE_DIR = os.path.dirname(os.path.abspath(__file__))\n"
        "\n"
        "TEMPLATES = [\n"
        "    {\n"
        "        'BACKEND': 'django.template.backends.django.DjangoTemplates',\n"
        "        'DIRS': [],\n"
        "        'APP_DIRS': True,\n"
        "```\n\n"
        "Now, let's run the verifier again.\n\n"
        "```json\n"
        '{"name": "run_command", "arguments": {"command": "python manage.py check"}}\n'
        "```\n"
    )

    turn = parse_model_turn(_resp(content), REGISTERED)

    assert [c.name for c in turn.tool_calls] == ["run_command"]
    assert turn.tool_calls[0].arguments == {"command": "python manage.py check"}


def test_unbalanced_brace_recovery_does_not_invent_calls_from_code():
    """Recovery must not turn ordinary truncated code into a tool call."""
    content = (
        "Here is the config so far:\n\n"
        "```python\n"
        "SETTINGS = {\n"
        "    'name': 'demo',\n"
        "    'nested': {'a': 1},\n"
        "```\n"
    )

    turn = parse_model_turn(_resp(content), REGISTERED)

    assert turn.tool_calls == []


def test_salvages_json_embedded_in_prose_and_fences():
    content = (
        "Sure, let me open that file for you.\n"
        "```json\n"
        '{"action": "read_file", "parameters": {"filepath": "src/App.tsx"}}\n'
        "```\n"
    )
    turn = parse_model_turn(_resp(content), REGISTERED)
    assert turn.tool_calls[0].name == "read_file"
    assert turn.tool_calls[0].arguments == {"filepath": "src/App.tsx"}
    assert "read_file" not in turn.text
    # The now-empty json fence is cleaned away.
    assert "```" not in turn.text
    assert "let me open that file" in turn.text


def test_salvages_small_model_commented_tool_fences():
    content = """Create the model.\n\n```python
# write_file
{"filepath":"app/models.py","content":"VALUE = 1\\n"}
```\n\nThen verify.\n\n```python
# run_command
python -m py_compile app/models.py
```"""

    turn = parse_model_turn(_resp(content), REGISTERED)

    assert [call.name for call in turn.tool_calls] == ["write_file", "run_command"]
    assert turn.tool_calls[0].arguments == {
        "filepath": "app/models.py",
        "content": "VALUE = 1\n",
    }
    assert turn.tool_calls[1].arguments == {
        "command": "python -m py_compile app/models.py"
    }
    assert "# write_file" not in turn.text
    assert "# run_command" not in turn.text


def test_repairs_double_escaped_multiline_write_content():
    content = r'''{"name":"write_file","arguments":{"filepath":"app.py","content":"import os\\n\\nprint('ok')\\n"}}'''

    turn = parse_model_turn(_resp(content), REGISTERED)

    assert turn.tool_calls[0].arguments["content"] == "import os\n\nprint('ok')\n"


def test_double_escaped_layout_preserves_escapes_inside_source_strings():
    response = {
        "message": {
            "content": "",
            "tool_calls": [
                {
                    "id": "write",
                    "function": {
                        "name": "write_file",
                        "arguments": {
                            "filepath": "app.py",
                            "content": r'import os\nprint("\\n")\n',
                        },
                    },
                }
            ],
        }
    }

    turn = parse_model_turn(response, REGISTERED)

    assert turn.tool_calls[0].arguments["content"] == 'import os\nprint("\\n")\n'


def test_does_not_execute_ordinary_commented_source_fence():
    content = """```python
# models.py
VALUE = 1
```"""

    turn = parse_model_turn(_resp(content), REGISTERED)

    assert turn.tool_calls == []
    assert "VALUE = 1" in turn.text


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


# ---------------------------------------------------------------------------
# Echoed <tool_response> wrappers: qwen-family models are trained on these
# tags and fabricate them as answers ('<tool_response>{"ok": true, ...}' -
# observed live, presented as if a tool had run). An echoed result must never
# stand as a real answer.
# ---------------------------------------------------------------------------


def test_echoed_tool_response_is_stripped_from_the_answer():
    turn = parse_model_turn(
        {
            "message": {
                "content": (
                    '<tool_response>\n{"ok": true, "message": "Overwrote old_name.py"}\n'
                    "</tool_response>\nDone - the file was renamed."
                ),
                "tool_calls": [],
            }
        }
    )
    assert "tool_response" not in turn.text
    assert '"ok": true' not in turn.text
    assert "Done - the file was renamed." in turn.text


def test_pure_echo_turn_becomes_empty_not_a_fake_success():
    """Only an echoed result, no prose: the visible text must go empty so the
    loop's empty-response correction fires instead of accepting fake success."""
    turn = parse_model_turn(
        {
            "message": {
                "content": '<tool_response>\n{"ok": true, "message": "Read file."}\n</tool_response>',
                "tool_calls": [],
            }
        }
    )
    assert turn.text == ""
    assert turn.tool_calls == []


def test_truncated_echo_without_closing_tag_is_still_stripped():
    turn = parse_model_turn(
        {
            "message": {
                "content": '<tool_response>\n{"ok": true, "message": "Overwrote old_name.py (+1 -1',
                "tool_calls": [],
            }
        }
    )
    assert turn.text == ""


def test_prose_mentioning_the_words_tool_response_is_untouched():
    turn = parse_model_turn(
        {"message": {"content": "The tool response format uses JSON.", "tool_calls": []}}
    )
    assert turn.text == "The tool response format uses JSON."


# ---------------------------------------------------------------------------
# Fence-only finals: a stray unpaired ``` left after salvage is not an answer.
# Observed live on the light tier - the whole visible turn was "```", which
# dodged the empty-response correction because it was not technically empty.
# ---------------------------------------------------------------------------


def test_a_bare_unpaired_fence_becomes_empty():
    turn = parse_model_turn({"message": {"content": "```", "tool_calls": []}})
    assert turn.text == ""


def test_fence_with_language_tag_and_blank_lines_becomes_empty():
    turn = parse_model_turn(
        {"message": {"content": "```json\n\n```\n\n```", "tool_calls": []}}
    )
    assert turn.text == ""


def test_an_answer_containing_an_unclosed_fence_keeps_its_content():
    content = "Here is the file:\n```python\nx = 1\n"
    turn = parse_model_turn({"message": {"content": content, "tool_calls": []}})
    assert "x = 1" in turn.text
    assert "Here is the file:" in turn.text


def test_salvaged_json_preserves_python_apostrophe_escapes():
    content = r'''{"name":"write_file","arguments":{"filepath":"converter.py","content":"print('Invalid mode: use \'c2f\' or \'f2c\'.')"}}'''

    turn = parse_model_turn(_resp(content), REGISTERED)

    assert turn.salvaged is True
    generated = turn.tool_calls[0].arguments["content"]
    assert "\\'c2f\\'" in generated
    compile(generated, "converter.py", "exec")
