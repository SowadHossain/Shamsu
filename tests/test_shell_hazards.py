"""The two ways the shell got around every guard in this harness.

Both were found in one live run, `F:/Work/voice-demo` 2026-08-31, and they
happened in sequence - the first caused the second.

1. `curl ... | head -50` was rewritten into a `powershell -Command` stage. On
   cmd.exe a pipeline reports the LAST stage's exit code, so curl failing to
   connect (exit 7) came back as exit 0 with empty output. Five times.
2. Reading that as "the page is empty", the model ran
   `curl -s http://localhost:8000/ > index.html`. cmd truncates the target
   before the left-hand side runs. A 5KB page became 0 bytes, with no backup,
   no journal entry, and `affected_paths: []` in the audit record.

`write_file` had already refused a blind overwrite of that same file, and
`delete_file` had refused to remove it twice. Every guard held; the shell went
around all of them.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from shamsu.tools.executor import _split_output_filter, redirect_would_truncate


# -- the laundered exit code -------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe pipeline semantics")
def test_a_pipeline_really_does_launder_the_exit_code():
    """Not a belief about cmd - a measurement of it. If this ever stops being
    true the filters below can go back to being shell stages."""
    alone = subprocess.run(
        "curl -s http://localhost:59999/", shell=True, capture_output=True
    )
    piped = subprocess.run(
        'curl -s http://localhost:59999/ | powershell -NoProfile -Command '
        '"$input | Select-Object -First 5"',
        shell=True,
        capture_output=True,
    )
    assert alone.returncode != 0
    assert piped.returncode == 0, "the pipeline hides the failure"


def test_the_command_runs_alone_so_its_exit_code_is_its_own():
    stripped, _filter = _split_output_filter("curl -s http://x | head -50")
    assert stripped == "curl -s http://x"
    assert "powershell" not in stripped


@pytest.mark.parametrize(
    "command,text,expected",
    [
        ("ls | head -2", "a\nb\nc", "a\nb"),
        ("ls | head -n 2", "a\nb\nc", "a\nb"),
        ("ls | tail -1", "a\nb\nc", "c"),
        ("ls | wc -l", "a\nb\nc", "3"),
        ("ls | grep b", "abc\nxyz\nbbb", "abc\nbbb"),
        ("ls | grep -i B", "abc\nxyz", "abc"),
        ("ls | grep -v b", "abc\nxyz", "xyz"),
        ('ls | grep "two words"', "two words here\nno", "two words here"),
    ],
)
def test_each_filter_is_applied_in_python(command, text, expected):
    _stripped, filter_fn = _split_output_filter(command)
    assert filter_fn is not None
    assert filter_fn(text) == expected


def test_grep_and_wc_used_to_die_outright():
    """`'grep' is not recognized as an internal or external command` - exit 255,
    twice in the live run. They were never translated at all."""
    for command in ("curl -s http://x | grep canvas", "curl -s http://x | wc -l"):
        stripped, filter_fn = _split_output_filter(command)
        assert stripped == "curl -s http://x"
        assert filter_fn is not None


def test_a_command_with_no_filter_is_untouched():
    assert _split_output_filter("npm run build") == ("npm run build", None)


def test_a_bare_filter_is_not_peeled_into_nothing():
    command, filter_fn = _split_output_filter("| head -5")
    assert filter_fn is None and command == "| head -5"


# -- the truncating redirect -------------------------------------------------


def test_the_exact_command_that_destroyed_the_file(tmp_path):
    page = tmp_path / "index.html"
    page.write_text("<html>real content</html>", encoding="utf-8")
    doomed = redirect_would_truncate(
        f'curl -s "http://localhost:8000/" > {page}', tmp_path, tmp_path
    )
    assert doomed == str(page.resolve())


def test_a_relative_target_resolves_against_the_cwd(tmp_path):
    (tmp_path / "game.js").write_text("code", encoding="utf-8")
    assert redirect_would_truncate("echo x > game.js", tmp_path, tmp_path)


def test_appending_destroys_nothing(tmp_path):
    (tmp_path / "log.txt").write_text("kept", encoding="utf-8")
    assert redirect_would_truncate("echo x >> log.txt", tmp_path, tmp_path) == ""


def test_writing_a_new_file_is_ordinary(tmp_path):
    """Only truncation is refused. Creating output is useful and common."""
    assert redirect_would_truncate("echo x > fresh.txt", tmp_path, tmp_path) == ""


def test_a_file_outside_the_workspace_is_not_this_guard_s_business(tmp_path):
    outside = tmp_path.parent / "elsewhere.txt"
    outside.write_text("x", encoding="utf-8")
    try:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        assert redirect_would_truncate(
            f"echo x > {outside}", workspace, workspace
        ) == ""
    finally:
        outside.unlink(missing_ok=True)


def test_a_command_with_no_redirect_at_all(tmp_path):
    assert redirect_would_truncate("npm run build", tmp_path, tmp_path) == ""


def test_the_refusal_names_the_tool_to_use_instead(tmp_path, monkeypatch):
    """The rule every message in this harness follows: name the next action."""
    from shamsu.tools import executor as module

    page = tmp_path / "index.html"
    page.write_text("real", encoding="utf-8")

    runner = module.CommandRunner.__new__(module.CommandRunner)
    runner.workspace_root = tmp_path
    runner.audit_logger = type("_A", (), {"log": lambda *a, **k: None})()
    code, out, err = module.CommandRunner.run(
        runner, f"curl -s http://x > {page}", tmp_path
    )

    assert code == module.BLOCKED_EXIT_CODE
    assert out == ""
    assert "write_file" in err and "append_file" in err
    assert page.read_text(encoding="utf-8") == "real", "the file must survive"
