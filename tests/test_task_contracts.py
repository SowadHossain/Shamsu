from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.plans.contracts import (
    TaskContract,
    contracts_from_markdown,
    load_plan_contracts,
    request_scope_expansion,
    run_file_preflight,
    validate_contract,
    write_plan_contracts,
)
from shamsu.plans.store import new_plan_id, write_plan
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import LLMResponse, RunStatus, ToolResult


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False)


def _contract(**overrides) -> TaskContract:
    data = {
        "task_id": "task-001",
        "run_id": "plan-001",
        "objective": "Modify game.py",
        "allowed_write_paths": ["game.py"],
        "expected_write_paths": ["game.py"],
        "planner_proposed_files": ["game.py"],
        "acceptance_criteria": ["score increments on asteroid collision"],
        "verification_requirements": ["python -m py_compile game.py"],
    }
    data.update(overrides)
    return TaskContract(**data)


def test_task_contract_round_trip_preserves_identity_scope_and_requirements(tmp_path: Path):
    contract = _contract(attempt_id="attempt-001", attempt_index=1)

    write_plan_contracts(tmp_path, contract.run_id, [contract])
    loaded = load_plan_contracts(tmp_path, contract.run_id)

    assert loaded[0].task_id == "task-001"
    assert loaded[0].run_id == "plan-001"
    assert loaded[0].attempt_id == "attempt-001"
    assert loaded[0].objective == "Modify game.py"
    assert loaded[0].acceptance_criteria == ["score increments on asteroid collision"]
    assert loaded[0].verification_requirements == ["python -m py_compile game.py"]
    assert loaded[0].allowed_write_paths == ["game.py"]


def test_task_contract_validation_rejects_conflicting_paths(tmp_path: Path):
    contract = _contract(forbidden_paths=["game.py"])

    result = validate_contract(contract, tmp_path)

    assert result.ok is False
    assert any("forbidden path game.py conflicts" in error for error in result.errors)


def test_allowed_write_scope_blocks_unlisted_file(tmp_path: Path):
    (tmp_path / "game.py").write_text("SCORE = 0\n", encoding="utf-8")
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    registry.set_allowed_write_paths(["game.py"])

    allowed = registry.write_file("game.py", "SCORE = 1\n", overwrite=True)
    blocked = registry.write_file("backend/core/forms.py", "class F: pass\n", overwrite=True)

    assert allowed.ok is True
    assert blocked.ok is False
    assert blocked.data["out_of_contract_write"] is True
    assert not (tmp_path / "backend" / "core" / "forms.py").exists()


def test_multiple_allowed_files_work_and_third_file_is_rejected(tmp_path: Path):
    (tmp_path / "game.py").write_text("SCORE = 0\n", encoding="utf-8")
    (tmp_path / "bullet.py").write_text("class Bullet: pass\n", encoding="utf-8")
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    registry.set_allowed_write_paths(["game.py", "bullet.py"])

    assert registry.write_file("game.py", "SCORE = 1\n", overwrite=True).ok is True
    assert registry.write_file("bullet.py", "class Bullet:\n    speed = 2\n", overwrite=True).ok is True
    blocked = registry.write_file("asteroid.py", "class Asteroid: pass\n", overwrite=True)

    assert blocked.ok is False
    assert not (tmp_path / "asteroid.py").exists()


def test_preflight_locks_planner_scope_without_making_irrelevant_file_writable(tmp_path: Path):
    for name in ("game.py", "spaceship.py", "asteroid.py", "bullet.py"):
        (tmp_path / name).write_text("# fixture\n", encoding="utf-8")
    (tmp_path / "README").mkdir()
    (tmp_path / "README" / "spec.md").write_text("Add asteroid collision and scoring.\n", encoding="utf-8")
    contract = _contract(
        objective="Add asteroid collision and scoring",
        allowed_write_paths=["game.py", "bullet.py"],
        expected_write_paths=["game.py", "bullet.py"],
        planner_proposed_files=["game.py", "bullet.py"],
    )

    preflighted = run_file_preflight(contract, tmp_path)

    assert preflighted.allowed_write_paths == ["game.py", "bullet.py"]
    assert "asteroid.py" in preflighted.candidate_files
    assert "backend/core/forms.py" not in preflighted.allowed_write_paths
    assert preflighted.file_selection_rationale


