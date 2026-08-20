from __future__ import annotations

import os
from typing import Any

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


ALLOW_LIVE_OLLAMA_ENV = "SHAMSU_TESTS_ALLOW_LIVE_OLLAMA"


class LiveOllamaCalled(ConnectionError):
    """Raised instead of letting a test talk to a real model server.

    A `ConnectionError` on purpose: production code already handles the server
    being unreachable, so a blocked call degrades exactly as a down server
    would rather than exploding somewhere that never expected an exception.

    The consequence is that most callers SWALLOW it - `unload_model` catches
    `OSError` and returns False. So "did the guard work" cannot be observed
    from the return value, which is what `BLOCKED_CALLS` is for.
    """


#: Every intercepted call, in order. The only way to tell "the guard stopped
#: it" from "the server happened to be down", since well-behaved callers treat
#: those identically.
BLOCKED_CALLS: list[str] = []


@pytest.fixture(autouse=True)
def _no_live_ollama(monkeypatch):
    """The suite must not depend on whether *this* machine is running Ollama.

    It did, and it cost hours. A test that reaches a live server does not fail
    fast - the loop bounds a model call at `request_timeout`, 600s by default
    (`simple_chat.py`, `asyncio.wait_for`), and that bound is right for a real
    600-second generation. In a test it is ten minutes of nothing per call, and
    a 24-round turn is a wedged suite. Runs here stalled at 59% for over an
    hour, four times, with an ESTABLISHED connection to 127.0.0.1:11434.

    It also made results depend on the machine: the two failures in the first
    baseline of this change were a real model answering non-deterministically,
    and they pass the moment it is out of the picture.

    So: hermetic by default. A test that needs a model injects a fake client -
    which nearly all of them already do. Set `SHAMSU_TESTS_ALLOW_LIVE_OLLAMA=1`
    to opt a run back into hitting a real server on purpose.
    """
    if os.environ.get(ALLOW_LIVE_OLLAMA_ENV, "").strip():
        yield
        return
    try:
        import ollama
    except Exception:  # pragma: no cover - the SDK is a hard dependency
        yield
        return

    def refuse(*args, **_kwargs):
        BLOCKED_CALLS.append(str(args[0]) if args else "?")
        raise LiveOllamaCalled(
            "This test tried to call a real Ollama server. Inject a fake client "
            f"instead, or set {ALLOW_LIVE_OLLAMA_ENV}=1 to allow it."
        )

    async def refuse_async(*_args, **_kwargs):
        refuse()

    for name in ("chat", "generate", "embeddings", "embed", "list", "show", "ps"):
        if hasattr(ollama.Client, name):
            monkeypatch.setattr(ollama.Client, name, refuse, raising=False)
        if hasattr(ollama.AsyncClient, name):
            monkeypatch.setattr(ollama.AsyncClient, name, refuse_async, raising=False)

    # The SDK is only half of it. `LLMManager` - which the legacy path uses, and
    # which `_pin_legacy_routing` above steers nearly every test file into -
    # talks to `/api/generate` and `/api/chat` with RAW httpx
    # (`llm/manager.py:290`, `:497`), so patching the ollama package alone left
    # the busiest route to a live server wide open. That is why the suite still
    # wedged with an ESTABLISHED connection to 11434 after the first attempt.
    import httpx

    def _is_model_server(url: Any) -> bool:
        return "11434" in str(url)

    real_post = httpx.AsyncClient.post
    real_stream = httpx.AsyncClient.stream
    real_send = httpx.AsyncClient.send

    async def guarded_post(self, url, *args, **kwargs):
        if _is_model_server(url):
            refuse()
        return await real_post(self, url, *args, **kwargs)

    def guarded_stream(self, method, url, *args, **kwargs):
        if _is_model_server(url):
            refuse()
        return real_stream(self, method, url, *args, **kwargs)

    async def guarded_send(self, request, *args, **kwargs):
        if _is_model_server(getattr(request, "url", "")):
            refuse()
        return await real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", guarded_post, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "stream", guarded_stream, raising=False)
    monkeypatch.setattr(httpx.AsyncClient, "send", guarded_send, raising=False)

    # And the third route, which is the one with teeth. `runtime/ollama.py`
    # speaks to the server with plain `urllib` - health checks, `/api/ps`, and
    # `unload_model`, which POSTs `keep_alive: 0`. That last one EVICTS A MODEL
    # FROM VRAM. A test suite that can do that does not merely depend on the
    # machine, it interferes with whatever else the machine is doing - like a
    # session the developer is in the middle of.
    import urllib.request

    real_urlopen = urllib.request.urlopen

    def guarded_urlopen(url, *args, **kwargs):
        target = getattr(url, "full_url", url)
        if _is_model_server(target):
            refuse(target)
        return real_urlopen(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", guarded_urlopen, raising=False)
    yield


class LiveTelegramCalled(ConnectionError):
    """Raised instead of letting a test talk to `api.telegram.org`.

    Same shape and same reasoning as :class:`LiveOllamaCalled`: the diagnostics
    layer already treats any transport failure as "unreachable", so a blocked
    call degrades the way a real outage would. Which again means the block is
    invisible from the return value - see `BLOCKED_CALLS`.
    """


@pytest.fixture(autouse=True)
def _no_live_telegram(monkeypatch):
    """The suite must not reach a real bot, even if a token is lying around.

    `diagnostics.probe()` and `delete_webhook()` are the first code here that
    calls Telegram from a plain request handler rather than from the poll loop,
    and `deleteWebhook` MUTATES someone's bot. A test that ran it against a
    developer's real token would silently reconfigure their bot, which is the
    same class of harm as `unload_model` evicting their model from VRAM.

    Tests exercise these by passing `base_url=` at a local fake, which is
    exactly why that parameter exists.
    """
    import httpx

    real_post = httpx.Client.post
    real_send = httpx.Client.send

    def _is_telegram(url: Any) -> bool:
        return "api.telegram.org" in str(url)

    def guarded_post(self, url, *args, **kwargs):
        if _is_telegram(url):
            BLOCKED_CALLS.append(str(url))
            raise LiveTelegramCalled(
                "This test tried to call the real Telegram API. Pass base_url= "
                "pointing at a local fake instead."
            )
        return real_post(self, url, *args, **kwargs)

    def guarded_send(self, request, *args, **kwargs):
        if _is_telegram(getattr(request, "url", "")):
            BLOCKED_CALLS.append(str(getattr(request, "url", "?")))
            raise LiveTelegramCalled("This test tried to call the real Telegram API.")
        return real_send(self, request, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "post", guarded_post, raising=False)
    monkeypatch.setattr(httpx.Client, "send", guarded_send, raising=False)

    # The poll loop is async and uses its own client. A test that configures a
    # token and starts the bridge would otherwise long-poll the real API.
    real_async_post = httpx.AsyncClient.post

    async def guarded_async_post(self, url, *args, **kwargs):
        if _is_telegram(url):
            BLOCKED_CALLS.append(str(url))
            raise LiveTelegramCalled("This test tried to call the real Telegram API.")
        return await real_async_post(self, url, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", guarded_async_post, raising=False)
    yield


@pytest.fixture(autouse=True)
def _install_home_not_ambient(monkeypatch, tmp_path_factory):
    """No test may read - or overwrite - the real `~/.shamsu`.

    The Telegram bot token and the phone's pairing now live there, install-wide
    and on purpose. Without this, a bare `TelegramService(tmp_path)` in any test
    would open the developer's actual state database and `configure` would
    overwrite their actual token, logging their phone out. Tests that care about
    the install root ask for the `home` fixture and set their own.
    """
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path_factory.mktemp("shamsu-home")))


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


