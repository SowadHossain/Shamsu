from __future__ import annotations

from pathlib import Path

from shamsu.tools import dev_server
from shamsu.tools.dev_server import DevServerManager, infer_dev_url, is_dev_server_command


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
