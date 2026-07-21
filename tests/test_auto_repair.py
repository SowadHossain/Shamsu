"""Gap E1: the agent loop verified but never repaired.

A 30-minute autonomous run could end "UNCONFIRMED: SyntaxError in game.js
line 4" while the machinery to fix a one-line error (RepairLoop, already used
by freeform/full_pipeline) sat uninvited in the same codebase. Now a failed
autonomous verify gets ONE bounded, lightweight repair pass; a repair that
doesn't fix it falls through to the same honest UNCONFIRMED note as before.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import shamsu.agents.chat_loop as chat_loop_module
from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import LLMResponse
from shamsu.verify.gate import VerifyOutcome


class _WriteThenAnswerClient:
    """One write_file call, then a final answer claiming success."""

    def __init__(self, filename: str, content: str) -> None:
        self._responses = [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "w1",
                            "function": {
                                "name": "write_file",
                                "arguments": {"filepath": filename, "content": content},
                            },
                        }
                    ],
                }
            },
            {"message": {"content": "Done, the file is written.", "tool_calls": []}},
        ]

    async def chat(self, model, messages, tools, stream, options):  # noqa: ANN001
        return self._responses.pop(0)


class _SchemaLLM:
    """Planner double that supports generate_structured (repair requires it)."""

    async def run_specialist(self, specialist, pack):  # noqa: ANN001
        return LLMResponse(raw="", model_used="fake")

    async def generate_structured(self, role, system, prompt, schema, **kwargs):  # noqa: ANN001
        return "{\"needs_input\": false}"


def _loop(workspace: Path, client) -> AgentChatLoop:
    return AgentChatLoop(
        workspace,
        client=client,
        tools=AgentToolRegistry(workspace, approval_func=lambda _r: True),
        llm=_SchemaLLM(),
        long_running=True,   # the verify gate only runs on autonomous runs
    )


@pytest.mark.asyncio
async def test_failed_verify_triggers_one_repair_and_reports_success(tmp_path: Path, monkeypatch):
    calls: list[dict] = []

    def _fake_repair(workspace, files, **kwargs):
        calls.append(kwargs)
        return VerifyOutcome(verified=True, exit_code=0, command="py_compile", summary="compiles now")

    monkeypatch.setattr("shamsu.verify.gate.verify_and_repair", _fake_repair)
    monkeypatch.setattr(
        chat_loop_module,
        "verify_only",
        lambda *a, **k: VerifyOutcome(verified=False, exit_code=1, command="py_compile", summary="SyntaxError"),
    )

    client = _WriteThenAnswerClient("app.py", "def broken(:\n    pass\n")
    result = await _loop(tmp_path, client).run("write app.py")

    assert len(calls) == 1
    assert calls[0]["max_attempts"] == 1          # bounded, not a retry storm
    assert calls[0]["lightweight"] is True        # never pip/npm mid-chat
    assert "[verified after repair]" in result.final
    assert "UNCONFIRMED" not in result.final


@pytest.mark.asyncio
async def test_failed_repair_keeps_the_honest_unconfirmed_note(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "shamsu.verify.gate.verify_and_repair",
        lambda *a, **k: VerifyOutcome(verified=False, exit_code=1, command="py_compile", summary="still broken"),
    )
    monkeypatch.setattr(
        chat_loop_module,
        "verify_only",
        lambda *a, **k: VerifyOutcome(verified=False, exit_code=1, command="py_compile", summary="SyntaxError"),
    )

    client = _WriteThenAnswerClient("app.py", "def broken(:\n")
    result = await _loop(tmp_path, client).run("write app.py")

    assert "UNCONFIRMED" in result.final
    assert "repair attempt did not fix it" in result.final


@pytest.mark.asyncio
async def test_repair_can_be_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(chat_loop_module, "_AUTO_REPAIR_ENABLED", False)

    def _must_not_run(*a, **k):
        raise AssertionError("repair must not run when disabled")

    monkeypatch.setattr("shamsu.verify.gate.verify_and_repair", _must_not_run)
    monkeypatch.setattr(
        chat_loop_module,
        "verify_only",
        lambda *a, **k: VerifyOutcome(verified=False, exit_code=1, command="c", summary="fail"),
    )

    client = _WriteThenAnswerClient("app.py", "x")
    result = await _loop(tmp_path, client).run("write app.py")

    assert "UNCONFIRMED" in result.final
    assert "repair attempt" not in result.final   # no false claim of an attempt


@pytest.mark.asyncio
async def test_a_repair_error_degrades_to_unconfirmed(tmp_path: Path, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("proposer exploded")

    monkeypatch.setattr("shamsu.verify.gate.verify_and_repair", _boom)
    monkeypatch.setattr(
        chat_loop_module,
        "verify_only",
        lambda *a, **k: VerifyOutcome(verified=False, exit_code=1, command="c", summary="fail"),
    )

    client = _WriteThenAnswerClient("app.py", "x")
    result = await _loop(tmp_path, client).run("write app.py")

    assert "UNCONFIRMED" in result.final          # never breaks the turn


@pytest.mark.asyncio
async def test_a_passing_verify_never_runs_repair(tmp_path: Path, monkeypatch):
    def _must_not_run(*a, **k):
        raise AssertionError("repair must not run on a green verify")

    monkeypatch.setattr("shamsu.verify.gate.verify_and_repair", _must_not_run)
    monkeypatch.setattr(
        chat_loop_module,
        "verify_only",
        lambda *a, **k: VerifyOutcome(verified=True, exit_code=0, command="c", summary="ok"),
    )

    client = _WriteThenAnswerClient("app.py", "x = 1\n")
    result = await _loop(tmp_path, client).run("write app.py")

    assert "[verified]" in result.final
