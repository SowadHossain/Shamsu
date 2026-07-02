from __future__ import annotations

import json

import pytest

from shamsu.llm.manager import LLMManager
from shamsu.runtime.models import SPECIALIST_MODELS, required_model_names
from shamsu.runtime.ollama import (
    RuntimeStatus,
    find_ollama_executable,
    parse_ollama_list,
    status_text,
    write_runtime_config,
)


def test_llm_manager_accepts_local_urls():
    assert LLMManager("http://localhost:11434").base_url == "http://localhost:11434"
    assert LLMManager("http://127.0.0.1:11434").base_url == "http://127.0.0.1:11434"
    assert LLMManager("http://[::1]:11434").base_url == "http://[::1]:11434"


def test_llm_manager_rejects_remote_urls():
    with pytest.raises(ValueError, match="local Ollama"):
        LLMManager("https://api.example.com")


def test_model_defaults_are_shared_by_runtime_and_llm_manager():
    required = required_model_names()

    assert SPECIALIST_MODELS["router"] in required
    assert SPECIALIST_MODELS["coder"] in required
    assert SPECIALIST_MODELS["bugfix"] in required
    assert SPECIALIST_MODELS["reviewer"] in required


def test_find_ollama_executable_uses_known_paths_when_path_lookup_misses(monkeypatch, tmp_path):
    exe = tmp_path / "ollama.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr("shamsu.runtime.ollama.shutil.which", lambda _name: None)

    assert find_ollama_executable(extra_paths=[exe]) == exe.resolve()


def test_parse_ollama_list_extracts_model_names():
    output = """NAME                                      ID              SIZE      MODIFIED
phi3:mini-4k-instruct                    abc123          2.2 GB    1 hour ago
qwen2.5-coder:7b-instruct-q4_K_M         def456          4.7 GB    2 hours ago
"""

    assert parse_ollama_list(output) == [
        "phi3:mini-4k-instruct",
        "qwen2.5-coder:7b-instruct-q4_K_M",
    ]


def test_status_text_is_friendly_for_missing_runtime():
    status = RuntimeStatus(missing_models=required_model_names())

    assert "Ollama not found" in status_text(status)
    assert "models repair" in status_text(status)


def test_write_runtime_config_stays_inside_repo_shamsu_dir(tmp_path):
    status = RuntimeStatus(
        ollama_path=str(tmp_path / "ollama"),
        server_running=True,
        installed_models=required_model_names(),
        missing_models=[],
    )

    config_path = write_runtime_config(tmp_path, status)
    data = json.loads(config_path.read_text(encoding="utf-8"))

    assert config_path == tmp_path / ".shamsu" / "runtime.json"
    assert data["local_only"] is True
    assert data["base_url"] == "http://localhost:11434"
