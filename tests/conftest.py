from __future__ import annotations

import pytest

from shamsu.abstract.types import GateResult
from shamsu.memory.types import MemoryGate


@pytest.fixture(autouse=True)
def _memory_queue_cleanup():
    from shamsu.memory.queue import reset_memory_queues

    yield
    reset_memory_queues(timeout=0.2)


@pytest.fixture(autouse=True)
def _pin_legacy_routing(monkeypatch, request):
    """Existing tests drive `_handle_request` to assert ROUTER behaviour.

    Simple mode is now the production default and short-circuits that router
    before it runs, so without this those tests would silently stop testing what
    they were written for - and worse, they would reach a real Ollama and hang,
    because the simple loop builds a live client.

    The router still ships behind SHAMSU_LEGACY_ROUTING, so pinning it here is
    what those tests actually mean. `tests/test_simple_chat.py` owns the new
    default and opts out.
    """
    if request.node.fspath.basename == "test_simple_chat.py":
        yield
        return
    monkeypatch.setenv("SHAMSU_LEGACY_ROUTING", "1")
    yield


class _AlwaysOpenAbstractService:
    """Stand-in used only for AgentOrchestrator's default (no explicit
    abstract_service passed). Tests that construct their own AbstractService
    and pass it explicitly are unaffected - see tests/test_abstract_*.py."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def ensure_ready(self, auto_build: bool = True) -> GateResult:
        return GateResult(allowed=True)



class _AlwaysOpenMemoryService:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def ensure_ready(self) -> MemoryGate:
        return MemoryGate(allowed=True)

    def ensure_ready_degraded(self) -> MemoryGate:
        return MemoryGate(allowed=True)

    def render_relevant(self, *_args, **_kwargs) -> str:
        return ""


@pytest.fixture(autouse=True)
def _graphiti_memory_gate_open(monkeypatch):
    from shamsu.agents import orchestrator as orchestrator_module
    from shamsu.agents import chat_loop as chat_loop_module
    from shamsu.llm import manager as manager_module

    monkeypatch.setattr(orchestrator_module, "MemoryService", _AlwaysOpenMemoryService)
    monkeypatch.setattr(chat_loop_module, "MemoryService", _AlwaysOpenMemoryService)
    monkeypatch.setattr(manager_module, "MemoryService", _AlwaysOpenMemoryService)
@pytest.fixture(autouse=True)
def _codebase_memory_gate_open(monkeypatch):
    """Default the Codebase-Memory MCP startup gate to open for the existing
    test suite, which predates this requirement and doesn't install the real
    upstream binary. Only replaces the name AgentOrchestrator falls back to
    when no abstract_service is injected; tests that specifically cover gate
    behaviour construct and pass their own AbstractService/fake adapter."""
    from shamsu.agents import orchestrator as orchestrator_module

    monkeypatch.setattr(orchestrator_module, "AbstractService", _AlwaysOpenAbstractService)


@pytest.fixture(autouse=True)
def _codebase_memory_binary_not_ambient(monkeypatch, tmp_path_factory):
    """Tests must not depend on whether *this* machine happens to have the
    real Codebase-Memory MCP binary installed under ~/.shamsu/tools/ (e.g.
    from a developer running `/abstract setup` for real). Point the *default*
    tool dir at an empty scratch directory so a bare `CodebaseMemoryAdapter()`
    is hermetic either way - this leaves explicit `tool_dir=`/env-var override
    behavior (see tests/test_codebase_memory_adapter.py) untouched, and tests
    that want a healthy adapter inject their own fake
    (tests/test_abstract_service.py's FakeCodebaseMemoryAdapter)."""
    import shamsu.tools.codebase_memory as codebase_memory_module

    empty_dir = tmp_path_factory.mktemp("no-codebase-memory-mcp")
    monkeypatch.setattr(codebase_memory_module, "default_tool_dir", lambda: empty_dir)


@pytest.fixture(autouse=True)
def _model_tier_reset(monkeypatch):
    """The active model tier is process-global state (model_for_role() is
    called from many places with no workspace argument - see
    shamsu/runtime/models.py). Reset it to the default tier for every test so
    one test's /models tier switch can't leak into the next."""
    import shamsu.runtime.models as models_module

    monkeypatch.setattr(models_module, "_ACTIVE_TIER", models_module.DEFAULT_TIER)
    monkeypatch.setattr(models_module, "_ACTIVE_MODEL_OVERRIDE", "")


@pytest.fixture(autouse=True)
def _model_presence_cache_reset(monkeypatch):
    """`_ensure_model` remembers confirmed-present models for the process so it
    stops re-shelling `ollama list` before every call. That cache is global, so a
    test whose fake reports a model installed would otherwise suppress the pull
    path in every later test."""
    import shamsu.llm.manager as manager_module

    monkeypatch.setattr(manager_module, "_MODELS_CONFIRMED_PRESENT", set())


