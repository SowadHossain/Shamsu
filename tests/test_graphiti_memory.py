from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from shamsu.agents.orchestrator import AgentOrchestrator
from shamsu.memory.graphiti_adapter import FALKORDB_IMAGE, GraphitiAdapter, is_local_uri
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


def test_agent_mode_proceeds_degraded_when_graphiti_unavailable(tmp_path):
    """A workspace with no Graphiti (fresh checkout, PRD-build target, ...) must
    NOT be bricked: the orchestrator runs in degraded mode on the local SQLite
    fallback instead of hard-blocking behind '/memory setup'."""
    memory = MemoryService(tmp_path, adapter=FakeGraphitiAdapter(available=False))
    abstract = AbstractService(tmp_path, adapter=FakeCodebaseMemoryAdapter(available=True))

    result = AgentOrchestrator(tmp_path, abstract_service=abstract, memory_service=memory).run("explain auth")

    # Proceeds to normal routing (not handled by a memory block).
    assert result.handled is False
    assert result.action != "memory.blocked"


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


def test_memory_remember_keeps_local_success_when_mirror_raises(tmp_path):
    class BrokenMirror(FakeGraphitiAdapter):
        def remember(self, workspace, text, kind, metadata=None):  # noqa: ANN001
            raise RuntimeError("graph write unavailable")

    service = MemoryService(tmp_path, adapter=BrokenMirror(available=True))

    result = service.remember("remember this: prefer narrow patches", "workflow_rule")

    assert result["ok"] is True
    assert result["local"] is True
    assert result["degraded"] is True
    assert "graph write unavailable" in result["mirror"]["error"]
    assert service.fallback._all()[0].text.endswith("prefer narrow patches")


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


def test_memory_policy_rejects_failed_automatic_completion():
    decision = MemoryPolicy().decide(
        "Task completed: fixed parser",
        "task_summary",
        {"automatic": True, "outcome": "failed", "intent": "bug_fix"},
    )

    assert decision.should_store is False
    assert "outcome" in decision.reason


def test_memory_policy_rejects_unverified_automatic_bug_lesson():
    decision = MemoryPolicy().decide(
        "The fix was to add a bounds check",
        "bug_lesson",
        {"automatic": True, "outcome": "success_unverified", "intent": "bug_fix"},
    )

    assert decision.should_store is False
    assert decision.reason == "bug lesson is not verified"


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
    """Memory is local SQLite now, so a missing Graphiti no longer blocks work.

    The honest-failure invariant moved rather than disappeared. The gate may
    allow the turn, but nothing is allowed to claim a Graphiti backend that is
    not there: storage reports as local, and the health check still says why.
    """
    service = MemoryService(tmp_path, adapter=FakeGraphitiAdapter(available=False))

    gate = service.ensure_ready()
    status = service.status()

    assert gate.allowed is True
    assert status.storage_mode == "local"
    assert status.local_available is True
    assert status.health.ok is False
    assert "missing" in status.health.message.lower()


