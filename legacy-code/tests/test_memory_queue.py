from __future__ import annotations

import threading
import time
from pathlib import Path

from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.cli.repl import _memory_request_text, _record_task_memory
from shamsu.memory.queue import MemoryWriteQueue
from shamsu.memory.service import MemoryService
from shamsu.memory.types import GraphitiHealth
from shamsu.session.manager import SessionManager
from shamsu.session.memory import ConversationMemory


class BlockingAdapter:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.remembered: list[tuple[str, str, dict]] = []

    def healthcheck(self, workspace: Path) -> GraphitiHealth:
        return GraphitiHealth(True, message="ready")

    def get_relevant(self, workspace, text, task_type=None, limit=8):  # noqa: ANN001
        return []

    def remember(self, workspace, text, kind, metadata=None):  # noqa: ANN001
        self.entered.set()
        self.release.wait(5)
        self.remembered.append((kind, text, dict(metadata or {})))
        return {"ok": True}

    def forget(self, workspace, value):  # noqa: ANN001
        return {"ok": False, "forgot": value}


def _queue(tmp_path: Path, adapter: BlockingAdapter, maxsize: int = 4) -> MemoryWriteQueue:
    return MemoryWriteQueue(
        tmp_path,
        maxsize=maxsize,
        service_factory=lambda workspace: MemoryService(workspace, adapter=adapter),
    )


def test_enqueue_is_local_first_and_non_blocking(tmp_path: Path):
    adapter = BlockingAdapter()
    queue = _queue(tmp_path, adapter)

    started = time.monotonic()
    result = queue.enqueue(
        "Task outcome (success): updated parser",
        "task_summary",
        {"source_run_id": "run-1", "confidence": 0.9},
    )
    elapsed = time.monotonic() - started

    assert elapsed < 0.25
    assert result["local"] is True and result["queued"] is True
    assert MemoryService(tmp_path, adapter=adapter).fallback._all()[0].text.endswith("updated parser")
    assert adapter.entered.wait(1)
    assert queue.flush(0.03) is False
    adapter.release.set()
    assert queue.flush(1.0) is True


def test_queue_is_bounded_but_never_loses_local_copy(tmp_path: Path):
    adapter = BlockingAdapter()
    queue = _queue(tmp_path, adapter, maxsize=1)

    queue.enqueue("Task outcome (success): one", "task_summary", {"source_run_id": "r1"})
    assert adapter.entered.wait(1)
    second = queue.enqueue("Task outcome (success): two", "task_summary", {"source_run_id": "r2"})
    third = queue.enqueue("Task outcome (success): three", "task_summary", {"source_run_id": "r3"})

    assert second["queued"] is True
    assert third["queued"] is False
    assert third["reason"] == "mirror queue full"
    assert len(MemoryService(tmp_path, adapter=adapter).fallback._all()) == 3
    adapter.release.set()
    assert queue.flush(1.0) is True


def test_flush_honors_hard_deadline(tmp_path: Path):
    adapter = BlockingAdapter()
    queue = _queue(tmp_path, adapter)
    queue.enqueue("Task outcome (success): slow", "task_summary", {"source_run_id": "r1"})
    assert adapter.entered.wait(1)

    started = time.monotonic()
    assert queue.flush(0.04) is False
    assert time.monotonic() - started < 0.15
    adapter.release.set()


def test_tombstone_blocks_a_queued_mirror(tmp_path: Path):
    adapter = BlockingAdapter()
    queue = _queue(tmp_path, adapter)
    queue.enqueue("Task outcome (success): blocker", "task_summary", {"source_run_id": "r1"})
    assert adapter.entered.wait(1)
    queue.enqueue("Remember the obsolete deployment fact", "project_decision", {"source_run_id": "r2"})
    MemoryService(tmp_path, adapter=adapter).forget("obsolete deployment fact")
    adapter.release.set()
    assert queue.flush(1.0) is True

    assert all("obsolete deployment fact" not in text for _kind, text, _metadata in adapter.remembered)


def test_failed_and_denied_runs_do_not_create_positive_memory(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Outcomes")
    ledger = start_run(tmp_path, "fix parser", session_logger=logger)
    set_current_run(ledger)
    ledger.log_event("mutation_finished", status="applied")
    ledger.log_event("verification_failed")

    failed = _record_task_memory(
        tmp_path,
        "fix parser",
        "bug_lesson",
        logger,
        {"intent": "bug_fix"},
    )
    clear_current_run()

    assert failed["skipped"] is True
    assert MemoryService(tmp_path).fallback._all() == []


def test_verified_bug_lesson_has_source_and_confidence(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Verified")
    ledger = start_run(tmp_path, "fix parser", session_logger=logger)
    set_current_run(ledger)
    ledger.log_event("mutation_finished", status="applied")
    ledger.log_event("verification_passed")

    result = _record_task_memory(
        tmp_path,
        "fixed parser bounds check",
        "bug_lesson",
        logger,
        {"intent": "bug_fix"},
    )
    clear_current_run()

    assert result["long_term"]["ok"] is True
    memory = MemoryService(tmp_path).fallback._all()[0]
    assert memory.kind == "bug_lesson"
    assert memory.metadata["source_run_id"] == ledger.run_id
    assert memory.metadata["confidence"] == 0.95


def test_resume_context_uses_compact_state_without_debug_noise(tmp_path: Path):
    logger = SessionManager(tmp_path).create_session("Resume")
    logger.log("user.prompt", {"prompt": "fix the parser"}, "prompt")
    logger.log("llm.request", {"raw": "DEBUG_NOISE_MARKER"}, "debug")
    logger.log("assistant.message", {"message": "I updated parser.py"}, "answer")
    logger.set_pending_action({"type": "plan", "awaiting": "confirmation"})
    logger.set_last_failure("pytest -q", "one failed", 1)
    logger.update_summary_from_events()

    context = ConversationMemory.from_session(logger).build_context()

    assert "Compact session resume summary" in context
    assert "Pending action: plan" in context
    assert "pytest -q" in context
    assert "fix the parser" in context
    assert "DEBUG_NOISE_MARKER" not in context


def test_automatic_memory_strips_injected_agent_context():
    text = (
        "create probe.txt\n\nAdditional SHAMSU context:\n"
        "Workspace root: C:/private\nTop-level files: lots of debug detail"
    )

    assert _memory_request_text(text) == "create probe.txt"
