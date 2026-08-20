"""The suite must not depend on this machine. Asserted, not assumed.

Three ambient dependencies had already cost real time before these existed:

- A live Ollama on 11434. Tests reached it, and a model call is bounded at
  `request_timeout` (600s) - correct for a real generation, ten minutes of
  nothing in a test. The suite wedged at 59% for over an hour, repeatedly, and
  the "2 failures" in one baseline were a real model answering differently the
  second time.
- The developer's own `~/.shamsu`, which now holds the Telegram bot token and
  the phone's pairing. A bare `TelegramService(tmp_path)` would have opened it,
  and `configure` would have overwritten the token.
- The real Codebase-Memory binary, guarded separately in `conftest.py`.

Each guard is one autouse fixture, and each is easy to disable by accident.
These tests fail loudly if that happens, rather than the suite quietly going
slow and machine-dependent again.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import ollama
import pytest

from tests.conftest import ALLOW_LIVE_OLLAMA_ENV, BLOCKED_CALLS, LiveOllamaCalled

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


def test_the_ollama_sdk_cannot_reach_a_real_server():
    with pytest.raises(LiveOllamaCalled):
        asyncio.run(ollama.AsyncClient(host="http://127.0.0.1:11434").chat(model="x"))


def test_raw_httpx_cannot_reach_a_real_model_server():
    """`LLMManager` bypasses the SDK entirely (`llm/manager.py:290`, `:497`).

    Guarding the `ollama` package alone left that route open, and it is the one
    `_pin_legacy_routing` steers nearly every test file down - which is exactly
    why the first attempt at this guard changed nothing.
    """

    async def call() -> None:
        async with httpx.AsyncClient() as client:
            await client.post(OLLAMA_URL, json={})

    with pytest.raises(LiveOllamaCalled):
        asyncio.run(call())


def test_streaming_to_the_model_server_is_blocked_too():
    async def call() -> None:
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", OLLAMA_URL, json={}):
                pass

    with pytest.raises(LiveOllamaCalled):
        asyncio.run(call())


def test_other_hosts_are_left_alone():
    """The guard is about the model server, not about httpx.

    Blocking every request would break the web tools' own tests, which have
    their own fakes and their own hosts.
    """

    async def call() -> None:
        async with httpx.AsyncClient(timeout=0.01) as client:
            await client.get("http://127.0.0.1:9/never-listening")

    # Any error EXCEPT the guard's: this host is simply not there.
    with pytest.raises(Exception) as caught:
        asyncio.run(call())
    assert not isinstance(caught.value, LiveOllamaCalled)


def test_the_escape_hatch_is_named_in_the_error():
    """Whoever trips this needs to know how to opt out on purpose."""
    try:
        asyncio.run(ollama.AsyncClient().chat(model="x"))
    except LiveOllamaCalled as exc:
        assert ALLOW_LIVE_OLLAMA_ENV in str(exc)
    else:  # pragma: no cover - the guard is missing
        raise AssertionError("a live Ollama call was allowed")


def test_the_install_home_is_never_the_real_one():
    home = os.environ.get("SHAMSU_HOME", "")
    assert home, "SHAMSU_HOME is unset, so ~/.shamsu is live in this run"
    assert Path(home).resolve() != (Path.home() / ".shamsu").resolve()


def test_a_test_can_never_evict_a_model_from_vram():
    """The guard with actual teeth.

    `runtime/ollama.py` talks to the server with plain `urllib`, and
    `unload_model` POSTs `keep_alive: 0` - which frees a loaded model from
    VRAM. A suite able to do that does not just depend on the machine, it
    interferes with whatever else is using the GPU, including a session the
    developer is in the middle of. Neither httpx nor the ollama SDK covers
    this route.
    """
    from shamsu.runtime import ollama as runtime_ollama

    before = len(BLOCKED_CALLS)
    # Returns False rather than raising: it catches OSError, as it should when
    # the server is simply not there. The point is that it never got out.
    assert runtime_ollama.unload_model("some-model") is False
    assert len(BLOCKED_CALLS) == before + 1
    assert "11434" in BLOCKED_CALLS[-1]


def test_the_health_check_is_blocked_too():
    """It is bounded at 2s, but thousands of them is still the machine's time."""
    from shamsu.runtime import ollama as runtime_ollama

    before = len(BLOCKED_CALLS)
    assert runtime_ollama.is_ollama_running() is False
    assert len(BLOCKED_CALLS) == before + 1
