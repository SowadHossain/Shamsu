"""Tests for the write channel: code must never be lost to a JSON escaping slip.

The anchor is a real failure. On 2026-08-03, run_2026-08-03_00-49-07_037b asked
qwen2.5-coder:7b-instruct to create templates/my_orders.html. The model emitted a
complete, correct write_file call as text — 736 chars, both content and filepath
present, nothing truncated — but escaped the opening quote of
``href=\\"{% url ... %}">`` and not the closing one. json.loads died at char 389,
the salvage cascade returned zero calls, and the harness reported "the model
returned prose". At temperature 0.1 the retry reproduced it byte for byte, so all
three rounds failed identically and the run wrote nothing.

The payload lives in tests/fixtures/ as a RAW text file, never as a Python
literal: re-escaping the fixture is the exact bug class under test.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.llm.output import parse_model_turn

REGISTERED = {
    "read_file",
    "write_file",
    "append_file",
    "edit_file",
    "run_command",
    "ask_user",
}

FIXTURES = Path(__file__).parent / "fixtures"


def _resp(content: str) -> dict:
    return {"message": {"content": content, "tool_calls": []}}


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# The 2026-08-03 regression
# ---------------------------------------------------------------------------


def test_the_2026_08_03_unescaped_quote_payload_yields_one_write_file_call():
    raw = _fixture("qwen_unescaped_write_file_2026_08_03.txt")
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.name == "write_file"
    assert call.arguments["filepath"] == "templates/my_orders.html"


def test_the_recovered_payload_keeps_the_stray_quote_as_a_literal():
    raw = _fixture("qwen_unescaped_write_file_2026_08_03.txt")
    content = parse_model_turn(_resp(raw), REGISTERED).tool_calls[0].arguments["content"]

    # The whole point: the byte the model failed to escape must reach disk as an
    # ordinary character, because that is what the Django template needs.
    assert "href=\"{% url 'core:order_detail' order.id %}\">" in content
    assert "\\n" not in content


def test_the_recovered_payload_is_not_truncated():
    """A wrong terminator choice shortens the content; that must fail here.

    json_repair "succeeds" on this payload by cutting the string at the stray
    quote, which would write a half file with no error to report. Assert the
    first line, the middle, and the last line so a truncating regression cannot
    pass by returning a plausible prefix.
    """
    raw = _fixture("qwen_unescaped_write_file_2026_08_03.txt")
    content = parse_model_turn(_resp(raw), REGISTERED).tool_calls[0].arguments["content"]

    lines = content.splitlines()
    assert lines[0] == '{% extends "base.html" %}'
    assert "{% empty %}" in content
    assert lines[-1] == "{% endblock %}"


def test_the_repair_is_reported_with_the_real_parser_error():
    """The loop needs the true reason, not "returned prose"."""
    raw = _fixture("qwen_unescaped_write_file_2026_08_03.txt")
    turn = parse_model_turn(_resp(raw), REGISTERED)

    repaired = [f for f in turn.parse_failures if f.repaired]
    assert len(repaired) == 1
    failure = repaired[0]
    assert failure.kind == "quote_repaired"
    assert failure.tool == "write_file"
    assert failure.path == "templates/my_orders.html"
    # The verbatim json.loads message, char offset included.
    assert "Expecting" in failure.error
    assert "char 389" in failure.error


# ---------------------------------------------------------------------------
# The repair must stay narrow
# ---------------------------------------------------------------------------


def test_a_call_cut_off_mid_payload_is_reported_as_truncated_not_prose():
    """Truncation is a different diagnosis from an escaping slip.

    A truncated call never balances its braces, so _iter_json_objects skips it
    and no salvager ever sees it. Without its own detection the loop falls
    through to "the model returned prose" for output that was a correct call the
    model ran out of room to finish — and the fix for that is a bigger output
    budget, not different escaping.
    """
    raw = '{"name": "write_file", "arguments": {"content": "def main():\\n    print('
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert turn.tool_calls == []
    truncated = [f for f in turn.parse_failures if f.kind == "json_truncated"]
    assert len(truncated) == 1
    assert truncated[0].tool == "write_file"
    assert truncated[0].error


def test_the_quote_repair_never_runs_on_a_non_mutation_call():
    """read_file / run_command args always parse; a rewrite could only corrupt.

    This span is deliberately broken in the same way as the write_file payload.
    It must NOT be repaired into a call, and must not be reported as a mutation
    parse failure either.
    """
    raw = '{"name": "run_command", "arguments": {"command": "echo "hi" > a.txt"}}'
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert [f for f in turn.parse_failures if f.tool == "write_file"] == []
    assert all(call.name != "write_file" for call in turn.tool_calls)


def test_an_unregistered_mutation_tool_is_not_repaired():
    raw = _fixture("qwen_unescaped_write_file_2026_08_03.txt")
    turn = parse_model_turn(_resp(raw), {"read_file", "run_command"})

    assert turn.tool_calls == []
    assert turn.parse_failures == ()


def test_python_apostrophe_escapes_survive_alongside_a_stray_quote():
    """Both repairs must compose in one pass.

    _load_json already preserved Python's ``\\'`` escape; the quote repair runs
    after it, so a payload carrying both must come out with the apostrophe
    escape intact AND the stray quote literalised.
    """
    raw = (
        '{"name": "write_file", "arguments": {"filepath": "a.py", '
        '"content": "x = \'a\\\'b\'\\nprint("hi")\\n"}}'
    )
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert len(turn.tool_calls) == 1
    content = turn.tool_calls[0].arguments["content"]
    assert 'print("hi")' in content
    assert "\\'" in content


def test_a_prose_example_without_a_content_key_can_never_write():
    """The salvager maps any {name, arguments} shape, by long-standing design.

    So a prose example DOES become a call object. The guarantee that matters is
    narrower and is enforced one layer down: with no filepath and no content,
    write_file refuses it ("Missing filepath."). Pin that, rather than asserting
    a strictness the salvager never promised — the repair must not quietly widen
    what counts as a writable call.
    """
    raw = (
        "You could call it like this, though I have not:\n"
        '{"tool": "write_file", "params": {"note": "no content key here"}}\n'
        "Tell me which file you meant."
    )
    turn = parse_model_turn(_resp(raw), REGISTERED)

    writes = [call for call in turn.tool_calls if call.name == "write_file"]
    for call in writes:
        assert "content" not in call.arguments
        assert "filepath" not in call.arguments