def test_ensure_local_backend_skips_non_default_uri(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")
    adapter._write_config(tmp_path, {**adapter.default_config(tmp_path), "graph_backend_uri": "bolt://localhost:7687"})

    assert adapter._ensure_local_backend(tmp_path) is None


def test_ensure_local_backend_noop_when_already_reachable(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")

    with patch.object(GraphitiAdapter, "_check_backend", return_value=""), patch(
        "shamsu.memory.graphiti_adapter.shutil.which"
    ) as which:
        result = adapter._ensure_local_backend(tmp_path)

    assert result == {"ok": True, "message": "FalkorDB backend already reachable."}
    which.assert_not_called()


def test_check_llm_endpoint_reports_unreachable_ollama(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")

    with patch("shamsu.memory.graphiti_adapter.socket.create_connection", side_effect=OSError("refused")):
        message = adapter._check_llm_endpoint("http://localhost:11434/v1")

    assert "Ollama" in message
    assert "not reachable" in message


def test_check_llm_endpoint_ok_when_reachable(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")

    with patch("shamsu.memory.graphiti_adapter.socket.create_connection"):
        message = adapter._check_llm_endpoint("http://localhost:11434/v1")

    assert message == ""


def test_healthcheck_fails_when_llm_endpoint_unreachable(tmp_path):
    """Regression test: a reachable FalkorDB used to be enough for `healthcheck()`
    to report Graphiti as ready even when Ollama (the actual LLM/embedding
    backend behind every read and write) was down. `healthcheck()` must
    consult `_check_llm_endpoint` and fail when it does."""
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")
    adapter._write_config(tmp_path, adapter.default_config(tmp_path))

    with patch.object(GraphitiAdapter, "resolve_python", return_value=Path("fake-python")), patch.object(
        GraphitiAdapter, "_graphiti_version", return_value="0.29.2"
    ), patch.object(GraphitiAdapter, "_check_backend", return_value=""), patch.object(
        GraphitiAdapter, "_check_llm_endpoint", return_value="Ollama unreachable"
    ):
        health = adapter.healthcheck(tmp_path)

    assert health.ok is False
    assert health.message == "Ollama unreachable"


def test_start_local_falkordb_reports_missing_docker_and_winget(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")

    with patch("shamsu.memory.graphiti_adapter.sys.platform", "win32"), patch(
        "shamsu.memory.graphiti_adapter._KNOWN_DOCKER_CLI_PATHS", []
    ), patch("shamsu.memory.graphiti_adapter.shutil.which", return_value=None):
        result = adapter._start_local_falkordb()

    assert result["ok"] is False
    assert "winget was not found" in result["error"]


def test_start_local_falkordb_installs_docker_via_winget_when_missing(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")
    install_calls: list[list[str]] = []

    def fake_which(name):
        return "winget" if name == "winget" else None

    def fake_run(cmd, **kwargs):
        install_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "installed", "")

    with patch("shamsu.memory.graphiti_adapter.sys.platform", "win32"), patch(
        "shamsu.memory.graphiti_adapter._KNOWN_DOCKER_CLI_PATHS", []
    ), patch("shamsu.memory.graphiti_adapter.shutil.which", side_effect=fake_which), patch(
        "shamsu.memory.graphiti_adapter._no_window_flags", return_value=0
    ), patch("shamsu.memory.graphiti_adapter.subprocess.run", side_effect=fake_run):
        result = adapter._start_local_falkordb()

    assert result["ok"] is False
    assert "isn't visible to this SHAMSU process yet" in result["error"]
    winget_call = next(cmd for cmd in install_calls if cmd[0] == "winget")
    assert winget_call[:4] == ["winget", "install", "--id", "Docker.DockerDesktop"]


def test_start_local_falkordb_starts_docker_desktop_when_daemon_down(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "inspect":
            return subprocess.CompletedProcess(cmd, 1, "", "no such container")
        return subprocess.CompletedProcess(cmd, 0, "container-id", "")

    with patch("shamsu.memory.graphiti_adapter.shutil.which", return_value="docker"), patch(
        "shamsu.memory.graphiti_adapter.subprocess.run", side_effect=fake_run
    ), patch.object(GraphitiAdapter, "_docker_daemon_ready", return_value=False), patch.object(
        GraphitiAdapter, "_start_docker_desktop_app", return_value=True
    ) as start_app, patch.object(GraphitiAdapter, "_wait_for_local_port", return_value=True):
        result = adapter._start_local_falkordb()

    assert result["ok"] is True
    start_app.assert_called_once()


def test_start_local_falkordb_fails_when_daemon_wont_start(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")

    with patch("shamsu.memory.graphiti_adapter.shutil.which", return_value="docker"), patch.object(
        GraphitiAdapter, "_docker_daemon_ready", return_value=False
    ), patch.object(GraphitiAdapter, "_start_docker_desktop_app", return_value=False):
        result = adapter._start_local_falkordb()

    assert result["ok"] is False
    assert "daemon isn't running" in result["error"]


def test_start_local_falkordb_uses_documented_docker_run_command(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "inspect":
            return subprocess.CompletedProcess(cmd, 1, "", "no such container")
        return subprocess.CompletedProcess(cmd, 0, "container-id", "")

    with patch("shamsu.memory.graphiti_adapter.shutil.which", return_value="docker"), patch(
        "shamsu.memory.graphiti_adapter.subprocess.run", side_effect=fake_run
    ), patch.object(GraphitiAdapter, "_wait_for_local_port", return_value=True):
        result = adapter._start_local_falkordb()

    assert result["ok"] is True
    run_call = next(cmd for cmd in calls if cmd[1] == "run")
    # Only 6379 (the graph DB) is published; we deliberately do NOT map 3000
    # (FalkorDB's unused browser UI) so it can't collide with a dev server.
    assert run_call == [
        "docker", "run", "-d", "--name", "shamsu-graphiti-falkordb",
        "-p", "6379:6379", FALKORDB_IMAGE,
    ]
    assert "3000:3000" not in run_call


def test_start_local_falkordb_reuses_existing_stopped_container(tmp_path):
    adapter = GraphitiAdapter(tool_dir=tmp_path / "tools" / "graphiti")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[1] == "inspect":
            return subprocess.CompletedProcess(cmd, 0, "false\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with patch("shamsu.memory.graphiti_adapter.shutil.which", return_value="docker"), patch(
        "shamsu.memory.graphiti_adapter.subprocess.run", side_effect=fake_run
    ), patch.object(GraphitiAdapter, "_wait_for_local_port", return_value=True):
        result = adapter._start_local_falkordb()

    assert result["ok"] is True
    assert any(cmd[1] == "start" for cmd in calls)
    assert not any(cmd[1] == "run" for cmd in calls)
