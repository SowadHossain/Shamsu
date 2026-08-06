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


def test_existing_filename_wins_over_greedy_fix_phrase(tmp_path: Path):
    (tmp_path / "qa_probe.py").write_text("value = 1\n", encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "Fix the bug in qa_probe.py: the value must be 2.",
        "```python\nvalue = 2\n```",
    )

    assert result.tool_result is not None and result.tool_result.ok
    assert (tmp_path / "qa_probe.py").read_text(encoding="utf-8") == "value = 2\n"
    assert not (tmp_path / "bug in qa_probe.py").exists()


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


def test_comment_only_file_fences_do_not_create_empty_implementations(tmp_path: Path):
    result = _fallback(tmp_path).maybe_write(
        "create the project files",
        "```python\n# backend/manage.py\n```\n```python\n# backend/core/models.py\n```",
    )

    assert result.handled is False
    assert not (tmp_path / "backend" / "manage.py").exists()
    assert not (tmp_path / "backend" / "core" / "models.py").exists()


def test_scoped_single_target_recovers_filename_less_source_fence(tmp_path: Path):
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    tools.set_allowed_write_paths(("demo/backend/manage.py",))
    fallback = MarkdownWriteFallback(tools)

    result = fallback.maybe_write(
        "Implement the milestone using examples.py and settings.py.",
        """```python
from django.core.management import execute_from_command_line
execute_from_command_line([])
```""",
    )

    assert result.handled is True
    assert result.tool_result is not None and result.tool_result.ok is True
    assert (tmp_path / "demo/backend/manage.py").is_file()


def test_scoped_target_outranks_other_existing_files_named_in_prompt(tmp_path: Path):
    source = tmp_path / "demo/models.py"
    target = tmp_path / "demo/test_models.py"
    source.parent.mkdir(parents=True)
    source.write_text("class User:\n    pass\n", encoding="utf-8")
    target.write_text("OLD = True\n", encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    tools.set_allowed_write_paths(("demo/test_models.py",))

    result = MarkdownWriteFallback(tools).maybe_write(
        "Read demo/models.py and update only demo/test_models.py.",
        "```python\nfrom demo.models import User\n\ndef test_user():\n    assert User is not None\n```",
    )

    assert result.handled is True
    assert result.tool_result is not None and result.tool_result.ok is True
    assert source.read_text(encoding="utf-8") == "class User:\n    pass\n"
    assert "test_user" in target.read_text(encoding="utf-8")


def test_scoped_empty_init_fence_creates_package_marker(tmp_path: Path):
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    tools.set_allowed_write_paths(("demo/backend/core/migrations/__init__.py",))
    fallback = MarkdownWriteFallback(tools)

    result = fallback.maybe_write(
        "Implement the required package marker.",
        """```python
# backend/core/migrations/__init__.py
```""",
    )

    assert result.handled is True
    assert result.tool_result is not None and result.tool_result.ok is True
    assert (tmp_path / "demo/backend/core/migrations/__init__.py").read_text() == ""


def test_fallback_respects_focused_repair_tool_allowlist(tmp_path: Path):
    target = tmp_path / "demo/settings.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    tools.set_allowed_write_paths(("demo/settings.py",))
    tools.set_allowed_tools(("read_file", "edit_file", "append_file"))

    result = MarkdownWriteFallback(tools).maybe_write(
        "Repair demo/settings.py without rewriting it.",
        "```python\nVALUE = 2\n```",
    )

    assert result.handled is True
    assert "edit_file or append_file" in result.summary
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_append_only_fallback_requires_append_to_scoped_target(tmp_path: Path):
    target = tmp_path / "demo/models.py"
    target.parent.mkdir(parents=True)
    target.write_text("class Existing:\n    pass\n", encoding="utf-8")
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    tools.set_allowed_write_paths(("demo/models.py",))
    tools.set_allowed_tools(("read_file", "append_file"))

    result = MarkdownWriteFallback(tools).maybe_write(
        "Append the missing model declarations.",
        "```python\nclass Submission:\n    pass\n```",
    )

    assert result.handled is True
    assert "must call append_file" in result.summary
    assert "demo/models.py" in result.summary
    assert target.read_text(encoding="utf-8") == "class Existing:\n    pass\n"


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


def test_verification_only_fences_are_not_reported_as_ambiguous(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 2\n", encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "update app.py",
        "The edit is applied. Verify it with:\n```bash\npython -m py_compile app.py\n```",
    )

    assert result.handled is False
    assert result.tool_result is None
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


def test_multi_file_fix_never_invents_a_prose_prefixed_path(tmp_path: Path):
    for path, content in (
        ("client/frontend.js", "fetch('/api/todos');\n"),
        ("server.js", "app.get('/api/tasks', handler);\n"),
        ("repository.py", "TABLE = 'missing_tasks'\n"),
        ("schema.sql", "CREATE TABLE tasks (id INTEGER);\n"),
    ):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

    result = _fallback(tmp_path).maybe_write(
        "Fix the wiring bugs across client/frontend.js, server.js, repository.py, and schema.sql.",
        "```javascript\napp.get('/api/todos', handler);\n```",
    )

    assert result.handled is False
    assert not (tmp_path / "wiring bugs across client" / "frontend.js").exists()


def test_named_multi_file_proposal_becomes_valid_workspace_writes(tmp_path: Path):
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    tools.set_allowed_write_paths(("demo",))
    fallback = MarkdownWriteFallback(tools)

    result = fallback.maybe_write(
        "Implement the authentication milestone in demo.",
        "Add the following code to `demo/core/views.py`:\n"
        "```python\nfrom django.http import JsonResponse\n\ndef login_view(request):\n"
        "    return JsonResponse({'ok': True})\n```\n"
        "Create `demo/core/urls.py`:\n"
        "```python\nfrom django.urls import path\nfrom .views import login_view\n\n"
        "urlpatterns = [path('login/', login_view)]\n```",
    )

    assert result.handled is True
    assert result.tool_results is not None
    assert all(item.ok for item in result.tool_results)
    assert (tmp_path / "demo/core/views.py").is_file()
    assert (tmp_path / "demo/core/urls.py").is_file()


def test_named_multi_file_proposal_skips_invalid_python(tmp_path: Path):
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    tools.set_allowed_write_paths(("demo",))
    fallback = MarkdownWriteFallback(tools)

    result = fallback.maybe_write(
        "Implement files in demo.",
        "Create `demo/broken.py`:\n```python\nthis is ! invalid\n```\n"
        "Create `demo/valid.py`:\n```python\nVALUE = 1\n```",
    )

    assert result.handled is True
    assert not (tmp_path / "demo/broken.py").exists()
    assert (tmp_path / "demo/valid.py").read_text(encoding="utf-8") == "VALUE = 1\n"
