from __future__ import annotations

import sys
from pathlib import Path

from shamsu.tools.agent_tools import AgentToolRegistry

PASS_CMD = f'"{sys.executable}" -c "print(1)"'
FAIL_CMD = f'"{sys.executable}" -c "import sys; sys.exit(1)"'


def _registry(tmp_path: Path) -> AgentToolRegistry:
    return AgentToolRegistry(tmp_path, approval_func=lambda _request: True)


def test_write_file_creates_and_reads_back(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.write_file("app.py", "print('hi')\n")

    assert result.ok is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "print('hi')\n"


def test_write_file_rejects_paths_outside_workspace(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.write_file("../escape.py", "hacked\n")

    assert result.ok is False
    assert not (tmp_path.parent / "escape.py").exists()


def test_write_file_rejects_empty_non_package_source(tmp_path: Path):
    target = tmp_path / "views.py"
    target.write_text("def index():\n    return 1\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.write_file("views.py", "", overwrite=True)

    assert result.ok is False
    assert result.data["content_missing"] is True
    assert target.read_text(encoding="utf-8") == "def index():\n    return 1\n"


def test_write_file_allows_empty_package_marker(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.write_file("core/__init__.py", "")

    assert result.ok is True
    assert (tmp_path / "core/__init__.py").read_text(encoding="utf-8") == ""


def test_allowed_tools_filter_schemas_and_execution(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_allowed_tools({"read_file", "write_file"})

    names = {schema["function"]["name"] for schema in registry.tool_schemas()}

    assert names == {"read_file", "write_file"}
    blocked = registry.execute("ask_user", {"question": "Which file?"})
    assert blocked.ok is False
    assert "not allowed" in blocked.message


def test_short_write_path_resolves_to_sole_scoped_file(tmp_path: Path):
    target = tmp_path / "demo/backend/core/models.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry.set_allowed_write_paths(("demo/backend/core/models.py",))

    result = registry.append_file("backend/core/models.py", "VALUE = 2\n")

    assert result.ok is True
    assert result.data["resolved_filepath"] == "demo/backend/core/models.py"


def test_cwdless_django_command_resolves_unique_scoped_manage_directory(tmp_path: Path):
    manage = tmp_path / "demo/backend/manage.py"
    manage.parent.mkdir(parents=True)
    manage.write_text("print('manage')\n", encoding="utf-8")
    registry = _registry(tmp_path)
    registry.set_allowed_read_paths(("demo",))

    assert registry._scoped_command_cwd("python manage.py check", ".") == "demo/backend"


def test_list_files_reports_not_a_directory(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.list_files("app.py")

    assert result.ok is False


def test_run_command_omits_diagnostics_on_success(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.run_command(PASS_CMD)

    assert result.ok is True
    assert result.data["exit_code"] == 0
    assert "diagnostics" not in result.data


def test_run_command_reports_generated_workspace_files(tmp_path: Path):
    registry = _registry(tmp_path)
    command = (
        f'"{sys.executable}" -c "from pathlib import Path; '
        "Path('generated.py').write_text('VALUE = 1\\n')\""
    )

    result = registry.run_command(command)

    assert result.ok is True
    assert result.data["touched_files"] == ["generated.py"]
    assert result.data["deleted_files"] == []


def test_run_command_surfaces_diagnostics_on_failure(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.run_command(FAIL_CMD)

    assert result.ok is False
    assert result.data["exit_code"] == 1
    assert "diagnostics" in result.data
    assert isinstance(result.data["diagnostics"], str)
    assert result.data["diagnostics"]
    assert FAIL_CMD.strip('"') in result.data["diagnostics"] or "exit 1" in result.data["diagnostics"]


def test_blocked_command_is_policy_outcome_and_clears_prior_error_packet(tmp_path: Path):
    registry = _registry(tmp_path)
    assert registry.run_command(FAIL_CMD).data["actionable"] is True

    blocked = registry.run_command("rm -rf /")

    assert blocked.ok is False
    assert blocked.data["outcome_classification"] == "policy_decision"
    assert blocked.data["actionable"] is False
    assert registry.command_runner.last_error_packet is None


def test_run_command_missing_command_is_rejected(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.run_command("   ")

    assert result.ok is False
    assert result.data == {}


def test_read_only_run_command_blocks_shell_redirection(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_read_only(True)

    result = registry.run_command('python calc.py 2>&1 > output.txt')

    assert result.ok is False
    assert result.data["read_only"] is True
    assert not (tmp_path / "output.txt").exists()


def test_read_only_run_command_allows_nonwriting_execution(tmp_path: Path):
    registry = _registry(tmp_path)
    registry.set_read_only(True)

    result = registry.run_command(PASS_CMD)

    assert result.ok is True


def test_find_file_returns_matching_candidates(tmp_path: Path):
    (tmp_path / "client" / "src").mkdir(parents=True)
    (tmp_path / "admin" / "src").mkdir(parents=True)
    (tmp_path / "client" / "src" / "App.tsx").write_text("export default 1\n", encoding="utf-8")
    (tmp_path / "admin" / "src" / "App.tsx").write_text("export default 2\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.find_file("App.tsx")

    assert result.ok is True
    assert set(result.data["candidates"]) == {"client/src/App.tsx", "admin/src/App.tsx"}


def test_grep_files_finds_content_locations(tmp_path: Path):
    (tmp_path / "a.py").write_text("x = 1\nNEEDLE here\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("nothing\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.grep_files("NEEDLE")

    assert result.ok is True
    matches = result.data["matches"]
    assert len(matches) == 1
    assert matches[0]["file"] == "a.py"
    assert matches[0]["line"] == 2


def test_grep_files_ignores_vcs_and_vendor_dirs(tmp_path: Path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("NEEDLE\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("NEEDLE\n", encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.grep_files("NEEDLE")

    assert result.ok is True
    assert result.data["matches"] == []


def test_ask_user_returns_structured_pending_question(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.ask_user(
        "Which file should I read?",
        [{"label": "client/src/App.tsx", "description": "frontend"}, {"label": "admin/src/App.tsx"}],
        allow_free_text=True,
    )

    assert result.ok is True
    assert result.data["ask_user"] is True
    pending = result.data["pending_question"]
    assert pending["question"] == "Which file should I read?"
    assert [option["label"] for option in pending["options"]] == [
        "client/src/App.tsx",
        "admin/src/App.tsx",
    ]
    assert pending["awaiting"] == "user_input"


def test_ask_user_rejects_empty_question(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.ask_user("   ")

    assert result.ok is False


def test_execute_routes_new_tools(tmp_path: Path):
    (tmp_path / "app.py").write_text("value = 42\n", encoding="utf-8")
    registry = _registry(tmp_path)

    assert registry.execute("find_file", {"query": "app.py"}).ok is True
    assert registry.execute("grep_files", {"query": "value"}).ok is True
    assert registry.execute(
        "append_file",
        {"filepath": "app.py", "content": "extra = 7\n"},
    ).ok is True
    assert registry.execute("ask_user", {"question": "Which one?"}).data["ask_user"] is True


def test_tool_schemas_expose_clarification_and_discovery_tools(tmp_path: Path):
    registry = _registry(tmp_path)

    names = {schema["function"]["name"] for schema in registry.tool_schemas()}

    assert {"find_file", "grep_files", "append_file", "ask_user"}.issubset(names)


# --- a literal backslash-n in old_string (C3) ---------------------------
#
# RC4. Live 2026-08-19: 24 patch attempts, 0 successes, then 29 more in the
# next session with one payload sent nine times. The model emits `\n` as two
# characters where a newline belongs, mixed with real newlines in the same
# string. That text is in no file, so it can never match, and the harness's
# error was accurate and useless because it compared the mangled string as
# given.

BACKSLASH = chr(92)


def _mangled(text: str) -> str:
    """A model's edit block with its newlines emitted as two characters."""
    return text.replace(chr(10), BACKSLASH + "n")


def test_a_patch_whose_newlines_arrived_as_two_characters_still_applies(tmp_path: Path):
    """The exact payload shape from the log."""
    registry = _registry(tmp_path)
    (tmp_path / "main.js").write_text(
        "// Start the application when DOM is ready\n"
        "document.addEventListener('DOMContentLoaded', init);\n",
        encoding="utf-8",
    )

    result = registry.edit_file(
        "main.js",
        _mangled("// Start the application when DOM is ready\ndocument.addEventListener('DOMContentLoaded', init);"),
        "// fixed",
    )

    assert result.ok is True
    assert (tmp_path / "main.js").read_text(encoding="utf-8") == "// fixed\n"


def test_the_replacement_is_unescaped_too_or_the_backslash_lands_in_the_file(tmp_path: Path):
    """Decoding only old_string would trade one corruption for another."""
    registry = _registry(tmp_path)
    (tmp_path / "a.js").write_text("one\ntwo\n", encoding="utf-8")

    result = registry.edit_file("a.js", _mangled("one\ntwo"), _mangled("uno\ndos"))

    assert result.ok is True
    assert (tmp_path / "a.js").read_text(encoding="utf-8") == "uno\ndos\n"


def test_the_salvage_is_reported_so_the_model_can_stop_doing_it(tmp_path: Path):
    """A salvage the model never hears about is one it makes every turn."""
    registry = _registry(tmp_path)
    (tmp_path / "a.js").write_text("one\ntwo\n", encoding="utf-8")

    result = registry.edit_file("a.js", _mangled("one\ntwo"), "1")

    assert "literal backslash-n" in result.message
    assert result.data["unescaped_literal_newlines"] is True


def test_a_real_backslash_n_in_source_is_left_alone(tmp_path: Path):
    """`"\n"` in JavaScript is content, not a mangled newline. Decoding it
    would corrupt the very edit being made."""
    registry = _registry(tmp_path)
    body = 'const nl = "' + BACKSLASH + BACKSLASH + 'n";' + chr(10)
    (tmp_path / "a.js").write_text(body, encoding="utf-8")

    result = registry.edit_file("a.js", 'const nl = "' + BACKSLASH + BACKSLASH + 'n";', "const nl = NEWLINE;")

    assert result.ok is True
    assert (tmp_path / "a.js").read_text(encoding="utf-8") == "const nl = NEWLINE;\n"


def test_regex_escapes_are_not_decoded(tmp_path: Path):
    r"""Only n, r and t. `\d` and `\s` are ordinary content."""
    from shamsu.tools.agent_tools import _decode_literal_escapes

    pattern = BACKSLASH + "d+" + BACKSLASH + "s*"

    assert _decode_literal_escapes(pattern) == pattern


def test_an_ordinary_patch_is_untouched_by_the_salvage(tmp_path: Path):
    """The decode is only ever tried after an exact match has already missed."""
    registry = _registry(tmp_path)
    (tmp_path / "a.js").write_text("one\ntwo\n", encoding="utf-8")

    result = registry.edit_file("a.js", "one\ntwo", "uno")

    assert result.ok is True
    assert "literal backslash-n" not in result.message
    assert result.data["unescaped_literal_newlines"] is False


def test_a_mangled_patch_that_still_does_not_match_names_the_format_mistake(tmp_path: Path):
    """The model cannot see that its own newlines went out as two characters,
    and it repeated the same payload nine times without ever being told."""
    registry = _registry(tmp_path)
    (tmp_path / "a.js").write_text("one\ntwo\n", encoding="utf-8")

    result = registry.edit_file("a.js", _mangled("nine\nten"), "x")

    assert result.ok is False
    assert "literal backslash-n" in result.message
    assert "The file was NOT changed" in result.message


# --- a failed patch returns the file's own text (C8) --------------------
#
# RC8. The same sentence came back 29 times unchanged: "Nearest similar line is
# line 424: ...". Accurate, and useless on repeat - it never widened and never
# offered the actual text, so the model had nothing new to reason from and
# computed the same call again from memory, which is where the error was.


def _numbered_file(tmp_path: Path) -> AgentToolRegistry:
    lines = [f"line {i} of main.js" for i in range(1, 40)]
    lines[23] = "// Start the application when DOM is ready"
    (tmp_path / "main.js").write_text(chr(10).join(lines) + chr(10), encoding="utf-8")
    return _registry(tmp_path)


def test_a_failed_patch_returns_the_real_lines_around_the_nearest_hit(tmp_path: Path):
    registry = _numbered_file(tmp_path)

    result = registry.edit_file(
        "main.js", "// Start the application when the DOM is ready", "// fixed"
    )

    assert result.ok is False
    assert "The file was NOT changed" in result.message
    assert "24 | // Start the application when DOM is ready" in result.message
    assert "21 | line 21 of main.js" in result.message, "context before the anchor"
    assert "27 | line 27 of main.js" in result.message, "context after the anchor"


def test_the_failure_tells_it_to_copy_rather_than_recall(tmp_path: Path):
    registry = _numbered_file(tmp_path)

    result = registry.edit_file("main.js", "// Start the application when the DOM is ready", "x")

    assert "character for character" in result.message
    assert "from memory" in result.message


def test_the_window_is_bounded(tmp_path: Path):
    """A recovery hint must not become the tool result."""
    from shamsu.tools.agent_tools import EDIT_HINT_CONTEXT_LINES

    registry = _numbered_file(tmp_path)

    result = registry.edit_file("main.js", "// Start the application when the DOM is ready", "x")

    numbered = [ln for ln in result.message.splitlines() if " | " in ln]
    assert len(numbered) <= EDIT_HINT_CONTEXT_LINES * 2 + 1


def test_an_anchor_at_the_top_of_the_file_does_not_run_off_the_start(tmp_path: Path):
    (tmp_path / "a.js").write_text("const a = 1;" + chr(10) + "const b = 2;" + chr(10), encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.edit_file("a.js", "const a = 11;", "x")

    assert result.ok is False
    assert "1 | const a = 1;" in result.message


def test_nothing_resembling_the_text_says_so_plainly(tmp_path: Path):
    """No anchor means no window, and a hint that pretends otherwise is worse
    than none."""
    (tmp_path / "a.js").write_text("const a = 1;" + chr(10), encoding="utf-8")
    registry = _registry(tmp_path)

    result = registry.edit_file("a.js", "zzzzzzzz qqqqqqqq wwwwwwww", "x")

    assert result.ok is False
    assert "Nothing in the file resembles" in result.message
    assert "read_file" in result.message


# --- a write that changes nothing is not a success (N1) -----------------
#
# Live 2026-08-19, qwen2.5:3b-instruct. Five write_file calls, all byte-identical
# to what was already on disk, every one reported "Overwrote js/main.js
# (+0 -0 lines, 30 total)" with ok=true. The model read that as "my fix landed",
# was told by the verifier the file was still broken, concluded something ELSE
# was wrong, and sent the same bytes again.


def test_writing_identical_content_is_refused(tmp_path: Path):
    registry = _registry(tmp_path)
    body = "function f() {" + chr(10)
    (tmp_path / "main.js").write_text(body, encoding="utf-8")

    result = registry.write_file("main.js", body, overwrite=True)

    assert result.ok is False
    assert "identical" in result.message
    assert result.data["identical"] is True


def test_the_refusal_says_the_fix_is_not_the_one_that_was_sent(tmp_path: Path):
    """The model needs to know its idea was wrong, not its typing."""
    registry = _registry(tmp_path)
    (tmp_path / "a.js").write_text("x" + chr(10), encoding="utf-8")

    result = registry.write_file("a.js", "x" + chr(10), overwrite=True)

    assert "read it again" in result.message


def test_a_no_op_write_leaves_the_file_and_writes_no_transaction(tmp_path: Path):
    """A journal full of no-ops is a journal nobody can read."""
    registry = _registry(tmp_path)
    body = "const a = 1;" + chr(10)
    (tmp_path / "a.js").write_text(body, encoding="utf-8")
    before = sorted(p.name for p in (tmp_path / ".shamsu" / "mutations").glob("*")) if (
        tmp_path / ".shamsu" / "mutations"
    ).exists() else []

    registry.write_file("a.js", body, overwrite=True)

    after = sorted(p.name for p in (tmp_path / ".shamsu" / "mutations").glob("*")) if (
        tmp_path / ".shamsu" / "mutations"
    ).exists() else []
    assert (tmp_path / "a.js").read_text(encoding="utf-8") == body
    assert before == after


def test_a_real_change_still_writes(tmp_path: Path):
    """The guard must not touch the ordinary path."""
    registry = _registry(tmp_path)
    (tmp_path / "a.js").write_text("const a = 1;" + chr(10), encoding="utf-8")

    result = registry.write_file("a.js", "const a = 2;" + chr(10), overwrite=True)

    assert result.ok is True
    assert (tmp_path / "a.js").read_text(encoding="utf-8") == "const a = 2;" + chr(10)


def test_creating_a_new_file_is_never_a_no_op(tmp_path: Path):
    registry = _registry(tmp_path)

    result = registry.write_file("new.js", "const a = 1;" + chr(10))

    assert result.ok is True
    assert (tmp_path / "new.js").exists()
