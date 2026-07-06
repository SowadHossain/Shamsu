from __future__ import annotations

from shamsu.abstract.service import AbstractService
from shamsu.agents.orchestrator import AgentOrchestrator
from tests.test_abstract_service import FakeCodebaseMemoryAdapter


def test_orchestrator_blocks_normal_code_agent_mode_when_unavailable(tmp_path):
    # Passing abstract_service explicitly bypasses conftest's default open-gate
    # stand-in, which only backs AgentOrchestrator's no-argument fallback.
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=False))

    result = AgentOrchestrator(tmp_path, abstract_service=service).run("explain how auth works")

    assert result.handled is True
    assert result.action == "abstract.blocked"
    assert "Codebase-Memory MCP is required" in result.message
    assert "/abstract setup" in result.message


def test_orchestrator_enters_normal_mode_when_codebase_memory_healthy(tmp_path):
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=True))

    result = AgentOrchestrator(tmp_path, abstract_service=service).run("explain how auth works")

    assert result.handled is False
    assert result.action == ""


def test_meta_workspace_questions_are_not_blocked_by_the_gate(tmp_path):
    # Workspace meta-queries (files/location) resolve before the gate is even
    # checked, matching the "always allowed" spirit for non-code-edit asks.
    service = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=False))

    result = AgentOrchestrator(tmp_path, abstract_service=service).run("what folder are you in rn?")

    assert result.handled is True
    assert result.action == "workspace.location"
