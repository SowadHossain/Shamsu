from __future__ import annotations

import json
from pathlib import Path

from shamsu.taskmaster import adapter as adapter_module
from shamsu.taskmaster.adapter import TaskmasterAdapter, default_tool_dir


def test_default_tool_dir_is_the_shamsu_managed_external_tool_path():
    tool_dir = default_tool_dir()

    assert tool_dir == Path.home() / ".shamsu" / "tools" / "taskmaster"


class _FakeCompleted:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_node_and_script(tmp_path: Path, monkeypatch) -> None:
    node = tmp_path / "node"
    node.write_text("fake node", encoding="utf-8")
    script = tmp_path / "task-master.js"
    script.write_text("fake script", encoding="utf-8")
    monkeypatch.setenv("SHAMSU_TASKMASTER_NODE", str(node))
    monkeypatch.setenv("SHAMSU_TASKMASTER_CMD", str(script))


def test_healthcheck_reports_missing_node(monkeypatch, tmp_path):
    monkeypatch.delenv("SHAMSU_TASKMASTER_NODE", raising=False)
    monkeypatch.delenv("SHAMSU_TASKMASTER_CMD", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "does-not-exist")

    health = adapter.healthcheck(tmp_path)

    assert health.ok is False
    assert "Node.js" in health.message


def test_healthcheck_reports_missing_cli_when_node_present(monkeypatch, tmp_path):
    node = tmp_path / "node"
    node.write_text("fake", encoding="utf-8")
    monkeypatch.setenv("SHAMSU_TASKMASTER_NODE", str(node))
    monkeypatch.delenv("SHAMSU_TASKMASTER_CMD", raising=False)
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")

    health = adapter.healthcheck(tmp_path)

    assert health.ok is False
    assert "not installed" in health.message
    assert "/taskmaster setup" in health.message


def test_healthcheck_never_fakes_success_without_a_real_cli_call(monkeypatch, tmp_path):
    """Even once node+script both resolve, the version probe must actually
    run - a missing/broken script must not be reported healthy."""
    _fake_node_and_script(tmp_path, monkeypatch)

    def fake_run(cmd, **kwargs):
        return _FakeCompleted(returncode=1, stderr="not a real cli")

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")

    health = adapter.healthcheck(tmp_path)

    assert health.ok is False
    assert "not a real cli" in health.message


def test_healthcheck_rejects_cloud_provider_config(monkeypatch, tmp_path):
    _fake_node_and_script(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter_module.subprocess, "run", lambda cmd, **kw: _FakeCompleted(stdout="0.43.1"))
    config_dir = tmp_path / ".taskmaster"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"models": {"main": {"provider": "anthropic", "modelId": "claude-3-7-sonnet"}}}),
        encoding="utf-8",
    )
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")

    health = adapter.healthcheck(tmp_path)

    assert health.ok is False
    assert "anthropic" in health.message
    assert "Only the local Ollama provider" in health.message


def test_healthcheck_accepts_local_ollama_provider_config(monkeypatch, tmp_path):
    _fake_node_and_script(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter_module.subprocess, "run", lambda cmd, **kw: _FakeCompleted(stdout="0.43.1"))
    config_dir = tmp_path / ".taskmaster"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"models": {"main": {"provider": "ollama", "modelId": "qwen3:8b"}}}),
        encoding="utf-8",
    )
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")

    health = adapter.healthcheck(tmp_path)

    assert health.ok is True
    assert health.message == "Taskmaster is ready."


def test_list_tasks_parses_json_and_shapes_the_command(monkeypatch, tmp_path):
    _fake_node_and_script(tmp_path, monkeypatch)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        payload = {
            "tasks": [
                {"id": "1", "title": "Do the thing", "status": "pending", "priority": "high", "dependencies": []},
            ],
            "metadata": {"total": 1},
        }
        return _FakeCompleted(stdout=json.dumps(payload))

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")

    result = adapter.list_tasks(tmp_path)

    assert result["ok"] is True
    assert len(result["tasks"]) == 1
    assert result["tasks"][0].title == "Do the thing"
    assert "list" in captured["cmd"]
    assert "--json" in captured["cmd"]


def test_list_tasks_with_status_filter_shapes_the_command(monkeypatch, tmp_path):
    _fake_node_and_script(tmp_path, monkeypatch)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(stdout=json.dumps({"tasks": [], "metadata": {}}))

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")

    adapter.list_tasks(tmp_path, status="pending")

    assert "-s" in captured["cmd"]
    assert "pending" in captured["cmd"]


def test_show_task_returns_honest_error_when_not_found(monkeypatch, tmp_path):
    _fake_node_and_script(tmp_path, monkeypatch)
    monkeypatch.setattr(
        adapter_module.subprocess, "run",
        lambda cmd, **kw: _FakeCompleted(stdout=json.dumps({"found": False, "task": None})),
    )
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")

    result = adapter.show_task(tmp_path, "999")

    assert result["ok"] is False
    assert "999" in result["error"]


def test_set_status_shapes_the_command(monkeypatch, tmp_path):
    _fake_node_and_script(tmp_path, monkeypatch)
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(stdout="ok")

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")

    result = adapter.set_status(tmp_path, "3", "blocked")

    assert result["ok"] is True
    assert "set-status" in captured["cmd"]
    assert "--id=3" in captured["cmd"]
    assert "--status=blocked" in captured["cmd"]


def test_parse_prd_never_fakes_success_on_cli_failure(monkeypatch, tmp_path):
    _fake_node_and_script(tmp_path, monkeypatch)
    monkeypatch.setattr(
        adapter_module.subprocess, "run",
        lambda cmd, **kw: _FakeCompleted(returncode=1, stderr="model call failed"),
    )
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")
    prd = tmp_path / "prd.txt"
    prd.write_text("some prd", encoding="utf-8")

    result = adapter.parse_prd(tmp_path, prd)

    assert result["ok"] is False
    assert "model call failed" in result["error"]


def test_run_without_cli_available_does_not_fake_success(tmp_path):
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "does-not-exist")

    result = adapter.list_tasks(tmp_path)

    assert result["ok"] is False
    assert result["error"]


def test_status_shows_workspace_task_state(monkeypatch, tmp_path):
    _fake_node_and_script(tmp_path, monkeypatch)
    config_dir = tmp_path / ".taskmaster"
    config_dir.mkdir()
    (config_dir / "config.json").write_text(
        json.dumps({"models": {"main": {"provider": "ollama", "modelId": "qwen3:8b"}}}), encoding="utf-8",
    )

    def fake_run(cmd, **kwargs):
        if "--version" in cmd:
            return _FakeCompleted(stdout="0.43.1")
        payload = {
            "tasks": [
                {"id": "1", "title": "A", "status": "done", "dependencies": []},
                {"id": "2", "title": "B", "status": "pending", "dependencies": ["1"]},
            ],
        }
        return _FakeCompleted(stdout=json.dumps(payload))

    monkeypatch.setattr(adapter_module.subprocess, "run", fake_run)
    adapter = TaskmasterAdapter(tool_dir=tmp_path / "tools")

    status = adapter.status(tmp_path)

    assert status["available"] is True
    assert status["initialized"] is True
    assert status["task_count"] == 2
    assert status["status_counts"] == {"done": 1, "pending": 1}
