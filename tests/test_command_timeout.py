"""The command timeout has to be a promise, not a suggestion.

`subprocess.run(..., capture_output=True, timeout=N)` cannot enforce N when the
command leaves a survivor behind. It kills the direct child - the shell - then
calls `communicate()` again to drain the pipes; a grandchild that inherited
those handles keeps them open, so `communicate()` waits for an EOF that never
arrives and `TimeoutExpired` is never raised.

Live 2026-08-18: `cd frontend && python -m http.server 8000 &` hung a turn for
28 minutes against a 120s timeout, writing no tool result at all. The desktop
sat on "Working..." and the model was evicted from VRAM while it waited.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from shamsu.tools.executor import _run_command_bounded


def _background_survivor(seconds: int, port_free_marker: str) -> str:
    """A command whose shell exits while a child lives on, holding the pipes."""
    python = sys.executable
    if sys.platform == "win32":
        return f'start /b "" "{python}" -c "import time; time.sleep({seconds})" & echo {port_free_marker}'
    return f'"{python}" -c "import time; time.sleep({seconds})" & echo {port_free_marker}'


def test_a_command_that_leaves_a_survivor_still_times_out():
    """This is the whole bug: the timeout must actually fire."""
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    started = time.perf_counter()

    with pytest.raises(subprocess.TimeoutExpired):
        _run_command_bounded(_background_survivor(120, "go"), ".", 2.0, flags)

    elapsed = time.perf_counter() - started
    # Generous, but nowhere near "forever" - the old path never returned at all.
    assert elapsed < 30, f"timeout took {elapsed:.1f}s to fire"


def test_an_ordinary_command_is_untouched():
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = _run_command_bounded("echo hello", ".", 30.0, flags)

    assert result.returncode == 0
    assert "hello" in result.stdout


def test_a_failing_command_still_reports_its_output():
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    result = _run_command_bounded(
        f'"{sys.executable}" -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"',
        ".",
        30.0,
        flags,
    )

    assert result.returncode == 3
    assert "boom" in result.stderr


def test_the_whole_tree_is_killed_not_just_the_shell():
    """A survivor left running would keep holding its port, file locks and VRAM."""
    import shamsu.tools.executor as executor

    killed: list[object] = []
    original = executor._kill_process_tree

    def spy(process):
        killed.append(process)
        return original(process)

    executor._kill_process_tree = spy
    try:
        flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        with pytest.raises(subprocess.TimeoutExpired):
            _run_command_bounded(_background_survivor(120, "go"), ".", 2.0, flags)
    finally:
        executor._kill_process_tree = original

    assert killed, "the process tree was never killed on timeout"
