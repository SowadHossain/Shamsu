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


# ---------------------------------------------------------------------------
# The raw envelope: the primary channel, where escaping never happens
# ---------------------------------------------------------------------------

FENCE = "`" * 3
FENCE4 = "`" * 4


def test_a_raw_write_envelope_writes_its_body_verbatim():
    body = '{% extends "base.html" %}\n<a href="{% url \'orders\' %}">Orders</a>'
    raw = f"Creating it now.\n\n{FENCE}html\n# write_file: templates/x.html\n{body}\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert call.name == "write_file"
    assert call.arguments["filepath"] == "templates/x.html"
    # Byte-identical to what sat between the fences, plus a trailing newline.
    assert call.arguments["content"] == body + "\n"
    # The header must not leak into the file, and nothing may be escaped.
    assert "write_file" not in call.arguments["content"]
    assert "\\\"" not in call.arguments["content"]


def test_the_envelope_is_stripped_from_the_visible_answer():
    raw = f"Done.\n\n{FENCE}python\n# write_file: a.py\nprint(1)\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert "write_file" not in turn.text
    assert "print(1)" not in turn.text
    assert turn.text.strip() == "Done."


def test_a_literal_backslash_n_in_the_body_survives_verbatim():
    """Raw bodies must never go through the escaped-layout repair."""
    body = 'const s = "a\\nb";'
    raw = f"{FENCE}js\n# write_file: src/a.js\n{body}\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert turn.tool_calls[0].arguments["content"] == body + "\n"


def test_a_four_backtick_envelope_carries_a_body_containing_fences():
    body = f"# Title\n\n{FENCE}python\nprint(1)\n{FENCE}\n\nDone."
    raw = f"{FENCE4}markdown\n# write_file: docs/guide.md\n{body}\n{FENCE4}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert len(turn.tool_calls) == 1
    content = turn.tool_calls[0].arguments["content"]
    assert content == body + "\n"
    assert content.count(FENCE) == 2


def test_a_three_backtick_envelope_for_a_markdown_target_is_refused():
    """It would close at the file's own first fence and write a truncated file."""
    raw = f"{FENCE}markdown\n# write_file: docs/guide.md\n# Title\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert turn.tool_calls == []
    kinds = [f.kind for f in turn.parse_failures]
    assert "raw_envelope_fence_collision" in kinds


def test_a_raw_append_envelope_becomes_append_file():
    raw = f"{FENCE}python\n# append_file: core/urls.py\nurlpatterns += []\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert [c.name for c in turn.tool_calls] == ["append_file"]
    assert turn.tool_calls[0].arguments["filepath"] == "core/urls.py"


def test_a_raw_edit_envelope_takes_its_path_from_the_header():
    """The header path must beat the prose-guessed one.

    A path line before the block is exactly what _path_before would latch onto,
    and it is wrong here. The envelope exists so the target is declared, never
    inferred.
    """
    raw = (
        "config/urls.py needs updating, here is the edit:\n\n"
        f"{FENCE}python\n"
        "# edit_file: core/urls.py\n"
        "<<<<<<< SEARCH\n"
        "urlpatterns = []\n"
        "=======\n"
        'urlpatterns = [path("orders/", views.my_orders)]\n'
        ">>>>>>> REPLACE\n"
        f"{FENCE}\n"
    )
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert [c.name for c in turn.tool_calls] == ["edit_file"]
    args = turn.tool_calls[0].arguments
    assert args["filepath"] == "core/urls.py"
    assert args["old_string"] == "urlpatterns = []"
    assert 'path("orders/", views.my_orders)' in args["new_string"]


def test_an_edit_envelope_without_search_replace_is_refused():
    """Never fall back to writing a fragment as the whole file."""
    raw = f"{FENCE}python\n# edit_file: core/urls.py\nurlpatterns = []\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert turn.tool_calls == []
    kinds = [f.kind for f in turn.parse_failures]
    assert "edit_envelope_without_search_replace" in kinds


def test_an_implausible_header_path_is_not_executed():
    """A YAML body containing `write_file: true` must not become a write."""
    raw = f"{FENCE}yaml\n# write_file: true\nsome: value\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert turn.tool_calls == []


def test_an_absolute_or_traversing_header_path_is_refused():
    for bad in ("/etc/passwd", "../../secrets.env"):
        raw = f"{FENCE}text\n# write_file: {bad}\nx\n{FENCE}\n"
        turn = parse_model_turn(_resp(raw), REGISTERED)
        assert turn.tool_calls == [], bad
        assert "raw_envelope_bad_path" in [f.kind for f in turn.parse_failures], bad


def test_an_empty_envelope_body_is_refused():
    raw = f"{FENCE}python\n# write_file: a.py\n\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert turn.tool_calls == []
    assert "raw_envelope_empty_body" in [f.kind for f in turn.parse_failures]


def test_a_raw_envelope_inside_a_think_block_is_not_executed():
    """Reasoning is not a decision. Writing from a think trace is worse than
    losing the call: the model is still weighing options in there."""
    raw = (
        "<think>\n"
        f"Maybe I should do:\n{FENCE}python\n# write_file: a.py\nprint('draft')\n{FENCE}\n"
        "</think>\n"
        "Let me check the file first."
    )
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert turn.tool_calls == []


def test_a_real_json_file_body_stays_raw():
    """package.json has a `name` field; that must not read as a tool envelope."""
    body = '{\n  "name": "my-app",\n  "version": "1.0.0"\n}'
    raw = f"{FENCE}json\n# write_file: package.json\n{body}\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].arguments["content"] == body + "\n"


def test_a_leaked_json_envelope_inside_a_raw_body_is_unwrapped():
    body = '{"name": "write_file", "arguments": {"content": "print(1)\\n"}}'
    raw = f"{FENCE}\n# write_file: src/a.py\n{body}\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert len(turn.tool_calls) == 1
    args = turn.tool_calls[0].arguments
    # Header path survives, which _unwrap_serialized_tool_call downstream cannot do.
    assert args["filepath"] == "src/a.py"
    assert args["content"] == "print(1)\n"


def test_the_commented_json_fence_dialect_still_works():
    """Tier-2 guard: the older `# write_file` + JSON body form has no `:`, so the
    raw envelope must not intercept it."""
    raw = f'{FENCE}\n# write_file\n{{"filepath": "a.py", "content": "print(1)\\n"}}\n{FENCE}\n'
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].arguments["filepath"] == "a.py"


def test_the_envelope_is_inert_where_write_file_is_not_registered():
    """tool_calling_loop shares this parser with a narrow action-tool registry.

    A loop that cannot execute write_file must not have the envelope silently
    manufacture one for it.
    """
    raw = f"{FENCE}python\n# write_file: a.py\nprint(1)\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), {"run_command", "git_status"})

    assert turn.tool_calls == []


def test_an_ordinary_source_comment_fence_is_never_executed():
    raw = f"{FENCE}python\n# models.py\nclass User: pass\n{FENCE}\n"
    turn = parse_model_turn(_resp(raw), REGISTERED)

    assert turn.tool_calls == []


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
