"""Tests for the end-of-plan integration verify gate (rest of G4): the chat loop
reports which files it changed, and _execute_plan verifies the whole set once at
the end with an honest verdict."""
from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

import shamsu.cli.repl as repl
from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.verify.gate import VerifyOutcome


class _NoPlanLLM:
    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        raise RuntimeError("no planner in tests")


class _ScriptedClient:
    def __init__(self, responses: list[dict]) -> None:
        self._responses = list(responses)

    async def chat(self, model, messages, tools, stream, options):  # noqa: ANN001
        return self._responses.pop(0)


def _tool_response(name: str, arguments: dict) -> dict:
    return {"message": {"content": "", "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": arguments}}]}}


def _text_response(content: str) -> dict:
    return {"message": {"content": content, "tool_calls": []}}


def _console() -> tuple[Console, StringIO]:
    buffer = StringIO()
    return Console(file=buffer, force_terminal=False, width=100), buffer


# ---------------------------------------------------------------------------
# The loop reports changed files
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_loop_reports_changed_files(tmp_path: Path):
    client = _ScriptedClient(
        [
            _tool_response("write_file", {"filepath": "hello.py", "content": "print('hi')\n"}),
            _text_response("Done, created hello.py."),
        ]
    )
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        llm=_NoPlanLLM(),
    )
    result = await loop.run("create hello.py that prints hi")
    assert "hello.py" in result.changed_files
    assert (tmp_path / "hello.py").is_file()


# ---------------------------------------------------------------------------
# _verify_completed_plan honest verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_completed_plan_reports_failure(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        repl,
        "verify_only",
        lambda *a, **k: VerifyOutcome(
            verified=False, exit_code=1, command="python -m py_compile a.py",
            summary="Verification FAILED: `python -m py_compile a.py` (exit 1).",
        ),
    )
    console, buffer = _console()
    await repl._verify_completed_plan(["a.py"], tmp_path, console, None)
    out = buffer.getvalue()
    assert "UNVERIFIED" in out
    assert "did NOT pass" in out


@pytest.mark.asyncio
async def test_verify_completed_plan_reports_success(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        repl,
        "verify_only",
        lambda *a, **k: VerifyOutcome(
            verified=True, exit_code=0, command="python -m py_compile a.py",
            summary="Verification passed: `python -m py_compile a.py` (exit 0).",
        ),
    )
    console, buffer = _console()
    await repl._verify_completed_plan(["a.py"], tmp_path, console, None)
    assert "verified" in buffer.getvalue().lower()


@pytest.mark.asyncio
async def test_verify_completed_plan_unverifiable_is_quiet_but_honest(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        repl,
        "verify_only",
        lambda *a, **k: VerifyOutcome(verified=False, unverifiable=True, summary="UNVERIFIED"),
    )
    console, buffer = _console()
    await repl._verify_completed_plan(["notes.txt"], tmp_path, console, None)
    assert "UNVERIFIED" in buffer.getvalue()


@pytest.mark.asyncio
async def test_verify_completed_plan_skips_when_no_changes(tmp_path: Path, monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("verify must not run with no changed files")

    monkeypatch.setattr(repl, "verify_only", _boom)
    console, buffer = _console()
    await repl._verify_completed_plan([], tmp_path, console, None)
    assert buffer.getvalue() == ""
