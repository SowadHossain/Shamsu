"""Routing tests for the Git intent override.

Git/repo requests must be classified before web-search, QA, and code-edit
routing so Git prompts reach the Git tools instead of being misread as needing
the web, plain QA, or the patch/coder workflow.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from rich.console import Console

from shamsu.cli import repl

# --- Pure classifier behavior ---------------------------------------------


def test_show_git_status_routes_to_git_not_web():
    prompt = "show git status"
    assert repl.is_git_request(prompt)
    assert repl.is_read_only_git_request(prompt)
    assert not repl.is_git_mutation_request(prompt)


def test_unstaged_changes_is_read_only_git_not_code_edit():
    prompt = "what are the unstaged changes here in this repo?"
    assert repl.is_git_request(prompt)
    assert repl.is_read_only_git_request(prompt)
    assert not repl.is_git_mutation_request(prompt)
    # The code-edit heuristic fires on "change" - the read-only Git guard must
    # override it so the patch/coder workflow is never entered.
    assert repl._looks_like_code_edit_request(prompt)
    assert repl._is_read_only_git_question(prompt)


def test_status_diff_commit_is_git_mutation_not_web():
    prompt = (
        'show git status and diff, then commit the current changes '
        'with message "Improve Git tool support"'
    )
    assert repl.is_git_request(prompt)
    assert repl.is_git_mutation_request(prompt)
    # This exact prompt trips the web-needed heuristic via "current " - the Git
    # override runs first in _handle_request, so it must still be a Git request.
    assert repl._looks_like_web_needed_prompt(prompt)
    # It is a mutation, so it is not treated as a blockable read-only question.
    assert not repl._is_read_only_git_question(prompt)


def test_stage_the_files_is_git_mutation_not_qa():
    prompt = "can you stage the files?"
    assert repl.is_git_request(prompt)
    assert repl.is_git_mutation_request(prompt)


def test_push_to_github_is_git_mutation():
    prompt = "push this to GitHub"
    assert repl.is_git_request(prompt)
    assert repl.is_git_mutation_request(prompt)


def test_weather_prompt_is_web_not_git():
    prompt = "what is the current weather in Dhaka?"
    assert not repl.is_git_request(prompt)
    # Non-Git current-info prompts still reach the web-search heuristic.
    assert repl._looks_like_web_needed_prompt(prompt)


# --- Dispatch behavior of _handle_git_request ------------------------------


def _quiet_console() -> Console:
    return Console(file=open("nul" if repl.sys.platform == "win32" else "/dev/null", "w"))


class _FakeRegistry:
    """Stand-in for AgentToolRegistry that records git tool calls."""

    calls: list[str] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def execute(self, name, arguments):
        _FakeRegistry.calls.append(name)
        if name == "git_status":
            return SimpleNamespace(
                ok=True,
                message="Read git status.",
                data={
                    "is_git_repo": True,
                    "is_dirty": True,
                    "changed_files": ["a.py"],
                    "raw_output": " M a.py",
                    "error": "",
                },
            )
        return SimpleNamespace(
            ok=True,
            message="Git command completed.",
            data={"command": "git diff", "exit_code": 0, "stdout": "diff --git a/a.py", "stderr": ""},
        )


def test_read_only_git_request_uses_git_tools_not_web(tmp_path, monkeypatch):
    _FakeRegistry.calls = []
    monkeypatch.setattr(repl, "AgentToolRegistry", _FakeRegistry)
    monkeypatch.setattr(repl, "_make_approval_manager", lambda *a, **k: None)
    monkeypatch.setattr(repl, "get_current_run", lambda: None)

    def _no_web(*args, **kwargs):
        raise AssertionError("read-only Git must not hit the web assist path")

    monkeypatch.setattr(repl, "_run_web_assist", _no_web)

    asyncio.run(
        repl._handle_git_request(
            "what are the unstaged changes here in this repo?",
            tmp_path,
            _quiet_console(),
        )
    )
    assert _FakeRegistry.calls == ["git_status", "git_diff"]


def test_git_mutation_request_routes_to_agent_chat(tmp_path, monkeypatch):
    recorded: dict[str, str] = {}

    async def _fake_agent_chat(user_input, workspace, console, **kwargs):
        recorded["input"] = user_input

    monkeypatch.setattr(repl, "_run_agent_chat", _fake_agent_chat)

    def _no_web(*args, **kwargs):
        raise AssertionError("Git mutation must not hit the web assist path")

    monkeypatch.setattr(repl, "_run_web_assist", _no_web)

    asyncio.run(
        repl._handle_git_request(
            'show git status and diff, then commit the current changes '
            'with message "Improve Git tool support"',
            tmp_path,
            _quiet_console(),
        )
    )
    # The tool agent was invoked, and it was told to inspect before committing.
    assert "input" in recorded
    assert "git_commit" in recorded["input"]
    assert "git_diff_staged" in recorded["input"]
