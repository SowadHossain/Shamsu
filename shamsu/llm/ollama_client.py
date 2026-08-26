"""Ollama client construction for the small harness."""

from __future__ import annotations

import httpx
import ollama

from shamsu.runtime.timeouts import TimeoutConfig

OLLAMA_BASE_URL = "http://localhost:11434"


def default_ollama_client(
    base_url: str = OLLAMA_BASE_URL,
    timeout_config: TimeoutConfig | None = None,
) -> ollama.AsyncClient:
    """Build the async Ollama client used by the TUI chat loop."""
    timeouts = timeout_config or TimeoutConfig.from_env()
    timeout = httpx.Timeout(
        timeout=None,
        connect=timeouts.connect_timeout,
        read=None,
        write=None,
        pool=None,
    )
    return ollama.AsyncClient(host=base_url, timeout=timeout)
