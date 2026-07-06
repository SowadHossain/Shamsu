from __future__ import annotations

from pathlib import Path

from shamsu.agents.orchestrator import AgentOrchestrator
from shamsu.memory.graphiti_adapter import GraphitiAdapter, is_local_uri
from shamsu.memory.policy import MemoryPolicy
from shamsu.memory.service import REQUIRED_MEMORY_MESSAGE, MemoryService
from shamsu.memory.types import GraphitiHealth, LongTermMemory
from tests.test_abstract_service import FakeCodebaseMemoryAdapter
from shamsu.abstract.service import AbstractService


class FakeGraphitiAdapter:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.setup_calls = 0
        self.repair_calls = 0
        self.remembered: list[tuple[str, str]] = []
        self.memories = [LongTermMemory(kind="workflow_rule", text="Never claim build success unless exit code is 0.", memory_id="m1")]

    def healthcheck(self, workspace: Path) -> GraphitiHealth:
        if self.available:
            return GraphitiHealth(True, tool_path="/fake/graphiti/python", config_path=str(workspace / ".shamsu" / "memory" / "config.json"), version="0.fake", message="ready")
        return GraphitiHealth(False, message="Graphiti missing")

    def setup(self, workspace: Path) -> dict:
        self.setup_calls += 1
        self.available = True
        return {"ok": True, "path": "/fake/graphiti/python"}

    def repair(self, workspace: Path) -> dict:
        self.repair_calls += 1
        self.available = True
        return {"ok": True, "message": "ready"}

    def add_episode(self, workspace: Path, text: str, metadata=None) -> dict:
        return {"ok": True}

    def remember(self, workspace: Path, text: str, kind: str, metadata=None) -> dict:
        self.remembered.append((kind, text))
        return {"ok": True, "id": f"m{len(self.remembered)}"}

    def search(self, workspace: Path, query: str, limit: int = 8, filters=None) -> dict:
        return {"ok": True, "results": [{"id": "m1", "text": self.memories[0].text, "kind": self.memories[0].kind}]}

    def get_relevant(self, workspace: Path, user_prompt: str, task_type=None, limit: int = 8):
        return self.memories[:limit]

    def forget(self, workspace: Path, memory_id_or_query: str) -> dict:
        return {"ok": True, "forgot": memory_id_or_query}


def test_startup_blocks_normal_agent_mode_when_graphiti_unavailable(tmp_path):
    memory = MemoryService(tmp_path, adapter=FakeGraphitiAdapter(available=False))
    abstract = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=True))

    result = AgentOrchestrator(tmp_path, abstract_service=abstract, memory_service=memory).run("explain auth")

    assert result.handled is True
    assert result.action == "memory.blocked"
    assert "Graphiti memory backend is required" in result.message
    assert "/memory setup" in result.message


def test_startup_enters_normal_mode_when_graphiti_healthy(tmp_path):
    memory = MemoryService(tmp_path, adapter=FakeGraphitiAdapter(available=True))
    abstract = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=True))

    result = AgentOrchestrator(tmp_path, abstract_service=abstract, memory_service=memory).run("explain auth")

    assert result.handled is False


def test_memory_setup_uses_managed_tool_path(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")

    assert str(adapter.tool_dir).endswith(str(Path("tools") / "graphiti"))


def test_remote_graphiti_url_is_rejected():
    assert is_local_uri("https://example.com/graphiti") is False
    assert is_local_uri("http://localhost:11434/v1") is True
    assert is_local_uri("falkor://127.0.0.1:6379") is True
    assert is_local_uri("file:///tmp/graphiti") is True


def test_memory_remember_stores_explicit_memory(tmp_path):
    adapter = FakeGraphitiAdapter(available=True)
    service = MemoryService(tmp_path, adapter=adapter)

    result = service.remember("remember this: always verify command exit code", "workflow_rule")

    assert result["ok"] is True
    assert adapter.remembered[0][0] == "workflow_rule"


def test_memory_search_returns_fake_adapter_results(tmp_path):
    service = MemoryService(tmp_path, adapter=FakeGraphitiAdapter(available=True))

    result = service.search("build success")

    assert result["ok"] is True
    assert "Never claim build success" in result["results"][0]["text"]


def test_memory_forget_handles_matching_memory(tmp_path):
    service = MemoryService(tmp_path, adapter=FakeGraphitiAdapter(available=True))

    result = service.forget("m1")

    assert result["ok"] is True
    assert result["forgot"] == "m1"


def test_memory_policy_stores_remember_this():
    decision = MemoryPolicy().decide("remember this: user prefers visible progress")

    assert decision.should_store is True
    assert decision.kind == "user_preference"


def test_memory_policy_stores_from_now_on():
    decision = MemoryPolicy().decide("from now on never claim build success unless exit code is 0")

    assert decision.should_store is True
    assert decision.kind == "workflow_rule"


def test_memory_policy_rejects_transient_errors():
    decision = MemoryPolicy().decide("temporary error: pytest failed once")

    assert decision.should_store is False


def test_memory_policy_rejects_secrets():
    decision = MemoryPolicy().decide("remember this API_KEY=abc123")

    assert decision.should_store is False


def test_memory_policy_rejects_full_source_files():
    source = "\n".join(["def f():", "    pass"] * 30)

    decision = MemoryPolicy().decide(source, "architecture_note")

    assert decision.should_store is False


def test_relevant_memories_are_rendered_compactly(tmp_path):
    service = MemoryService(tmp_path, adapter=FakeGraphitiAdapter(available=True))

    rendered = service.render_relevant("build succeeded?")

    assert "Relevant long-term memory" in rendered
    assert "[workflow_rule]" in rendered


def test_no_fake_success_when_graphiti_tool_missing(tmp_path):
    service = MemoryService(tmp_path, adapter=FakeGraphitiAdapter(available=False))

    gate = service.ensure_ready()

    assert gate.allowed is False
    assert gate.reason == REQUIRED_MEMORY_MESSAGE
