from __future__ import annotations

import os
from pathlib import Path

from shamsu.tools import dev_server
from shamsu.tools.dev_server import (
    DevServerManager,
    extract_dev_command_from_sentence,
    infer_dev_url,
    is_dev_server_command,
)


def test_npm_run_dev_is_detected_as_long_running():
    assert is_dev_server_command("npm run dev")


def test_workspace_npm_run_dev_is_detected_as_long_running():
    assert is_dev_server_command("npm --workspace client run dev")


def test_dev_url_is_inferred_for_common_frameworks():
    assert infer_dev_url("npm run dev") == "http://localhost:5173/"
    assert infer_dev_url("next dev") == "http://localhost:3000/"
    assert infer_dev_url("python manage.py runserver") == "http://127.0.0.1:8000/"
    assert infer_dev_url("flask run") == "http://127.0.0.1:5000/"


def test_dev_server_launches_detached_without_waiting(monkeypatch, tmp_path: Path):
    calls = []

    class FakeProcess:
        pid = 1234

    def fake_launch(command, cwd):
        calls.append((command, cwd))
        return FakeProcess()

    monkeypatch.setattr(dev_server, "_launch_detached", fake_launch)
    monkeypatch.setattr(dev_server, "_pid_alive", lambda pid: True)

    result = DevServerManager(tmp_path, approval_func=lambda _request: True).start("npm run dev")

    assert result.launched is True
    assert result.pid == 1234
    assert result.url == "http://localhost:5173/"
    assert calls == [("npm run dev", tmp_path.resolve())]


def test_python_dev_server_uses_existing_project_venv(monkeypatch, tmp_path: Path):
    calls = []
    interpreter = (
        tmp_path / ".venv" / "Scripts" / "python.exe"
        if os.name == "nt"
        else tmp_path / ".venv" / "bin" / "python"
    )
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("", encoding="utf-8")
    (tmp_path / "manage.py").write_text("", encoding="utf-8")

    class FakeProcess:
        pid = 1235

    monkeypatch.setattr(
        dev_server,
        "_launch_detached",
        lambda command, cwd: calls.append((command, cwd)) or FakeProcess(),
    )
    monkeypatch.setattr(dev_server, "_pid_alive", lambda pid: True)

    result = DevServerManager(tmp_path, approval_func=lambda _request: True).start(
        "python manage.py runserver"
    )

    assert result.launched is True
    assert str(interpreter) in result.command
    assert calls == [(result.command, tmp_path.resolve())]


def test_dev_server_denied_command_does_not_launch(monkeypatch, tmp_path: Path):
    launched = False

    def fake_launch(command, cwd):
        nonlocal launched
        launched = True

    monkeypatch.setattr(dev_server, "_launch_detached", fake_launch)

    result = DevServerManager(tmp_path, approval_func=lambda _request: False).start(
        "npm --workspace client run dev"
    )

    assert result.launched is False
    assert "denied" in result.message.lower()
    assert launched is False


# --- extract_dev_command_from_sentence ---

def test_extract_command_from_natural_language_sentence():
    assert extract_dev_command_from_sentence("can you run npm run dev in a new terminal") == "npm run dev"


def test_extract_workspace_command_from_sentence():
    assert extract_dev_command_from_sentence(
        "run npm --workspace client run dev in a new window"
    ) == "npm --workspace client run dev"


def test_extract_returns_none_when_no_dev_command():
    assert extract_dev_command_from_sentence("hello world") is None
    assert extract_dev_command_from_sentence("what files are here") is None


def test_extract_command_from_sentence_with_extra_words():
    assert extract_dev_command_from_sentence(
        "can you please start the dev server using npm run dev for me"
    ) == "npm run dev"


def test_bare_command_still_extracted():
    assert extract_dev_command_from_sentence("npm run dev") == "npm run dev"


def test_extract_handles_pnpm():
    assert extract_dev_command_from_sentence("please run pnpm dev in a terminal") == "pnpm dev"


def test_extract_handles_flask():
    assert extract_dev_command_from_sentence("start the server with flask run now") == "flask run"


# --- _looks_like_dev_server_failure and _handle_dev_server_recovery ---

def test_extract_dev_command_in_repl_strips_sentence(tmp_path: Path):
    """_extract_dev_command should extract 'npm run dev' from a full sentence."""
    from shamsu.cli.repl import _extract_dev_command
    assert _extract_dev_command(
        "can you run the code npm run dev in a new terminal window", tmp_path
    ) == "npm run dev"


def test_extract_dev_command_workspace_variant(tmp_path: Path):
    from shamsu.cli.repl import _extract_dev_command
    assert _extract_dev_command(
        "run npm --workspace client run dev please", tmp_path
    ) == "npm --workspace client run dev"


def test_looks_like_dev_server_failure_detects_didnt_run():
    from shamsu.cli.repl import _looks_like_dev_server_failure
    assert _looks_like_dev_server_failure("it didnt run btw")
    assert _looks_like_dev_server_failure("it didn't run btw")
    assert _looks_like_dev_server_failure("it did not run")


def test_looks_like_dev_server_failure_detects_didnt_start():
    from shamsu.cli.repl import _looks_like_dev_server_failure
    assert _looks_like_dev_server_failure("the dev server didn't start")
    assert _looks_like_dev_server_failure("failed to start")


def test_looks_like_dev_server_failure_does_not_match_unrelated():
    from shamsu.cli.repl import _looks_like_dev_server_failure
    assert not _looks_like_dev_server_failure("how do I fix the navbar?")
    assert not _looks_like_dev_server_failure("what files are here")
