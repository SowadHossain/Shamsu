from __future__ import annotations

from io import StringIO

from rich.console import Console

from shamsu.cli.repl import (
    _handle_log,
    _handle_parse_prd,
    _resolve_workspace_file,
    parse_args,
    resolve_workspace,
)
from shamsu.safety.sandbox import SecurityError
from shamsu.session.manager import SessionManager


def test_parse_args_accepts_workspace():
    args = parse_args(["--workspace", "sample-project"])

    assert args.workspace == "sample-project"


def test_resolve_workspace_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    assert resolve_workspace(None) == tmp_path.resolve()


def test_resolve_workspace_uses_explicit_path(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    assert resolve_workspace(str(workspace)) == workspace.resolve()


def test_resolve_workspace_does_not_auto_redirect_to_ancestor_workspace(tmp_path):
    (tmp_path / ".shamsu").mkdir()
    child = tmp_path / "scripts"
    child.mkdir()

    assert resolve_workspace(str(child)) == child.resolve()


def test_parse_prd_path_accepts_file_inside_workspace(tmp_path):
    prd = tmp_path / "PROJECT.md"
    prd.write_text("# Project\n\n## Entities\n- Task: title (text)\n", encoding="utf-8")

    resolved = _resolve_workspace_file("PROJECT.md", tmp_path)

    assert resolved == prd.resolve()


def test_parse_prd_path_rejects_file_outside_workspace(tmp_path):
    outside = tmp_path.parent / "OUTSIDE_PRD.md"
    outside.write_text("# Outside\n", encoding="utf-8")

    try:
        try:
            _resolve_workspace_file(str(outside), tmp_path)
        except SecurityError as exc:
            assert "outside workspace" in str(exc)
        else:  # pragma: no cover - explicit failure path
            raise AssertionError("Expected SecurityError")
    finally:
        outside.unlink(missing_ok=True)


def test_handle_parse_prd_prints_parsed_title_inside_workspace(tmp_path):
    prd = tmp_path / "PROJECT.md"
    prd.write_text("# Project\n\n## Pages\n- Dashboard: overview\n", encoding="utf-8")
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)

    _handle_parse_prd("parse-prd PROJECT.md", tmp_path, console)

    assert "Title: Project" in output.getvalue()


def test_handle_parse_prd_reports_outside_workspace(tmp_path):
    outside = tmp_path.parent / "OUTSIDE_PRD.md"
    outside.write_text("# Outside\n", encoding="utf-8")
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)

    try:
        _handle_parse_prd(f'parse-prd "{outside}"', tmp_path, console)
        assert "outside workspace" in output.getvalue()
    finally:
        outside.unlink(missing_ok=True)


def test_handle_log_tails_and_redacts_session_events(tmp_path):
    logger = SessionManager(tmp_path).create_session("test")
    logger.log("test.first", {"message": "first"}, "first")
    logger.log("test.secret", {"password": "abc123"}, 'password = "abc123"')
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    _handle_log("log tail 1", logger, console)

    rendered = output.getvalue()
    assert "Last 1 Events" in rendered
    assert "[REDACTED]" in rendered
    assert "abc123" not in rendered
    assert "first" not in rendered


def test_handle_log_reports_empty_session_events(tmp_path):
    logger = SessionManager(tmp_path).create_session("test")
    logger.events_path.unlink()
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    _handle_log("log", logger, console)

    assert "No session events yet" in output.getvalue()


# --- input latency: nothing expensive may run on a keystroke -------------------
#
# Measured before these landed, on a 2,726-path workspace: ~780 ms per keystroke
# for an @mention and ~2,130 ms for `/models use`, both on the event loop thread
# with `complete_while_typing=True`. Keystrokes queued in the OS buffer and
# arrived in a burst, which is what "the TUI lags" was.


def _write_big_workspace(root, files: int = 1500):
    for index in range(files):
        directory = root / f"pkg{index % 25}"
        directory.mkdir(exist_ok=True)
        (directory / f"module_{index}.py").write_text("x = 1\n", encoding="utf-8")
    return root


def test_mention_completion_does_not_walk_the_workspace_per_keystroke(tmp_path):
    import time

    from shamsu.tools.workspace import (
        clear_mention_index_cache,
        mention_suggestions_cached,
    )

    _write_big_workspace(tmp_path)
    clear_mention_index_cache()
    mention_suggestions_cached(tmp_path, "m")  # cold: pays for the walk once

    started = time.perf_counter()
    for fragment in ("mo", "mod", "modu", "modul", "module", "module_1"):
        mention_suggestions_cached(tmp_path, fragment)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.25, f"six keystrokes took {elapsed:.2f}s - the walk is back"


def test_mention_completion_serves_stale_rather_than_stalling(tmp_path, monkeypatch):
    """Expiry must not hand one keystroke in every TTL the whole walk."""
    import time

    from shamsu.tools import workspace as workspace_module

    _write_big_workspace(tmp_path, files=400)
    workspace_module.clear_mention_index_cache()
    workspace_module.mention_suggestions_cached(tmp_path, "m")
    monkeypatch.setattr(workspace_module, "_MENTION_INDEX_TTL_SECONDS", 0.0)

    started = time.perf_counter()
    suggestions = workspace_module.mention_suggestions_cached(tmp_path, "module_1")
    elapsed = time.perf_counter() - started

    assert suggestions, "a stale index must still answer"
    assert elapsed < 0.1, f"expiry blocked for {elapsed:.2f}s instead of refreshing behind"


def test_mention_completion_matches_the_uncached_tool(tmp_path):
    """The fast path must not quietly become a different search."""
    from shamsu.tools.workspace import (
        WorkspaceTool,
        clear_mention_index_cache,
        mention_suggestions_cached,
    )

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "asteroid.js").write_text("//\n", encoding="utf-8")
    (tmp_path / "src" / "asteroid_spawner.js").write_text("//\n", encoding="utf-8")
    (tmp_path / "notes file.md").write_text("#\n", encoding="utf-8")
    clear_mention_index_cache()

    for fragment in ("aster", "src/", "notes"):
        assert mention_suggestions_cached(tmp_path, fragment) == WorkspaceTool(
            tmp_path
        ).mention_suggestions(fragment), fragment


def test_the_agent_facing_walk_is_never_cached(tmp_path):
    """`WorkspaceTool` must see a file the agent wrote a moment ago.

    The completion cache exists precisely because it may be stale; wiring it
    into the tool the agent calls would resurrect the "provide the full path"
    dead end a to-be-created file used to hit.
    """
    from shamsu.tools.workspace import WorkspaceTool, mention_suggestions_cached

    (tmp_path / "before.py").write_text("x = 1\n", encoding="utf-8")
    mention_suggestions_cached(tmp_path, "b")  # prime the cache

    (tmp_path / "after.py").write_text("y = 2\n", encoding="utf-8")

    found = [path.name for path in WorkspaceTool(tmp_path).find_files("after")]
    assert "after.py" in found


def test_the_prompt_completer_runs_off_the_event_loop(tmp_path):
    """The load-bearing one: a sync `Completer` blocks input while it works.

    Built inside an app session with a pipe input so this actually runs on a
    machine with no console screen buffer, rather than skipping there - which
    is every CI box, and would have made this assertion decorative.
    """
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.completion import ThreadedCompleter
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from shamsu.cli.repl import _make_prompt_session

    with create_pipe_input() as pipe, create_app_session(
        input=pipe, output=DummyOutput()
    ):
        session = _make_prompt_session(tmp_path)

    assert session is not None
    assert isinstance(session.completer, ThreadedCompleter)
