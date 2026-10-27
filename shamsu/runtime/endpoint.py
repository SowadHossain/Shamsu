"""The operator-selected Ollama service, shared by runtime checks and inference."""
from __future__ import annotations

import os
from urllib.parse import urlsplit

LOCAL_LLM_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def ollama_endpoint() -> str:
    endpoint = os.environ.get("SHAMSU_OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path
    ):
        raise ValueError("SHAMSU_OLLAMA_BASE_URL must be an HTTP(S) service origin")
    return endpoint


def validate_ollama_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    configured = urlsplit(ollama_endpoint())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid Ollama URL: {endpoint}")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Ollama URLs cannot contain credentials, queries, or fragments")
    if parsed.hostname not in LOCAL_LLM_HOSTS and endpoint.rstrip("/") != configured.geturl():
        raise ValueError("Refusing an Ollama endpoint not explicitly configured by the operator")
