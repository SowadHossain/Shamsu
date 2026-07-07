from __future__ import annotations

from pathlib import Path

from rich.console import Console

from shamsu.agents.clarification import build_pending_question
from shamsu.cli.repl import _continuation_clarification, _resolve_pending_question
from shamsu.session.manager import SessionManager


def _logger_with_pending(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Clarify")
    logger.set_pending_question(
        build_pending_question(
            "Which file should I use?",
            [{"label": "client/src/App.tsx"}, {"label": "admin/src/App.tsx"}],
            created_from_prompt="read the file src/App.tsx",
        )
    )
    return logger


def test_numbered_reply_rewrites_prompt_and_clears_pending(tmp_path: Path):
    logger = _logger_with_pending(tmp_path)
    console = Console(record=True)

    rewritten = _resolve_pending_question(
        logger.get_pending_question(), "1", tmp_path, console, logger
    )

    assert rewritten is not None
    assert "read the file src/App.tsx" in rewritten
    assert "client/src/App.tsx" in rewritten
    assert logger.get_pending_question() == {}
    assert "session.pending_question.answered" in [e["event_type"] for e in logger.tail(10)]


def test_bare_yes_with_pending_resolves_without_routing_to_qa(tmp_path: Path):
    # A single-option pending question + "yes" selects that option instead of
    # sending a bare "yes" onward as a fresh prompt.
    logger = SessionManager(tmp_path).create_session("Clarify")
    logger.set_pending_question(
        build_pending_question(
            "Use client/src/App.tsx?",
            [{"label": "client/src/App.tsx"}],
            created_from_prompt="read App.tsx",
        )
    )
    console = Console(record=True)

    rewritten = _resolve_pending_question(logger.get_pending_question(), "yes", tmp_path, console, logger)

    assert rewritten is not None
    assert "client/src/App.tsx" in rewritten


def test_cancel_reply_returns_none_and_clears_pending(tmp_path: Path):
    logger = _logger_with_pending(tmp_path)
    console = Console(record=True)

    rewritten = _resolve_pending_question(logger.get_pending_question(), "cancel", tmp_path, console, logger)

    assert rewritten is None
    assert logger.get_pending_question() == {}
    assert "Cancelled" in console.export_text()


def test_bare_yes_without_pending_asks_what_to_continue():
    assert _continuation_clarification("yes", "") is not None
    assert _continuation_clarification("continue", "") is not None
    # With a prior prompt, the existing follow-up handling takes over.
    assert _continuation_clarification("yes", "add a login form") is None
    # A real request is never treated as a continuation.
    assert _continuation_clarification("read app.py", "") is None
