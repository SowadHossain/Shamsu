"""ActionLedger: the writer side of SHAMSU's local run/debug log.

Storage layout (see agent context/prompts/audit_log.md):

    <workspace>/.shamsu/runs/<run-id>/
        manifest.json
        events.jsonl
        decisions.jsonl
        tool-calls.jsonl
        model-calls.jsonl
        commands/cmd_NNN.stdout.log, cmd_NNN.stderr.log
        diagnostics/error_packet_NNN.json
        mutations/mutations.jsonl
        context-preview.json
        final-output.md
        summary.json

Every write path funnels through _append_jsonl / _write_json / _write_text,
which redact before touching disk - this is the single enforcement point for
"no secrets on disk", rather than relying on every call site to remember.

This module never reads from Graphiti, Codebase-Memory MCP, or the context
pipeline, and nothing here is ever fed back into a model prompt.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.action_ledger.config import DEFAULT_CONFIG, load_config
from shamsu.action_ledger.ids import new_run_id
from shamsu.action_ledger.redaction import redact_text, redact_value
from shamsu.safety.sandbox import Sandbox

PREVIEW_CHARS = 800
MAX_LIST_ITEMS = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


class ActionLedger:
    def __init__(
        self,
        workspace: Path,
        run_id: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.sandbox = Sandbox(self.workspace)
        self.run_id = run_id or new_run_id()
        self.run_dir = self.sandbox.validate(Path(".shamsu") / "runs" / self.run_id)
        self.config = config or load_config(self.workspace)
        self.enabled = bool(self.config.get("enabled", True))
        self._max_inline = int(self.config.get("max_inline_event_size", DEFAULT_CONFIG["max_inline_event_size"]))
        self._event_seq = self._count_lines(self.events_path)
        self._decision_seq = self._count_lines(self.decisions_path)
        self._tool_call_seq = self._count_lines(self.tool_calls_path)
        self._command_seq = self._count_glob(self.commands_dir, "cmd_*.stdout.log")
        self._diagnostics_seq = self._count_glob(self.diagnostics_dir, "error_packet_*.json")

    # -- paths ----------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    @property
    def events_path(self) -> Path:
        return self.run_dir / "events.jsonl"

    @property
    def decisions_path(self) -> Path:
        return self.run_dir / "decisions.jsonl"

    @property
    def tool_calls_path(self) -> Path:
        return self.run_dir / "tool-calls.jsonl"

    @property
    def model_calls_path(self) -> Path:
        return self.run_dir / "model-calls.jsonl"

    @property
    def commands_dir(self) -> Path:
        return self.run_dir / "commands"

    @property
    def diagnostics_dir(self) -> Path:
        return self.run_dir / "diagnostics"

    @property
    def mutations_dir(self) -> Path:
        return self.run_dir / "mutations"

    @property
    def context_preview_path(self) -> Path:
        return self.run_dir / "context-preview.json"

    @property
    def final_output_path(self) -> Path:
        return self.run_dir / "final-output.md"

    @property
    def summary_path(self) -> Path:
        return self.run_dir / "summary.json"

    # -- lifecycle --------------------------------------------------------------

    def start(self, prompt: str) -> None:
        if not self.enabled:
            return
        manifest = {
            "run_id": self.run_id,
            "workspace": str(self.workspace),
            "started_at": _now(),
            "finished_at": None,
            "status": "running",
            "prompt_preview": _preview(redact_text(prompt or "")),
        }
        self._write_json(self.manifest_path, manifest)
        self.log_event("run_started", prompt_preview=manifest["prompt_preview"])
        self.log_event("user_prompt_received", prompt_preview=manifest["prompt_preview"])

    def finish(self, final_output: str = "", status: str = "success") -> dict[str, Any]:
        if not self.enabled:
            return {}
        self._write_text(self.final_output_path, final_output or "")
        manifest = self._read_json(self.manifest_path) or {
            "run_id": self.run_id,
            "workspace": str(self.workspace),
            "started_at": _now(),
        }
        manifest["finished_at"] = _now()
        manifest["status"] = status
        self._write_json(self.manifest_path, manifest)
        self.log_event("run_finished" if status == "success" else "run_failed", status=status)
        self.log_event(
            "final_response_written",
            final_output_path="final-output.md",
            preview=_preview(final_output or ""),
        )
        summary = {
            "run_id": self.run_id,
            "status": status,
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "prompt_preview": manifest.get("prompt_preview", ""),
            "event_count": self._count_lines(self.events_path),
            "decision_count": self._count_lines(self.decisions_path),
            "tool_call_count": self._count_lines(self.tool_calls_path),
            "command_count": self._count_glob(self.commands_dir, "cmd_*.stdout.log"),
            "final_output_preview": _preview(final_output or ""),
        }
        self._write_json(self.summary_path, summary)
        return summary

    def fail(self, error: str) -> dict[str, Any]:
        return self.finish(final_output=error, status="failed")

    # -- generic event timeline ---------------------------------------------

    def log_event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        if not self.enabled:
            return {}
        event = {
            "event_id": self._next_id("_event_seq", "evt", 4),
            "run_id": self.run_id,
            "type": event_type,
            "timestamp": _now(),
        }
        event.update(self._compact(fields))
        self._append_jsonl(self.events_path, event)
        return event

    # -- decisions ------------------------------------------------------------

    def log_decision(
        self,
        decision: str,
        reason_summary: str = "",
        evidence: list[str] | None = None,
        alternatives_considered: list[str] | None = None,
        chosen_action: str = "",
        confidence: float | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        if not self.enabled:
            return {}
        record = {
            "decision_id": self._next_id("_decision_seq", "dec", 4),
            "run_id": self.run_id,
            "timestamp": _now(),
            "decision": decision,
            "reason_summary": reason_summary,
            "evidence": list(evidence or []),
            "alternatives_considered": list(alternatives_considered or []),
            "chosen_action": chosen_action,
            "confidence": confidence,
            "outcome": outcome,
        }
        self._append_jsonl(self.decisions_path, record)
        self.log_event("decision_recorded", decision_id=record["decision_id"], decision=decision, outcome=outcome)
        return record

    # -- tool calls ------------------------------------------------------------

    def log_tool_call(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if not self.enabled:
            return ""
        call_id = self._next_id("_tool_call_seq", "tool", 4)
        record = {
            "tool_call_id": call_id,
            "run_id": self.run_id,
            "timestamp": _now(),
            "tool": name,
            "phase": "called",
            "arguments": self._compact(arguments or {}),
        }
        self._append_jsonl(self.tool_calls_path, record)
        self.log_event("tool_called", tool_call_id=call_id, tool=name)
        return call_id

    def log_tool_result(
        self,
        call_id: str,
        name: str,
        ok: bool,
        message: str = "",
        data: Any = None,
    ) -> None:
        if not self.enabled:
            return
        record = {
            "tool_call_id": call_id,
            "run_id": self.run_id,
            "timestamp": _now(),
            "tool": name,
            "phase": "finished",
            "ok": bool(ok),
            "message": message,
            "data": self._compact(data if data is not None else {}),
        }
        self._append_jsonl(self.tool_calls_path, record)
        self.log_event("tool_finished", tool_call_id=call_id, tool=name, ok=bool(ok))

    # -- model calls ------------------------------------------------------------

    def log_model_call_started(self, role: str, model: str, prompt: str = "") -> None:
        if not self.enabled:
            return
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "timestamp": _now(),
            "role": role,
            "model": model,
            "phase": "started",
        }
        if self.config.get("log_model_prompts", False):
            record["prompt_preview"] = _preview(prompt or "")
        self._append_jsonl(self.model_calls_path, record)
        # Produces the catalog's "planner_model_called"/"coder_model_called"
        # for those roles, and an analogous name for every other specialist.
        self.log_event(f"{role}_model_called", role=role, model=model)

    def log_model_call_finished(
        self,
        role: str,
        model: str,
        response: str = "",
        duration_ms: float | None = None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        record: dict[str, Any] = {
            "run_id": self.run_id,
            "timestamp": _now(),
            "role": role,
            "model": model,
            "phase": "finished",
            "duration_ms": duration_ms,
            "meta": self._compact(meta or {}),
        }
        if self.config.get("log_model_responses", True):
            record["response_preview"] = _preview(response or "")
        self._append_jsonl(self.model_calls_path, record)
        self.log_event("model_response_received", role=role, model=model, duration_ms=duration_ms)

    # -- commands ------------------------------------------------------------

    def log_command_start(self, command: str, cwd: str | Path) -> str:
        if not self.enabled:
            return ""
        cmd_id = self._next_id("_command_seq", "cmd", 3)
        self.log_event("command_started", cmd_id=cmd_id, command=command, cwd=str(cwd))
        return cmd_id

    def log_command_finish(
        self,
        cmd_id: str,
        command: str,
        cwd: str | Path,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        diagnostics_path: str = "",
    ) -> dict[str, Any]:
        if not self.enabled or not cmd_id:
            return {}
        stdout_path = self.commands_dir / f"{cmd_id}.stdout.log"
        stderr_path = self.commands_dir / f"{cmd_id}.stderr.log"
        self._write_text(stdout_path, stdout or "")
        self._write_text(stderr_path, stderr or "")
        return self.log_event(
            "command_finished",
            cmd_id=cmd_id,
            command=command,
            cwd=str(cwd),
            exit_code=exit_code,
            stdout_path=str(stdout_path.relative_to(self.run_dir).as_posix()),
            stderr_path=str(stderr_path.relative_to(self.run_dir).as_posix()),
            diagnostics_path=diagnostics_path,
        )

    # -- diagnostics ------------------------------------------------------------

    def log_diagnostics(
        self,
        parser_chain: list[str],
        summary: str,
        packet: dict[str, Any] | None = None,
    ) -> str:
        if not self.enabled:
            return ""
        idx = self._diagnostics_seq
        self._diagnostics_seq += 1
        path = self.diagnostics_dir / f"error_packet_{idx:03d}.json"
        self._write_json(path, packet or {})
        relative = str(path.relative_to(self.run_dir).as_posix())
        self.log_event(
            "diagnostics_parsed",
            parser_chain=list(parser_chain or []),
            summary=summary,
            diagnostics_path=relative,
        )
        return relative

    # -- files / patches / mutations --------------------------------------------

    def log_file_read(self, path: str) -> None:
        self.log_event("file_read", path=path)

    def log_file_write_requested(self, path: str) -> None:
        self.log_event("file_write_requested", path=path)

    def log_patch_planned(self, files: list[str]) -> None:
        self.log_event("patch_planned", files=list(files))

    def log_patch_applied(self, files: list[str]) -> None:
        self.log_event("patch_applied", files=list(files))

    def log_mutation_started(self, transaction_id: str, reason: str = "") -> None:
        self.log_event("mutation_started", transaction_id=transaction_id, reason=reason)

    def log_mutation_finished(
        self,
        transaction_id: str,
        status: str,
        touched_files: list[str] | None = None,
        rollback_available: bool = False,
        error: str = "",
    ) -> None:
        if not self.enabled:
            return
        record = {
            "run_id": self.run_id,
            "timestamp": _now(),
            "transaction_id": transaction_id,
            "status": status,
            "touched_files": list(touched_files or []),
            "rollback_available": bool(rollback_available),
            "error": error,
        }
        self._append_jsonl(self.mutations_dir / "mutations.jsonl", record)
        event_type = "mutation_finished" if status in {"applied", "rolled_back"} else "mutation_failed"
        self.log_event(
            event_type,
            transaction_id=transaction_id,
            status=status,
            touched_files=list(touched_files or []),
            rollback_available=bool(rollback_available),
        )

    def log_rollback(self, transaction_id: str, ok: bool, message: str = "") -> None:
        self.log_event("rollback_performed", transaction_id=transaction_id, ok=bool(ok), message=message)

    # -- verification ------------------------------------------------------------

    def log_verification_started(self, command: str) -> None:
        self.log_event("verification_started", command=command)

    def log_verification_result(self, passed: bool, summary: str = "") -> None:
        self.log_event("verification_passed" if passed else "verification_failed", summary=summary)

    # -- context preview / memory / classification --------------------------

    def log_context_preview(self, preview: dict[str, Any]) -> None:
        if not self.enabled or not self.config.get("log_context_preview", True):
            return
        self._write_json(self.context_preview_path, preview)
        self.log_event(
            "context_pack_built",
            task_id=preview.get("task_id"),
            specialist=preview.get("specialist"),
            token_estimate=preview.get("token_estimate"),
            snippet_count=len(preview.get("snippets") or []),
        )

    def log_task_classified(self, task_type: str, **meta: Any) -> None:
        self.log_event("task_classified", task_type=task_type, **meta)

    def log_memory_status_checked(self, allowed: bool, reason: str = "") -> None:
        self.log_event("memory_status_checked", allowed=bool(allowed), reason=reason)

    def log_graphiti_retrieved(self, has_memory: bool, count: int = 0) -> None:
        self.log_event("graphiti_retrieved", has_memory=bool(has_memory), count=count)

    def log_code_memory_queried(self, query_type: str, query: str, result_count: int) -> None:
        self.log_event("code_memory_queried", query_type=query_type, query=_preview(query, 300), result_count=result_count)

    # -- internal: id sequencing --------------------------------------------

    def _next_id(self, attr: str, prefix: str, width: int) -> str:
        idx = getattr(self, attr)
        setattr(self, attr, idx + 1)
        return f"{prefix}_{idx:0{width}d}"

    # -- internal: storage helpers (single redaction/enforcement point) ----------

    def _compact(self, value: Any) -> Any:
        return _compact_value(value, self._max_inline)

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = redact_value(record) if self.config.get("redact_secrets", True) else record
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=True, default=str) + "\n")

    def _write_json(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = redact_value(data) if self.config.get("redact_secrets", True) else data
        path.write_text(json.dumps(safe, indent=2, default=str), encoding="utf-8")

    def _write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = redact_text(text) if self.config.get("redact_secrets", True) else text
        path.write_text(safe, encoding="utf-8")

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _count_lines(self, path: Path) -> int:
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())

    def _count_glob(self, directory: Path, pattern: str) -> int:
        if not directory.exists():
            return 0
        return len(list(directory.glob(pattern)))


def _compact_value(value: Any, limit: int) -> Any:
    if isinstance(value, str):
        return _truncate(value, limit)
    if isinstance(value, list):
        compacted = [_compact_value(item, max(limit // 4, 200)) for item in value[:MAX_LIST_ITEMS]]
        if len(value) > MAX_LIST_ITEMS:
            compacted.append(f"... [truncated {len(value) - MAX_LIST_ITEMS} item(s)]")
        return compacted
    if isinstance(value, dict):
        items = list(value.items())[:40]
        per_item = max(limit // max(len(items), 1), 200)
        compacted = {str(key): _compact_value(item, per_item) for key, item in items}
        if len(value) > len(items):
            compacted["..."] = f"truncated {len(value) - len(items)} key(s)"
        return compacted
    return value


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [truncated {len(text) - limit} chars]"


def start_run(workspace: Path, prompt: str, config: dict[str, Any] | None = None) -> ActionLedger:
    ledger = ActionLedger(workspace, config=config)
    ledger.start(prompt)
    return ledger
