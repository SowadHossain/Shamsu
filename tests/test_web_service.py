"""Standalone service behavior, without a terminal or a live model."""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from shamsu.control.runner import QueuedRunner
from shamsu.control.store import ControlStore
from shamsu.runtime.endpoint import ollama_endpoint, validate_ollama_endpoint
from shamsu.webui import api
from shamsu.webui.server import WebPortal
from shamsu.webui.sse import _read_from


def test_stream_waits_for_a_complete_appended_record(tmp_path):
    path = tmp_path / "activity.jsonl"
    path.write_text('{"kind":"turn.start"', encoding="utf-8")
    assert _read_from(path, 0) == ([], 0)
    with path.open("a", encoding="utf-8") as stream:
        stream.write('}\n')
    records, cursor = _read_from(path, 0)
    assert records == [{"kind": "turn.start"}]
    assert cursor == 1
    assert _read_from(path, cursor) == ([], cursor)


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def test_scheduler_drains_another_session_without_more_browser_requests(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    runner = QueuedRunner(store)
    entered = threading.Event()
    release = threading.Event()
    completed = []

    def run(workspace, session, item):
        if session == "first":
            entered.set()
            assert release.wait(3)
        completed.append(session)

    runner._run_one = run
    runner.start()
    try:
        runner.submit(tmp_path, "first", "work")
        assert entered.wait(2)
        second = runner.submit(tmp_path, "second", "work")
        assert second.queued
        release.set()
        wait_until(lambda: completed == ["first", "second"])
    finally:
        release.set()
        runner.stop()


def test_scheduler_picks_up_persisted_queue_on_start(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    store.enqueue(tmp_path, "saved-session", "work submitted before restart")
    runner = QueuedRunner(store)
    completed = []
    runner._run_one = lambda workspace, session, item: completed.append(item.text)
    runner.start()
    try:
        wait_until(lambda: bool(completed))
        assert completed == ["work submitted before restart"]
    finally:
        runner.stop()


def test_interrupted_work_is_reported_instead_of_replayed(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    queue_id = store.enqueue(tmp_path, "interrupted", "possibly applied changes")
    store.acquire_lease(tmp_path, "interrupted")
    store.claim_next(tmp_path, "interrupted")
    recovered = store.recover_interrupted(include_own_pid=True)
    assert [item.queue_id for item in recovered] == [queue_id]
    assert store.queued_sessions() == []
    with store._connect() as conn:
        assert conn.execute("SELECT status FROM queue WHERE queue_id = ?", (queue_id,)).fetchone()[0] == "cancelled"


def test_strict_machine_lease_cannot_be_reentered_by_another_runner(tmp_path):
    store = ControlStore(tmp_path / "control.db")
    assert store.acquire_lease(tmp_path, "machine", reentrant=False)
    assert not store.acquire_lease(tmp_path, "machine", reentrant=False)
    assert store.acquire_lease(tmp_path, "machine")


def test_host_header_is_checked_even_without_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    portal = WebPortal(port=0)
    portal.start()
    try:
        request = urllib.request.Request(portal.base_url + "/api/health", headers={"Host": "attacker.invalid"})
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=3)
        assert caught.value.code == 403
        with urllib.request.urlopen(portal.base_url + "/healthz", timeout=3) as response:
            assert json.load(response)["ok"]
    finally:
        portal.stop()


def test_container_origin_works_with_operator_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    portal = WebPortal(host="0.0.0.0", port=0, token="test-service-token")
    portal.start()
    try:
        origin = f"http://127.0.0.1:{portal.port}"
        request = urllib.request.Request(origin + "/api/health", headers={"Origin": origin, "X-Shamsu-Token": portal.token})
        with urllib.request.urlopen(request, timeout=3) as response:
            assert json.load(response)["ok"]
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(origin + "/api/health", timeout=3)
        assert caught.value.code == 401
    finally:
        portal.stop()


def test_project_creation_stays_inside_mounted_root(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SHAMSU_WORKSPACE_ROOT", str(root))
    project = api.add_workspace("my-app", create=True)
    assert Path(project["path"]) == root / "my-app"
    with pytest.raises(api.BadPath):
        api.add_workspace("../outside", create=True)
    assert not (tmp_path / "outside").exists()


def test_network_ollama_requires_explicit_operator_configuration(monkeypatch):
    monkeypatch.delenv("SHAMSU_OLLAMA_BASE_URL", raising=False)
    with pytest.raises(ValueError):
        validate_ollama_endpoint("http://unconfigured.invalid:11434")
    monkeypatch.setenv("SHAMSU_OLLAMA_BASE_URL", "http://ollama:11434")
    assert ollama_endpoint() == "http://ollama:11434"
    validate_ollama_endpoint("http://ollama:11434")
    with pytest.raises(ValueError):
        validate_ollama_endpoint("http://other-service:11434")


def test_web_module_starts_without_importing_repl(tmp_path):
    import subprocess
    import sys

    env = {**os.environ, "SHAMSU_HOME": str(tmp_path)}
    command = [sys.executable, "-c", "import sys; import shamsu.webui.__main__; assert 'shamsu.cli.repl' not in sys.modules"]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