def test_scope_expansion_explicitly_updates_contract(tmp_path: Path):
    contract = _contract()

    result = request_scope_expansion(contract, tmp_path, "collision.py", "existing collision helpers live here")

    assert result.ok is True
    assert "collision.py" in result.contract.allowed_write_paths
    assert result.contract.file_selection_rationale[-1].source == "scope_expansion"


def test_scope_expansion_rejects_missing_reason(tmp_path: Path):
    contract = _contract()

    result = request_scope_expansion(contract, tmp_path, "collision.py", "")

    assert result.ok is False
    assert "collision.py" not in result.contract.allowed_write_paths


@pytest.mark.asyncio
async def test_markdown_plan_compatibility_converts_to_contract_and_executor_uses_scope(
    tmp_path: Path, monkeypatch
):
    from shamsu.cli import repl

    plan_id = new_plan_id()
    markdown = "# Plan\n\n## Steps\n1. Modify `game.py`\n\n## Verification\npython -m py_compile game.py\n"
    write_plan(tmp_path, plan_id, markdown)
    captured: dict[str, object] = {}

    async def fake_run_agent_chat(user_input, workspace, console, session_logger=None, **kwargs):
        captured["user_input"] = user_input
        captured.update(kwargs)

    monkeypatch.setattr(repl, "_run_agent_chat", fake_run_agent_chat)

    await repl._execute_pending_plan(
        {
            "awaiting": "plan_approval",
            "plan_id": plan_id,
            "route": "code_edit",
            "created_from_prompt": "modify the game",
        },
        tmp_path,
        _console(),
        session_logger=None,
    )

    assert captured["use_planner"] is False
    assert captured["allowed_write_paths"] == ("game.py",)
    contract = captured["task_contract"]
    assert isinstance(contract, TaskContract)
    assert contract.allowed_write_paths == ["game.py"]
    assert "python -m py_compile game.py" in contract.verification_requirements
    assert "Task Contract:" in str(captured["user_input"])


class _ContextClient:
    def __init__(self) -> None:
        self.messages_seen: list[list[dict]] = []

    async def chat(self, **kwargs):
        self.messages_seen.append([dict(message) for message in kwargs["messages"]])
        return {"message": {"content": "done", "tool_calls": []}}


class _NoPlanLLM:
    async def run_specialist(self, *_args, **_kwargs):
        return LLMResponse(raw="", model_used="fake")


@pytest.mark.asyncio
async def test_contract_acceptance_and_verification_reach_context_frame(tmp_path: Path):
    contract = _contract(
        objective="Report on game scoring",
        allowed_write_paths=[],
        expected_write_paths=[],
        planner_proposed_files=[],
        verification_requirements=["python -m py_compile game.py"],
    )
    client = _ContextClient()
    loop = AgentChatLoop(
        tmp_path,
        client=client,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        llm=_NoPlanLLM(),
        use_planner=False,
        use_long_term_memory=False,
        hydrate_history=False,
        task_contract=contract,
        verify_changes=False,
    )

    result = await loop.run("summarize the contract")

    sent = "\n".join(str(message.get("content", "")) for message in client.messages_seen[0])
    assert result.status in {RunStatus.COMPLETED, RunStatus.FAILED}
    assert "score increments on asteroid collision" in sent
    assert "python -m py_compile game.py" in sent


def test_scope_expansion_tool_uses_registered_handler(tmp_path: Path):
    contract = _contract()
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    def handler(filepath: str, reason: str) -> ToolResult:
        expanded = request_scope_expansion(contract, tmp_path, filepath, reason)
        return ToolResult(expanded.ok, expanded.message, {"allowed": expanded.contract.allowed_write_paths})

    registry.set_scope_expansion_handler(handler)

    result = registry.execute(
        "request_scope_expansion",
        {"filepath": "collision.py", "reason": "collision helper belongs here"},
    )

    assert result.ok is True
    assert "collision.py" in result.data["allowed"]
