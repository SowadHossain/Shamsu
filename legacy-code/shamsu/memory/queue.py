"""Bounded, best-effort Graphiti mirroring for locally stored memories."""
from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from shamsu.memory.service import MemoryService

DEFAULT_QUEUE_SIZE = 64
DEFAULT_FLUSH_SECONDS = 1.5


@dataclass(frozen=True)
class MirrorJob:
    text: str
    kind: str
    metadata: dict[str, Any]
    key: tuple[str, str, str]
    observer: Callable[[str, dict[str, Any]], None] | None = None


class MemoryWriteQueue:
    """One daemon worker per workspace with bounded pending Graphiti writes."""

    def __init__(
        self,
        workspace: Path,
        *,
        maxsize: int = DEFAULT_QUEUE_SIZE,
        service_factory: Callable[[Path], MemoryService] = MemoryService,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.maxsize = max(1, int(maxsize))
        self._service_factory = service_factory
        self._jobs: queue.Queue[MirrorJob | None] = queue.Queue(maxsize=self.maxsize)
        self._pending_keys: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._accepting = True

    def enqueue(
        self,
        text: str,
        kind: str,
        metadata: dict[str, Any] | None = None,
        observer: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Store locally now and enqueue only the optional Graphiti mirror."""
        service = self._service_factory(self.workspace)
        local = service.remember_local(text, kind, metadata)
        if not local.get("ok"):
            return {**local, "local": False, "queued": False}

        clean_text = str(local.get("text") or text).strip()
        clean_kind = str(local.get("kind") or kind)
        clean_metadata = dict(local.get("metadata") or metadata or {})
        source_run_id = str(clean_metadata.get("source_run_id") or clean_metadata.get("run_id") or "")
        key = (clean_kind, _norm(clean_text), source_run_id)

        with self._lock:
            if key in self._pending_keys:
                return {**local, "local": True, "queued": False, "deduped": True}
            if not self._accepting:
                return {**local, "local": True, "queued": False, "reason": "queue stopped"}
            self._pending_keys.add(key)

        job = MirrorJob(clean_text, clean_kind, clean_metadata, key, observer)
        try:
            self._jobs.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._pending_keys.discard(key)
            service.log_queue_event("mirror_queue_full", {"kind": clean_kind, "source_run_id": source_run_id})
            return {**local, "local": True, "queued": False, "reason": "mirror queue full"}

        payload = {**local, "local": True, "queued": True}
        _notify(observer, "queued", payload)
        self._ensure_worker()
        return payload

    def flush(self, timeout: float = DEFAULT_FLUSH_SECONDS) -> bool:
        """Wait at most ``timeout`` seconds for currently queued jobs."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while self._jobs.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(0.01, remaining))
        return True

    def stop(self, timeout: float = DEFAULT_FLUSH_SECONDS) -> bool:
        flushed = self.flush(timeout)
        with self._lock:
            self._accepting = False
        try:
            self._jobs.put_nowait(None)
        except queue.Full:
            pass
        return flushed

    def status(self) -> dict[str, Any]:
        worker = self._worker
        return {
            "pending": self._jobs.unfinished_tasks,
            "capacity": self.maxsize,
            "worker_alive": bool(worker and worker.is_alive()),
            "accepting": self._accepting,
        }

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._worker = threading.Thread(
                target=self._run,
                name=f"shamsu-memory-{abs(hash(self.workspace)) % 10000}",
                daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                self._jobs.task_done()
                return
            try:
                result = self._service_factory(self.workspace).mirror_to_graphiti(
                    job.text, job.kind, job.metadata
                )
                _notify(job.observer, "mirrored", result)
            except Exception as exc:  # pragma: no cover - defensive worker boundary
                result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                try:
                    self._service_factory(self.workspace).log_queue_event(
                        "mirror_failed", {"kind": job.kind, "error": result["error"]}
                    )
                except Exception:
                    pass
                _notify(job.observer, "failed", result)
            finally:
                with self._lock:
                    self._pending_keys.discard(job.key)
                self._jobs.task_done()


_QUEUES: dict[Path, MemoryWriteQueue] = {}
_QUEUES_LOCK = threading.Lock()


def get_memory_queue(workspace: Path) -> MemoryWriteQueue:
    root = Path(workspace).resolve()
    with _QUEUES_LOCK:
        item = _QUEUES.get(root)
        if item is None:
            item = MemoryWriteQueue(root, maxsize=_env_int("SHAMSU_MEMORY_QUEUE_SIZE", DEFAULT_QUEUE_SIZE))
            _QUEUES[root] = item
        return item


def flush_memory_queues(timeout: float | None = None) -> bool:
    """Flush all queues within one shared hard deadline."""
    budget = _env_float("SHAMSU_MEMORY_FLUSH_SECONDS", DEFAULT_FLUSH_SECONDS) if timeout is None else max(0.0, timeout)
    deadline = time.monotonic() + budget
    with _QUEUES_LOCK:
        queues = list(_QUEUES.values())
    complete = True
    for item in queues:
        remaining = max(0.0, deadline - time.monotonic())
        complete = item.flush(remaining) and complete
    return complete


def reset_memory_queues(timeout: float = 0.1) -> None:
    """Test/support hook that stops and forgets all workspace queues."""
    with _QUEUES_LOCK:
        queues = list(_QUEUES.values())
        _QUEUES.clear()
    deadline = time.monotonic() + max(0.0, timeout)
    for item in queues:
        item.stop(max(0.0, deadline - time.monotonic()))


def _notify(observer: Callable[[str, dict[str, Any]], None] | None, event: str, payload: dict[str, Any]) -> None:
    if observer is None:
        return
    try:
        observer(event, payload)
    except Exception:
        pass


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.getenv(name, str(default))))
    except ValueError:
        return default
