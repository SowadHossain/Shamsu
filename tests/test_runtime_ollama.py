from __future__ import annotations

import json

import pytest

from shamsu.llm.manager import LLMManager
from shamsu.runtime.models import SPECIALIST_MODELS, required_model_names
from shamsu.runtime.ollama import (
    RuntimeStatus,
    ensure_model_available,
    find_ollama_executable,
    list_installed_models,
    parse_ollama_list,
    pull_model,
    pull_model_streaming,
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
phi3:mini                                abc123          2.2 GB    1 hour ago
qwen2.5-coder:7b-instruct-q4_K_M         def456          4.7 GB    2 hours ago
"""

    assert parse_ollama_list(output) == [
        "phi3:mini",
        "qwen2.5-coder:7b-instruct-q4_K_M",
    ]


def test_ollama_list_uses_utf8_replacement_decoding(monkeypatch, tmp_path):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return type("Completed", (), {"returncode": 0, "stdout": "NAME ID SIZE\nphi3:mini latest 1 GB\n"})()

    monkeypatch.setattr("shamsu.runtime.ollama.subprocess.run", fake_run)

    assert list_installed_models(tmp_path / "ollama.exe") == ["phi3:mini"]
    kwargs = calls[0][1]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["env"]["PYTHONUTF8"] == "1"


def test_ollama_pull_uses_utf8_replacement_decoding(monkeypatch, tmp_path):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return type("Completed", (), {"returncode": 0, "stdout": "pulled ✓", "stderr": ""})()

    monkeypatch.setattr("shamsu.runtime.ollama.subprocess.run", fake_run)

    code, stdout, stderr = pull_model(tmp_path / "ollama.exe", "phi3:mini")

    assert code == 0
    assert stdout == "pulled ✓"
    assert stderr == ""
    kwargs = calls[0][1]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["env"]["PYTHONUTF8"] == "1"


def test_streaming_pull_reports_progress_chunks(monkeypatch, tmp_path):
    class FakeStdout:
        def __init__(self) -> None:
            self.chunks = iter(["a", "b", ""])

        def read(self, _size: int) -> str:
            return next(self.chunks)

    class FakeProcess:
        stdout = FakeStdout()

        def wait(self) -> int:
            return 0

    calls = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return FakeProcess()

    chunks = []
    monkeypatch.setattr("shamsu.runtime.ollama.subprocess.Popen", fake_popen)

    assert pull_model_streaming(tmp_path / "ollama.exe", "phi3:mini", chunks.append) == 0
    assert chunks == ["a", "b"]
    kwargs = calls[0][1]
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["env"]["PYTHONUTF8"] == "1"


def test_ensure_model_available_skips_pull_when_already_installed(monkeypatch, tmp_path):
    pull_calls = []
    monkeypatch.setattr(
        "shamsu.runtime.ollama.list_installed_models", lambda _path: ["qwen3:8b"]
    )
    monkeypatch.setattr(
        "shamsu.runtime.ollama.pull_model_streaming",
        lambda *args, **kwargs: pull_calls.append(args) or 0,
    )

    assert ensure_model_available(tmp_path / "ollama.exe", "qwen3:8b") is True
    assert pull_calls == []


def test_ensure_model_available_pulls_when_missing(monkeypatch, tmp_path):
    pull_calls = []

    def fake_list(_path):
        return ["qwen3:8b"] if pull_calls else []

    def fake_pull(_path, model_name, _progress_callback=None):
        pull_calls.append(model_name)
        return 0

    monkeypatch.setattr("shamsu.runtime.ollama.list_installed_models", fake_list)
    monkeypatch.setattr("shamsu.runtime.ollama.pull_model_streaming", fake_pull)

    assert ensure_model_available(tmp_path / "ollama.exe", "qwen3:8b") is True
    assert pull_calls == ["qwen3:8b"]


def test_ensure_model_available_returns_false_when_pull_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("shamsu.runtime.ollama.list_installed_models", lambda _path: [])
    monkeypatch.setattr(
        "shamsu.runtime.ollama.pull_model_streaming", lambda *args, **kwargs: 1
    )

    assert ensure_model_available(tmp_path / "ollama.exe", "qwen3:8b") is False


def test_ensure_model_available_forwards_progress_chunks(monkeypatch, tmp_path):
    chunks = []

    def fake_pull(_path, _model_name, progress_callback=None):
        if progress_callback:
            progress_callback("chunk")
        return 0

    monkeypatch.setattr("shamsu.runtime.ollama.list_installed_models", lambda _path: [])
    monkeypatch.setattr("shamsu.runtime.ollama.pull_model_streaming", fake_pull)

    ensure_model_available(tmp_path / "ollama.exe", "qwen3:8b", chunks.append)

    assert chunks == ["chunk"]


def test_no_window_flags_is_zero_off_windows(monkeypatch):
    from shamsu.runtime import ollama

    monkeypatch.setattr(ollama.sys, "platform", "linux")
    assert ollama._no_window_flags() == 0


def test_no_window_flags_hides_console_on_windows(monkeypatch):
    from shamsu.runtime import ollama

    monkeypatch.setattr(ollama.sys, "platform", "win32")
    # CREATE_NO_WINDOW is a Windows-only attribute; getattr keeps this portable
    # if the suite is ever run on a non-Windows host.
    expected = getattr(ollama.subprocess, "CREATE_NO_WINDOW", 0)
    if expected:
        assert ollama._no_window_flags() == expected


def test_ollama_list_command_hides_console_window(monkeypatch, tmp_path):
    from shamsu.runtime import ollama

    calls = []

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        return type("Completed", (), {"returncode": 0, "stdout": "NAME\nphi3:mini x\n"})()

    monkeypatch.setattr("shamsu.runtime.ollama.subprocess.run", fake_run)

    list_installed_models(tmp_path / "ollama.exe")

    assert calls[0]["creationflags"] == ollama._no_window_flags()


def test_streaming_pull_hides_console_window(monkeypatch, tmp_path):
    from shamsu.runtime import ollama

    class FakeStdout:
        def read(self, _size: int) -> str:
            return ""

    class FakeProcess:
        stdout = FakeStdout()

        def wait(self) -> int:
            return 0

    calls = []

    def fake_popen(*args, **kwargs):
        calls.append(kwargs)
        return FakeProcess()

    monkeypatch.setattr("shamsu.runtime.ollama.subprocess.Popen", fake_popen)

    pull_model_streaming(tmp_path / "ollama.exe", "phi3:mini")

    assert calls[0]["creationflags"] == ollama._no_window_flags()


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
