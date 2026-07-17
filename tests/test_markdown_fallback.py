"""MarkdownWriteFallback: prose-with-code answers become real writes.

This had NO tests while being load-bearing for every "the model showed the fix
instead of applying it" turn. The `bugfix_syntax_error` eval flaking at 2/3
exposed three holes, each pinned below:

1. the path regex required a leading verb, so "broken.py has a syntax error -
   fix it" found no target;
2. a ```bash usage fence next to the fix tripped the multiple-blocks refusal;
3. the write used overwrite=False, which is always refused for the existing
   file a FIX necessarily targets.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.agents.markdown_fallback import MarkdownWriteFallback
from shamsu.tools.agent_tools import AgentToolRegistry

BROKEN = "def greet(name)\n    return 'hi ' + name\n"
FIXED = "def greet(name):\n    return 'hi ' + name\n"


def _fallback(workspace: Path) -> MarkdownWriteFallback:
    return MarkdownWriteFallback(AgentToolRegistry(workspace, approval_func=lambda _r: True))


def _bugfix_answer() -> str:
    return (
        "The `def` line is missing a colon. Fixing it:\n\n"
        f"```python\n{FIXED}```\n\n"
        "Run the file to verify:\n\n```bash\npython broken.py\n```\n"
    )


# --- the exact captured eval failure, end to end -------------------------------


def test_fix_answer_with_usage_fence_writes_the_named_existing_file(tmp_path: Path):
    (tmp_path / "broken.py").write_text(BROKEN, encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "broken.py has a syntax error on the def line. Fix it so the file compiles.",
        _bugfix_answer(),
    )

    assert result.handled
    assert result.tool_result is not None and result.tool_result.ok, result.summary
    assert (tmp_path / "broken.py").read_text(encoding="utf-8") == FIXED


def test_filename_before_the_verb_is_still_found(tmp_path: Path):
    """PATH_RE only sees verb-led forms; an existing file named anywhere in the
    prompt is the strongest target signal there is."""
    (tmp_path / "app.py").write_text("x=1\n", encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "app.py prints the wrong total. Correct the code.",
        "Here you go:\n```python\nx = 2\n```\n",
    )

    assert result.handled and result.tool_result is not None and result.tool_result.ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 2\n"


def test_usage_fences_do_not_count_as_content(tmp_path: Path):
    (tmp_path / "broken.py").write_text(BROKEN, encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "fix broken.py",
        f"```python\n{FIXED}```\n```bash\npython broken.py\n```\n```sh\necho done\n```",
    )

    assert result.tool_result is not None and result.tool_result.ok
    assert (tmp_path / "broken.py").read_text(encoding="utf-8") == FIXED


def test_two_real_content_blocks_still_refuse(tmp_path: Path):
    """Genuine ambiguity (two python blocks, one target) must not guess."""
    (tmp_path / "broken.py").write_text(BROKEN, encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "fix broken.py",
        "```python\nversion_a = 1\n```\n```python\nversion_b = 2\n```",
    )

    assert result.handled
    assert result.tool_result is None
    assert (tmp_path / "broken.py").read_text(encoding="utf-8") == BROKEN


def test_a_fragment_never_clobbers_a_large_file(tmp_path: Path):
    big = "\n".join(f"line_{i} = {i}" for i in range(200)) + "\n"
    (tmp_path / "big.py").write_text(big, encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "fix big.py",
        "```python\nline_5 = 50\n```",
    )

    assert result.handled
    assert "edit_file" in result.summary
    assert (tmp_path / "big.py").read_text(encoding="utf-8") == big


def test_small_files_may_be_fully_replaced(tmp_path: Path):
    (tmp_path / "tiny.py").write_text("a = 1\nb = 2\n", encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "fix tiny.py", "```python\na = 1\nb = 3\n```"
    )

    assert result.tool_result is not None and result.tool_result.ok


def test_create_verb_with_new_file_still_works(tmp_path: Path):
    result = _fallback(tmp_path).maybe_write(
        "create hello.py that prints hi",
        "```python\nprint('hi')\n```",
    )

    assert result.tool_result is not None and result.tool_result.ok
    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_no_blocks_or_no_target_is_not_handled(tmp_path: Path):
    fallback = _fallback(tmp_path)
    assert fallback.maybe_write("fix broken.py", "just prose, no code").handled is False
    assert fallback.maybe_write("what is a closure?", "```python\nx=1\n```").handled is False


def test_lying_fence_tags_cannot_write_the_run_command_into_the_file(tmp_path: Path):
    """Observed live: the fix in a bare ``` fence, `python3 broken.py` inside a
    ```python fence. Trusting the tag wrote the RUN COMMAND into broken.py and
    the loop then spun claiming success. Content wins over tags."""
    (tmp_path / "broken.py").write_text(BROKEN, encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "broken.py has a syntax error on the def line. Fix it so the file compiles.",
        f"Fixed:\n```\n{FIXED}```\nRun it:\n```python\npython3 broken.py\n```",
    )

    assert result.tool_result is not None and result.tool_result.ok, result.summary
    assert (tmp_path / "broken.py").read_text(encoding="utf-8") == FIXED


def test_a_python_target_never_receives_unparseable_content(tmp_path: Path):
    """Whatever block wins for a .py file must at least parse - a replacement
    that cannot compile is strictly worse than leaving the file alone."""
    (tmp_path / "broken.py").write_text(BROKEN, encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "fix broken.py",
        "```python\nthis is ! not python at all (\n```",
    )

    assert (tmp_path / "broken.py").read_text(encoding="utf-8") == BROKEN
    assert result.tool_result is None or not result.tool_result.ok


def test_multiline_run_commands_are_still_usage(tmp_path: Path):
    (tmp_path / "app.py").write_text("x=1\n", encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "fix app.py",
        "```python\nx = 2\n```\n```\npip install rich\npython app.py\n```",
    )

    assert result.tool_result is not None and result.tool_result.ok
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "x = 2\n"


def test_two_existing_files_mentioned_is_ambiguous(tmp_path: Path):
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "a.py and b.py disagree - make them consistent",
        "```python\nx = 1\n```",
    )

    # No unique target -> the comment-path inference path, which finds none.
    assert result.handled is False
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "a"
