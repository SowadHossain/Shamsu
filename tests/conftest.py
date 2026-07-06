from __future__ import annotations

import pytest

from shamsu.abstract.types import GateResult


class _AlwaysOpenAbstractService:
    """Stand-in used only for AgentOrchestrator's default (no explicit
    abstract_service passed). Tests that construct their own AbstractService
    and pass it explicitly are unaffected - see tests/test_abstract_*.py."""

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def ensure_ready(self, auto_build: bool = True) -> GateResult:
        return GateResult(allowed=True)


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
