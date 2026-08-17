"""File-block layouts a small model actually emits must all reach the parser.

The 2026-08-17 live smoke run lost a complete, correct milestone because the
model put its `# write_file:` header above the fence instead of inside it. These
tests pin every layout that has been observed, and pin the prose that must NOT
be mistaken for one.
"""
from __future__ import annotations

from shamsu.llm.output import parse_model_turn
from shamsu.transcript.blocks import normalize_file_headers

WRITE_TOOLS = ("write_file", "append_file", "edit_file")


def calls(text: str):
    turn = parse_model_turn({"message": {"content": normalize_file_headers(text)}}, WRITE_TOOLS)
    return [(c.name, c.arguments.get("filepath"), c.arguments.get("content", "")) for c in turn.tool_calls]


def test_canonical_header_inside_fence_still_works():
    text = "```python\n# write_file: app.py\nX = 1\n```"
    assert calls(text) == [("write_file", "app.py", "X = 1\n")]


def test_header_above_fence_is_recovered():
    """The exact shape qwen2.5-coder produced on 2026-08-17."""
    text = "# write_file: app.py\n```python\nfrom flask import Flask\n```"
    assert calls(text) == [("write_file", "app.py", "from flask import Flask\n")]


def test_header_above_fence_without_comment_prefix():
    text = "write_file: requirements.txt\n```txt\nFlask==2.0.1\n```"
    assert calls(text) == [("write_file", "requirements.txt", "Flask==2.0.1\n")]


def test_multiple_blocks_in_one_answer():
    text = (
        "Here is milestone 1.\n\n"
        "# write_file: app.py\n```python\nA = 1\n```\n\n"
        "# write_file: requirements.txt\n```txt\nFlask\n```\n"
    )
    assert calls(text) == [
        ("write_file", "app.py", "A = 1\n"),
        ("write_file", "requirements.txt", "Flask\n"),
    ]


def test_bolded_path_title_becomes_a_write():
    text = "**templates/tasks.html**\n```html\n<h1>Tasks</h1>\n```"
    assert calls(text) == [("write_file", "templates/tasks.html", "<h1>Tasks</h1>\n")]


def test_backticked_path_title_becomes_a_write():
    text = "`src/db.js`\n```js\nexport const db = 1;\n```"
    assert calls(text) == [("write_file", "src/db.js", "export const db = 1;\n")]


def test_heading_path_title_becomes_a_write():
    text = "### config/settings.py\n```python\nDEBUG = True\n```"
    assert calls(text) == [("write_file", "config/settings.py", "DEBUG = True\n")]


def test_append_and_edit_headers_survive():
    text = "# append_file: notes.txt\n```\nmore\n```"
    assert calls(text)[0][0] == "append_file"


def test_prose_fence_without_a_path_writes_nothing():
    text = "Here is an example:\n```python\nprint('hi')\n```"
    assert calls(text) == []


def test_bare_language_fence_is_not_a_path():
    text = "```python\nprint('hi')\n```"
    assert calls(text) == []


def test_normalization_is_idempotent():
    text = "# write_file: app.py\n```python\nX = 1\n```"
    once = normalize_file_headers(text)
    assert normalize_file_headers(once) == once
    assert calls(once) == [("write_file", "app.py", "X = 1\n")]


def test_path_traversal_is_still_rejected():
    """Normalization must not smuggle anything past the parser's safety work."""
    text = "# write_file: ../../etc/passwd\n```\npwned\n```"
    assert calls(text) == []


def test_absolute_path_is_still_rejected():
    text = "# write_file: /etc/passwd\n```\npwned\n```"
    assert calls(text) == []


def test_markdown_target_still_needs_a_four_backtick_fence():
    """The truncation guard in output.py must survive normalization."""
    three = "# write_file: README.md\n```\n# Title\n```"
    assert calls(three) == [], "a 3-backtick markdown block must still be refused"
    four = "# write_file: README.md\n````\n# Title\n\n```py\nx\n```\n````"
    assert calls(four)[0][1] == "README.md"


def test_denylisted_titles_are_not_paths():
    text = "e.g.\n```python\nx = 1\n```"
    assert calls(text) == []


def test_empty_input_is_safe():
    assert normalize_file_headers("") == ""
    assert calls("") == []
