r"""A server does not exit, so waiting for it to exit is not a check.

Live 2026-08-24, `F:\Work\shamsu test - 24aug\demo-3\asteroid`. Thirteen
commands ran across a two-hour, sixteen-turn session. EIGHT of them were
`npm run dev`, each burning the full 120s and returning exit 124 - sixteen
minutes of wall clock for a command that had printed

    VITE v5.4.21  ready in 421 ms
    Local:   http://localhost:3000/

inside half a second and was serving the whole time. The model tried to
background it once, `npm run dev -- --host 0.0.0.0 &`, and cmd.exe read the `&`
as a command separator and ran it in the foreground anyway.

The agent therefore never once saw the page it spent the session fixing. Every
rendering claim in sixteen turns was unverifiable in principle, and the only
command that ever produced a real signal was `npm run build`, which exits 0 on
this project because none of its bugs are build-time errors.

The same session also lost two turns to POSIX-isms handed straight to cmd.exe:

    mkdir -p src assets public   -> created a directory named `-p`
    curl -s localhost:3000 | head -50
                                 -> 'head' is not recognized as an internal or
                                    external command

`_platform_command` existed to translate exactly this and was called from
nowhere in the package, so the `python3` shim in its docstring had never run
either.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from shamsu.tools.executor import (
    CommandRunner,
    _platform_command,
    _strip_posix_background,
    looks_like_a_server,
)

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="cmd.exe translation is Windows-only"
)


def _runner(workspace: Path) -> CommandRunner:
    return CommandRunner(workspace, approval_func=lambda request: True, timeout_seconds=120)


# -- knowing a server when it sees one --------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "npm run dev",
        "npm run dev -- --host 0.0.0.0",
        "npm start",
        "yarn dev",
        "pnpm dev",
        "vite",
        "next dev",
        "flask run",
        "uvicorn app:app",
        "python -m http.server 8000",
        "manage.py runserver",
    ],
)
def test_a_command_that_never_exits_is_recognised(command: str):
    assert looks_like_a_server(command), command


@pytest.mark.parametrize(
    "command",
    ["npm install", "npm run build", "npm test", "pytest", "node index.js",
     "curl -s http://localhost:3000", "git status", "ls"],
)
def test_a_command_that_does_exit_is_left_alone(command: str):
    assert not looks_like_a_server(command), command


def test_a_trailing_ampersand_is_a_request_to_background():
    """cmd.exe reads it as a separator and runs the thing in the foreground."""
    assert _strip_posix_background("npm run dev -- --host 0.0.0.0 &") == (
        "npm run dev -- --host 0.0.0.0",
        True,
    )
    assert _strip_posix_background("a && b") == ("a && b", False)
    assert _strip_posix_background("npm test") == ("npm test", False)


# -- the shell translation --------------------------------------------------


@WINDOWS_ONLY
def test_mkdir_dash_p_stops_creating_a_directory_called_dash_p():
    assert _platform_command("mkdir -p src assets public") == "mkdir src assets public"


@WINDOWS_ONLY
def test_mkdir_dash_p_also_fixes_the_separators():
    """`mkdir a/b/c` fails on cmd too - it reads `/b` as a switch - so dropping
    the flag alone would trade one failure for another."""
    assert _platform_command("mkdir -p a/b/c") == r"mkdir a\b\c"


@WINDOWS_ONLY
@pytest.mark.parametrize(
    "command,expected_fragment",
    [
        ("curl -s http://x | head -50", "Select-Object -First 50"),
        ("type log.txt | tail -20", "Select-Object -Last 20"),
        ("which node", "where node"),
    ],
)
def test_the_posix_isms_that_cost_this_session_a_turn(command, expected_fragment):
    assert expected_fragment in _platform_command(command)


@WINDOWS_ONLY
def test_a_destructive_command_is_never_translated():
    """Translating `rm -rf /` to `rmdir /s /q /` stopped it matching the
    blocklist, so a command the harness refuses became one it ran. Caught by
    four existing safety tests going red - the translation now happens
    downstream of `classify_command`, and this rule is gone besides.
    """
    assert _platform_command("rm -rf /") == "rm -rf /"
    assert _platform_command("rm -rf dist") == "rm -rf dist"


@WINDOWS_ONLY
def test_a_blocked_command_stays_blocked_through_the_whole_path(tmp_path: Path):
    from shamsu.tools.executor import BLOCKED_EXIT_CODE

    code, _, _ = _runner(tmp_path).run("rm -rf /", tmp_path)

    assert code == BLOCKED_EXIT_CODE


@WINDOWS_ONLY
def test_a_command_with_nothing_to_translate_is_untouched():
    for command in ("npm install", "git status", "pytest -q"):
        assert _platform_command(command) == command


@WINDOWS_ONLY
def test_the_translation_reaches_the_command_that_actually_runs(tmp_path: Path):
    """`_platform_command` was dead code: defined, documented, never called."""
    code, _, _ = _runner(tmp_path).run("mkdir -p src assets", tmp_path)

    assert code == 0
    assert (tmp_path / "src").is_dir()
    assert (tmp_path / "assets").is_dir()
    assert not (tmp_path / "-p").exists(), "the flag was taken as a directory name"


# -- the server actually stays up -------------------------------------------


def test_a_server_returns_promptly_and_keeps_serving(tmp_path: Path):
    """The whole point: 120s and exit 124 became seconds and a live port."""
    (tmp_path / "hello.txt").write_text("it serves", encoding="utf-8")
    runner = _runner(tmp_path)

    started = time.monotonic()
    code, stdout, _ = runner.run("python -m http.server 8791", tmp_path)
    elapsed = time.monotonic() - started

    assert code == 0, "a server that is still up is the SUCCESS case"
    assert elapsed < 30, f"came back in {elapsed:.1f}s; the bug was paying 120"
    assert "still running" in stdout

    # ...and it is genuinely reachable, which is what the agent could never do.
    from urllib.request import urlopen

    with urlopen("http://localhost:8791/hello.txt", timeout=10) as response:
        assert response.read().decode() == "it serves"


def test_a_command_that_exits_on_its_own_is_reported_normally(tmp_path: Path):
    """`npm start` on a project without that script exits 1 immediately, and
    that has to read as a failure, not as a happily-running server."""
    runner = _runner(tmp_path)

    code, stdout, _stderr = runner.run(
        f'"{sys.executable}" -m http.server --bogus-flag-that-fails', tmp_path
    )

    assert code != 0
    assert "still running" not in stdout


def test_detached_processes_are_reaped(tmp_path: Path):
    from shamsu.tools import executor

    _runner(tmp_path).run("python -m http.server 8792", tmp_path)
    assert executor._DETACHED, "the process must be registered for cleanup"

    executor._reap_detached()

    assert not executor._DETACHED
