from __future__ import annotations

import json
from pathlib import Path

from shamsu.abstract.service import AbstractService
from shamsu.action_ledger.ledger import start_run
from shamsu.cli import request_lifecycle
from shamsu.patch.engine import PatchEngine
from shamsu.tools.agent_tools import AgentToolRegistry
from tests.test_abstract_service import FakeCodebaseMemoryAdapter


def _last_index_payload(workspace: Path) -> dict:
    path = workspace / ".shamsu" / "abstract" / "last-index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def test_successful_write_file_marks_code_memory_stale(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    AbstractService(tmp_path, adapter=adapter).ensure_ready()  # seed a fresh index

    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    result = registry.write_file("app.py", "print('hi')\n")

    assert result.ok is True
    assert _last_index_payload(tmp_path).get("forced_stale") is True


def test_denied_write_file_does_not_mark_code_memory_stale(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    AbstractService(tmp_path, adapter=adapter).ensure_ready()

    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: False)
    result = registry.write_file("app.py", "print('hi')\n")

    assert result.ok is False
    assert _last_index_payload(tmp_path).get("forced_stale") is not True


def test_successful_patch_apply_queues_refresh(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    adapter = FakeCodebaseMemoryAdapter(available=True)
    AbstractService(tmp_path, adapter=adapter).ensure_ready()

    diff = (
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "-value = 1\n"
        "+value = 2\n"
    )
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    applied = engine.apply(diff, tmp_path)

    assert applied is True
    assert _last_index_payload(tmp_path).get("forced_stale") is True


def test_failed_patch_apply_does_not_queue_refresh(tmp_path):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    AbstractService(tmp_path, adapter=adapter).ensure_ready()

    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)
    applied = engine.apply("not a valid diff", tmp_path)

    assert applied is False
    assert _last_index_payload(tmp_path).get("forced_stale") is not True


def test_run_finalization_refreshes_code_memory_once_after_mutations(tmp_path, monkeypatch):
    adapter = FakeCodebaseMemoryAdapter(available=True)
    service = AbstractService(tmp_path, adapter=adapter)
    service.ensure_ready()
    ledger = start_run(tmp_path, "create app.py")
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda _request: True,
        action_ledger=ledger,
    )
    assert registry.write_file("app.py", "print('hi')\n").ok is True
    assert service.index_status().stale is True
    monkeypatch.setattr(
        request_lifecycle,
        "AbstractService",
        lambda _workspace: service,
    )

    request_lifecycle.finish_current_run(tmp_path, ledger)

    assert adapter.refresh_calls == 1
    assert service.index_status().stale is False
