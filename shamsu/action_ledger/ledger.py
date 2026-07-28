"""ActionLedger: the writer side of SHAMSU's local run/debug log.

Storage layout (see agent context/prompts/audit_log.md):

    <workspace>/.shamsu/runs/<run-id>/
        manifest.json
        narrative.md
        events.jsonl
        decisions.jsonl
        tool-calls.jsonl
        model-calls.jsonl
        prompts/model_NNNN.txt
        cot/model_NNNN.txt
        responses/model_NNNN.txt
        commands/cmd_NNN.stdout.log, cmd_NNN.stderr.log
        diagnostics/error_packet_NNN.json
        tool-results/tool_NNNN.json
        mutations/mutations.jsonl
        context-preview.json
        final-output.md
        summary.json

The JSONL rows stay small: full prompt / chain-of-thought / response text is
spilled to prompts/, cot/ and responses/ and referenced by relative path, the
same way commands/ and tool-results/ already work. Set `log_level` to
`compact` (or SHAMSU_LOG_LEVEL=compact) to keep previews only.

Every write path funnels through _append_jsonl / _write_json / _write_text,
which redact before touching disk - this is the single enforcement point for
"no secrets on disk", rather than relying on every call site to remember.

This module never reads from Graphiti, Codebase-Memory MCP, or the context
pipeline, and nothing here is ever fed back into a model prompt.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shamsu.action_ledger.config import DEFAULT_CONFIG, load_config, wants_full_artifacts
from shamsu.action_ledger.ids import new_run_id
from shamsu.action_ledger.redaction import redact_text, redact_value
from shamsu.context.budget import count_tokens
from shamsu.safety.sandbox import Sandbox

if TYPE_CHECKING:
    from shamsu.session.manager import SessionLogger

PREVIEW_CHARS = 800
MAX_LIST_ITEMS = 20
TOOL_RESULT_ARTIFACT_TOKEN_THRESHOLD = 1000
SCHEMA_VERSION = 2
TERMINAL_OUTCOMES = frozenset(
    {
        "success",
        "success_unverified",
        "partial",
        "failed",
        "denied",
        "needs_input",
        "cancelled",
        "timed_out",
        "dry_run",
    }
)


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
        session_id: str = "",
        turn_id: str = "",
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.sandbox = Sandbox(self.workspace)
        self.run_id = run_id or new_run_id()
        suffix = self.run_id.removeprefix("run_")
        self.session_id = session_id or f"session_{suffix}"
        self.turn_id = turn_id or f"turn_{suffix}"
        self.root_operation_id = "op_root"
        self.run_dir = self.sandbox.validate(Path(".shamsu") / "runs" / self.run_id)
        self.config = config or load_config(self.workspace)
        self.enabled = bool(self.config.get("enabled", True))
        self.full_artifacts = wants_full_artifacts(self.config)
        self._max_inline = int(self.config.get("max_inline_event_size", DEFAULT_CONFIG["max_inline_event_size"]))
        self._event_seq = self._count_lines(self.events_path)
        self._decision_seq = self._count_lines(self.decisions_path)
        self._tool_call_seq = self._count_records(self.tool_calls_path, "phase", "called")
        self._model_call_seq = self._count_records(self.model_calls_path, "phase", "started")
        self._context_seq = self._count_glob(self.contexts_dir, "context_*.json")
        self._command_seq = self._count_glob(self.commands_dir, "cmd_*.stdout.log")
        self._diagnostics_seq = self._count_glob(self.diagnostics_dir, "error_packet_*.json")
        self._sequence = self._max_sequence()
        self._tool_started: dict[str, float] = {}
        self._model_started: dict[str, float] = {}

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
    def tool_results_dir(self) -> Path:
        return self.run_dir / "tool-results"

    @property
    def prompts_dir(self) -> Path:
        return self.run_dir / "prompts"

    @property
    def cot_dir(self) -> Path:
        return self.run_dir / "cot"

    @property
    def responses_dir(self) -> Path:
        return self.run_dir / "responses"

    @property
    def narrative_path(self) -> Path:
        return self.run_dir / "narrative.md"

    @property
    def mutations_dir(self) -> Path:
        return self.run_dir / "mutations"

    @property
    def context_preview_path(self) -> Path:
        return self.run_dir / "context-preview.json"

    @property
    def contexts_dir(self) -> Path:
        return self.run_dir / "contexts"

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
        self._initialize_run_layout()
        manifest = {
            **self._correlation(self.root_operation_id),
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

    def _initialize_run_layout(self) -> None:
        """Create the canonical artifact set before any activity is recorded."""
        directories = [
            self.commands_dir,
            self.diagnostics_dir,
            self.tool_results_dir,
            self.mutations_dir,
            self.contexts_dir,
        ]
        if self.full_artifacts:
            directories.extend([self.prompts_dir, self.cot_dir, self.responses_dir])
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        for path in (
            self.events_path,
            self.decisions_path,
            self.tool_calls_path,
            self.model_calls_path,
            self.mutations_dir / "mutations.jsonl",
        ):
            if not path.exists():
                self._write_text(path, "")
        if not self.context_preview_path.exists():
            self._write_json(self.context_preview_path, {"schema_version": SCHEMA_VERSION, "contexts": []})

    def finish(self, final_output: str = "", status: str = "success") -> dict[str, Any]:
        if not self.enabled:
            return {}
        if status not in TERMINAL_OUTCOMES:
            raise ValueError(f"Unsupported run outcome: {status}")
        existing = self._read_json(self.manifest_path) or {}
        if existing.get("status") in TERMINAL_OUTCOMES:
            return self._read_json(self.summary_path) or {}
        self._close_open_records()
        if not self.final_output_path.exists() or self._current_final_output() != final_output:
            self.record_final_response(final_output)
        manifest = self._read_json(self.manifest_path) or {
            **self._correlation(self.root_operation_id),
            "run_id": self.run_id,
            "workspace": str(self.workspace),
            "started_at": _now(),
        }
        manifest["finished_at"] = _now()
        manifest["status"] = status
        self._write_json(self.manifest_path, manifest)
        terminal_event = "run_finished" if status in {"success", "success_unverified"} else "run_failed"
        self.log_event(terminal_event, status=status)
        events = self._read_jsonl(self.events_path)
        decisions = self._read_jsonl(self.decisions_path)
        tool_records = self._read_jsonl(self.tool_calls_path)
        mutations = self._read_jsonl(self.mutations_dir / "mutations.jsonl")
        finished_tools = [record for record in tool_records if record.get("phase") == "finished"]
        route = next(
            (
                str(record.get("chosen_action", ""))
                for record in reversed(decisions)
                if record.get("decision") == "dispatch_request"
            ),
            "",
        )
        summary = {
            **self._correlation(self.root_operation_id),
            "run_id": self.run_id,
            "status": status,
            "started_at": manifest.get("started_at"),
            "finished_at": manifest.get("finished_at"),
            "prompt_preview": manifest.get("prompt_preview", ""),
            "event_count": self._count_lines(self.events_path),
            "decision_count": self._count_lines(self.decisions_path),
            "tool_call_count": self._count_records(self.tool_calls_path, "phase", "called"),
            "model_call_count": self._count_records(self.model_calls_path, "phase", "started"),
            "context_count": self._count_glob(self.contexts_dir, "context_*.json"),
            "command_count": self._count_glob(self.commands_dir, "cmd_*.stdout.log"),
            "final_output_preview": _preview(final_output or ""),
            "route": route,
            "tools": [
                {
                    "tool": str(record.get("tool", "")),
                    "ok": bool(record.get("ok")),
                    "status": str(record.get("status", "")),
                    "duration_ms": record.get("duration_ms"),
                    "message": str(record.get("message", "")),
                }
                for record in finished_tools
            ],
            "changed_files": list(
                dict.fromkeys(
                    str(path)
                    for mutation in mutations
                    if mutation.get("status") == "applied"
                    for path in mutation.get("touched_files", [])
                )
            ),
            "transaction_ids": [
                str(record.get("transaction_id", ""))
                for record in mutations
                if record.get("transaction_id")
            ],
            "approvals": [
                {
                    "type": str(event.get("type", "")),
                    "action_type": str(
                        event.get("action_type")
                        or (event.get("request") or {}).get("action_type", "")
                    ),
                    "approved": (
                        bool(event.get("approved"))
                        if "approved" in event
                        else True
                        if event.get("type") == "approval_granted"
                        else False
                        if event.get("type") == "approval_denied"
                        else None
                    ),
                    "decision_scope": str(event.get("decision_scope", "")),
                    "decision_source": str(event.get("decision_source", "")),
                }
                for event in events
                if str(event.get("type", "")).startswith("approval_")
            ],
            "verification": [
                {
                    "type": str(event.get("type", "")),
                    "command": str(event.get("command", "")),
                    "verifier_id": str(event.get("verifier_id", "")),
                    "source": str(event.get("source", "")),
                    "required": bool(event.get("required", False)),
                    "exit_code": event.get("exit_code"),
                }
                for event in events
                if event.get("type")
                in {
                    "verification_started",
                    "verification_passed",
                    "verification_failed",
                    "verification_unavailable",
                }
            ],
            "artifacts": {
                "events": "events.jsonl",
                "decisions": "decisions.jsonl",
                "tool_calls": "tool-calls.jsonl",
                "tool_results": "tool-results/",
                "model_calls": "model-calls.jsonl",
                "prompts": "prompts/",
                "cot": "cot/",
                "responses": "responses/",
                "contexts": "contexts/",
                "mutations": "mutations/mutations.jsonl",
                "narrative": "narrative.md",
                "final_output": "final-output.md",
            },
        }
        self._write_json(self.summary_path, summary)
        return summary

    def fail(self, error: str) -> dict[str, Any]:
        return self.finish(final_output=error, status="failed")

    def record_final_response(self, final_output: str) -> None:
        if not self.enabled:
            return
        self._write_text(self.final_output_path, final_output or "")
        self.log_event(
            "final_response_written",
            final_output_path="final-output.md",
            preview=_preview(final_output or ""),
        )
        summary = self._read_json(self.summary_path)
        if summary is not None:
            summary["final_output_preview"] = _preview(final_output or "")
            summary["event_count"] = self._count_lines(self.events_path)
            self._write_json(self.summary_path, summary)

    def _current_final_output(self) -> str:
        try:
            return self.final_output_path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def finalize_from_evidence(self) -> dict[str, Any]:
        manifest = self._read_json(self.manifest_path) or {}
        if manifest.get("status") in TERMINAL_OUTCOMES:
            return self._read_json(self.summary_path) or {}
        outcome = self.evidence_outcome()
        final_output = self._current_final_output()
        return self.finish(final_output, status=outcome)

    def evidence_outcome(self) -> str:
        """Infer the current outcome without closing the run."""
        manifest = self._read_json(self.manifest_path) or {}
        if manifest.get("status") in TERMINAL_OUTCOMES:
            return str(manifest["status"])
        events = self._read_jsonl(self.events_path)
        event_types = [str(event.get("type", "")) for event in events]
        mutations = self._read_jsonl(self.mutations_dir / "mutations.jsonl")
        mutation_statuses = [str(item.get("status", "")) for item in mutations]
        mutation_seen = bool(mutations) or "patch_apply_succeeded" in event_types
        verification_passed = "verification_passed" in event_types
        failure_event_seen = any(
            event_type in event_types
            for event_type in {
                "patch_validation_failed",
                "mutation_required_but_missing",
                "contract_failed",
                "agent_stopped",
            }
        )
        bad_mutation_seen = any(
            status not in {"applied", "rolled_back"} for status in mutation_statuses
        )
        if "run_cancelled" in event_types:
            outcome = "cancelled"
        elif "run_timed_out" in event_types:
            outcome = "timed_out"
        elif "run_needs_input" in event_types:
            outcome = "needs_input"
        elif "approval_denied" in event_types:
            outcome = "denied"
        elif "composite_failed" in event_types:
            outcome = "failed"
        elif "composite_partial" in event_types:
            outcome = "partial"
        elif (
            self._has_unrecovered_verification_failure(events)
            or self._has_unrecovered_command_failure(events)
            or failure_event_seen
            or bad_mutation_seen
            or self._has_unrecovered_tool_failure()
            or self._has_unrecovered_patch_failure(events)
        ):
            outcome = "failed"
        elif mutation_seen and verification_passed:
            outcome = "success"
        elif mutation_seen:
            outcome = "success_unverified"
        elif self._has_successful_command(events):
            outcome = "success"
        else:
            outcome = "success"
        return outcome

    def _has_unrecovered_command_failure(self, events: list[dict[str, Any]]) -> bool:
        latest_exit_code: int | None = None
        for event in events:
            if event.get("type") != "command_finished":
                continue
            try:
                latest_exit_code = int(event.get("exit_code", 1))
            except (TypeError, ValueError):
                latest_exit_code = 1
        return latest_exit_code is not None and latest_exit_code != 0

    def _has_successful_command(self, events: list[dict[str, Any]]) -> bool:
        for event in events:
            if event.get("type") != "command_finished":
                continue
            try:
                if int(event.get("exit_code", 1)) == 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def _has_unrecovered_tool_failure(self) -> bool:
        """Detect failures that were not superseded by later successful work."""
        records = self._read_jsonl(self.tool_calls_path)
        arguments = {
            str(record.get("tool_call_id", "")): record.get("arguments", {})
            for record in records
            if record.get("phase") == "called"
        }
        latest: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, record in enumerate(records):
            if record.get("phase") != "finished":
                continue
            latest[str(record.get("tool", "unknown"))] = (index, record)
        for tool, (failure_index, record) in latest.items():
            if bool(record.get("ok")):
                continue
            if tool not in {"read_file", "edit_file", "write_file"}:
                return True
            failed_args = arguments.get(str(record.get("tool_call_id", "")), {})
            failed_path = str(failed_args.get("filepath") or "").replace("\\", "/")
            recovered = False
            for later in records[failure_index + 1 :]:
                if later.get("phase") != "finished" or not bool(later.get("ok")):
                    continue
                if later.get("tool") not in {"edit_file", "write_file"}:
                    continue
                later_args = arguments.get(str(later.get("tool_call_id", "")), {})
                later_path = str(later_args.get("filepath") or "").replace("\\", "/")
                if failed_path and later_path == failed_path:
                    recovered = True
                    break
            if not recovered:
                return True
        return False

    @staticmethod
    def _has_unrecovered_verification_failure(events: list[dict[str, Any]]) -> bool:
        """A later verdict for the same command supersedes an earlier failure."""
        latest: dict[str, bool] = {}
        for event in events:
            event_type = str(event.get("type", ""))
            if event_type not in {"verification_failed", "verification_passed"}:
                continue
            key = str(event.get("verifier_id") or event.get("command") or "__general__")
            latest[key] = event_type == "verification_passed"
        return any(not passed for passed in latest.values())

    @staticmethod
    def _has_unrecovered_patch_failure(events: list[dict[str, Any]]) -> bool:
        """A bugfix retry that fixes an invalid first diff must not fail the
        whole run: only a file whose LAST patch attempt in this run failed
        counts. Without this, one bad diff followed by a successful rewrite of
        the same file - the normal bugfix retry path - still reported the run
        as failed even though the file was correctly fixed and verified."""
        latest: dict[str, bool] = {}
        for event in events:
            event_type = str(event.get("type", ""))
            if event_type not in {"patch_apply_failed", "patch_apply_succeeded"}:
                continue
            succeeded = event_type == "patch_apply_succeeded"
            for file in event.get("files") or ["__unknown__"]:
                latest[str(file)] = succeeded
        return any(not succeeded for succeeded in latest.values())

    # -- generic event timeline ---------------------------------------------

    def log_event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        if not self.enabled:
            return {}
        event_id = self._next_id("_event_seq", "evt", 4)
        event = {
            **self._correlation(self.root_operation_id),
            "event_id": event_id,
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
        goal: str = "",
        observation: str = "",
        expected_postcondition: str = "",
        actual_outcome: str = "",
    ) -> dict[str, Any]:
        if not self.enabled:
            return {}
        decision_id = self._next_id("_decision_seq", "dec", 4)
        record = {
            **self._correlation(decision_id, self.root_operation_id),
            "decision_id": decision_id,
            "run_id": self.run_id,
            "timestamp": _now(),
            "decision": decision,
            "reason_summary": reason_summary,
            "evidence": list(evidence or []),
            "alternatives_considered": list(alternatives_considered or []),
            "chosen_action": chosen_action,
            "confidence": confidence,
            "outcome": outcome,
            "goal": goal,
            "observation": observation,
            "expected_postcondition": expected_postcondition,
            "actual_outcome": actual_outcome,
        }
        self._append_jsonl(self.decisions_path, record)
        self.log_event("decision_recorded", decision_id=record["decision_id"], decision=decision, outcome=outcome)
        return record

    # -- tool calls ------------------------------------------------------------

    def log_tool_call(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if not self.enabled:
            return ""
        call_id = self._next_id("_tool_call_seq", "tool", 4)
        self._tool_started[call_id] = time.perf_counter()
        record = {
            **self._correlation(call_id, self.root_operation_id),
            "tool_call_id": call_id,
            "run_id": self.run_id,
            "timestamp": _now(),
            "tool": name,
            "phase": "called",
            "status": "running",
            "tool_version": 1,
            "arguments": self._compact(arguments or {}),
        }
        self._append_jsonl(self.tool_calls_path, record)
        self.log_event("tool_called", tool_call_id=call_id, tool=name)
        self._narrative("append_tool_call", name, arguments or {})
        return call_id

    def log_tool_result(
        self,
        call_id: str,
        name: str,
        ok: bool,
        message: str = "",
        data: Any = None,
        status: str = "",
        exception_class: str = "",
        traceback_path: str = "",
        original_tokens: int | None = None,
        returned_tokens: int | None = None,
        max_tokens: int | None = None,
        truncated: bool | None = None,
        artifact_path: str = "",
        full_result_text: str = "",
    ) -> None:
        if not self.enabled:
            return
        result_text = full_result_text or _tool_result_text(ok, message, data)
        original_tokens = (
            int(original_tokens)
            if original_tokens is not None
            else count_tokens(result_text)
        )
        returned_tokens = (
            int(returned_tokens)
            if returned_tokens is not None
            else original_tokens
        )
        truncated = bool(truncated) if truncated is not None else returned_tokens < original_tokens
        artifact_path = artifact_path or self._write_tool_result_artifact(
            call_id,
            result_text,
            truncated or original_tokens > TOOL_RESULT_ARTIFACT_TOKEN_THRESHOLD,
        )
        record = {
            **self._correlation(call_id, self.root_operation_id),
            "tool_call_id": call_id,
            "run_id": self.run_id,
            "timestamp": _now(),
            "tool": name,
            "phase": "finished",
            "ok": bool(ok),
            "status": status or ("success" if ok else "failed"),
            "duration_ms": self._elapsed_ms(self._tool_started.pop(call_id, None)),
            "message": message,
            "data": self._compact(data if data is not None else {}),
            "exception_class": exception_class,
            "traceback_path": traceback_path,
            "diagnostics_path": (
                str(data.get("diagnostics_path", "")) if isinstance(data, dict) else ""
            ),
            "original_tokens": original_tokens,
            "returned_tokens": returned_tokens,
            "max_tokens": max_tokens,
            "truncated": bool(truncated) if truncated is not None else False,
            "artifact_path": artifact_path,
        }
        self._append_jsonl(self.tool_calls_path, record)
        self._narrative("append_tool_result", name, bool(ok), message)
        self.log_event(
            "tool_finished",
            tool_call_id=call_id,
            tool=name,
            ok=bool(ok),
            original_tokens=original_tokens,
            returned_tokens=returned_tokens,
            truncated=bool(truncated) if truncated is not None else False,
            artifact_path=artifact_path,
        )

    # -- model calls ------------------------------------------------------------

    def log_model_call_started(
        self,
        role: str,
        model: str,
        prompt: str = "",
        *,
        system: str = "",
        messages: list[dict[str, Any]] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        """Record a model call, capturing the request exactly as sent.

        `system`/`messages`/`tools` are what actually goes over the wire on the
        chat path; passing them makes the stored prompt the real thing rather
        than a reconstruction. `prompt` alone still works for the completion
        path. The full text lands in prompts/<call_id>.txt; the JSONL row keeps
        the hash, a preview, and the path."""
        if not self.enabled:
            return ""
        call_id = self._next_id("_model_call_seq", "model", 4)
        self._model_started[call_id] = time.perf_counter()
        request_text = _format_request(prompt, system, messages, tools)
        record: dict[str, Any] = {
            **self._correlation(call_id, self.root_operation_id),
            "model_call_id": call_id,
            "run_id": self.run_id,
            "timestamp": _now(),
            "role": role,
            "model": model,
            "phase": "started",
            "prompt_sha256": hashlib.sha256(request_text.encode("utf-8")).hexdigest(),
            "prompt_chars": len(request_text),
            "message_count": len(messages or []),
            "tool_count": len(tools or []),
        }
        if self.full_artifacts:
            record["prompt_path"] = self._write_artifact(self.prompts_dir, call_id, request_text)
        if self.config.get("log_model_prompts", True):
            record["prompt_preview"] = _preview(request_text)
        self._append_jsonl(self.model_calls_path, record)
        # Produces the catalog's "planner_model_called"/"coder_model_called"
        # for those roles, and an analogous name for every other specialist.
        self.log_event(f"{role}_model_called", role=role, model=model)
        return call_id

    def log_model_call_finished(
        self,
        role: str,
        model: str,
        response: str = "",
        duration_ms: float | None = None,
        meta: dict[str, Any] | None = None,
        call_id: str = "",
        error: str = "",
        traceback_text: str = "",
    ) -> None:
        if not self.enabled:
            return
        call_id = call_id or self._latest_open_model_call(role, model)
        measured_duration = self._elapsed_ms(self._model_started.pop(call_id, None))
        traceback_path = ""
        if error:
            traceback_path = self.record_exception(
                "model",
                role,
                traceback_text or error,
                operation_id=call_id,
            )
        exception_class, _, exception_message = error.partition(":")
        record: dict[str, Any] = {
            **self._correlation(call_id or self.root_operation_id, self.root_operation_id),
            "model_call_id": call_id,
            "run_id": self.run_id,
            "timestamp": _now(),
            "role": role,
            "model": model,
            "phase": "failed" if error else "finished",
            "duration_ms": duration_ms if duration_ms is not None else measured_duration,
            "error": error,
            "exception_class": exception_class.strip() if error else "",
            "exception_message": exception_message.strip() if error else "",
            "traceback_path": traceback_path,
            "meta": self._compact(meta or {}),
        }
        if self.full_artifacts and response:
            record["response_path"] = self._write_artifact(self.responses_dir, call_id, response)
        record["response_chars"] = len(response or "")
        if self.config.get("log_model_responses", True):
            record["response_preview"] = _preview(response or "")
        self._append_jsonl(self.model_calls_path, record)
        self.log_event(
            "model_call_failed" if error else "model_response_received",
            model_call_id=call_id,
            role=role,
            model=model,
            duration_ms=record["duration_ms"],
            error=error,
        )

    def log_model_thinking(
        self,
        call_id: str,
        role: str,
        model: str,
        thinking: str,
    ) -> str:
        """Capture a reasoning model's full chain-of-thought.

        Written untruncated to cot/<call_id>.txt and referenced by path - the
        previous 4000-char clip meant a long reasoning trace was never
        recoverable, which is exactly when you need it. Returns the relative
        path, or "" when there was no reasoning or artifacts are off.

        This is debug evidence only: like everything else under runs/, it is
        never read back into a model prompt (see the module docstring)."""
        if not self.enabled:
            return ""
        thinking = (thinking or "").strip()
        if not thinking:
            return ""
        cot_path = ""
        if self.full_artifacts:
            cot_path = self._write_artifact(self.cot_dir, call_id or "model_unknown", thinking)
        record = {
            **self._correlation(call_id or self.root_operation_id, self.root_operation_id),
            "model_call_id": call_id,
            "run_id": self.run_id,
            "timestamp": _now(),
            "role": role,
            "model": model,
            "phase": "thinking",
            "thinking_chars": len(thinking),
            "cot_path": cot_path,
        }
        self._append_jsonl(self.model_calls_path, record)
        self.log_event(
            "model_thinking_captured",
            model_call_id=call_id,
            role=role,
            model=model,
            thinking_chars=len(thinking),
            cot_path=cot_path,
        )
        return cot_path

    # -- commands ------------------------------------------------------------

    def log_command_start(self, command: str, cwd: str | Path) -> str:
        if not self.enabled:
            return ""
        cmd_id = self._next_id("_command_seq", "cmd", 3)
        self.log_event("command_started", operation_id=cmd_id, cmd_id=cmd_id, command=command, cwd=str(cwd))
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
        operation_id: str = "",
    ) -> str:
        if not self.enabled:
            return ""
        idx = self._diagnostics_seq
        self._diagnostics_seq += 1
        kind = "error_packet" if bool((packet or {}).get("actionable", False)) else "diagnostic_packet"
        path = self.diagnostics_dir / f"{kind}_{idx:03d}.json"
        self._write_json(path, packet or {})
        relative = str(path.relative_to(self.run_dir).as_posix())
        self.log_event(
            "diagnostics_parsed",
            operation_id=operation_id or self.root_operation_id,
            parser_chain=list(parser_chain or []),
            summary=summary,
            diagnostics_path=relative,
            classification=str((packet or {}).get("classification", "")),
            actionable=bool((packet or {}).get("actionable", False)),
            affected_files=list((packet or {}).get("target_files", [])),
            suggested_next_check=str((packet or {}).get("suggested_next_check", "")),
        )
        return relative

    def record_exception(
        self,
        phase: str,
        operation: str,
        traceback_text: str,
        operation_id: str = "",
    ) -> str:
        if not self.enabled:
            return ""
        idx = self._diagnostics_seq
        self._diagnostics_seq += 1
        safe_phase = "".join(char for char in phase.lower() if char.isalnum() or char in {"-", "_"})
        path = self.diagnostics_dir / f"exception_{idx:03d}_{safe_phase or 'runtime'}.traceback.log"
        self._write_text(path, traceback_text or "No traceback text was captured.")
        relative = str(path.relative_to(self.run_dir).as_posix())
        self.log_event(
            "exception_recorded",
            operation_id=operation_id or self.root_operation_id,
            phase=phase,
            operation=operation,
            traceback_path=relative,
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
        operations: list[dict[str, Any]] | None = None,
        before_hashes: dict[str, str | None] | None = None,
        after_hashes: dict[str, str | None] | None = None,
        backups: dict[str, str] | None = None,
        patch_path: str = "",
        verification: dict[str, Any] | None = None,
        abstract_index_state: str = "stale",
    ) -> None:
        if not self.enabled:
            return
        record = {
            **self._correlation(transaction_id, self.root_operation_id),
            "run_id": self.run_id,
            "timestamp": _now(),
            "transaction_id": transaction_id,
            "status": status,
            "touched_files": list(touched_files or []),
            "rollback_available": bool(rollback_available),
            "error": error,
            "operations": list(operations or []),
            "before_hashes": dict(before_hashes or {}),
            "after_hashes": dict(after_hashes or {}),
            "backups": dict(backups or {}),
            "patch_path": patch_path,
            "verification": verification,
            "abstract_index_state": abstract_index_state,
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

    def verifier_id_for(self, command: str, source: str = "") -> str:
        command = (command or "").strip()
        source = (source or "").strip()
        if not command and not source:
            return ""
        digest = hashlib.sha256(f"{source}\0{command}".encode("utf-8")).hexdigest()
        return f"verifier_{digest[:12]}"

    def log_verification_started(
        self,
        command: str,
        *,
        verifier_id: str = "",
        source: str = "",
        transaction_id: str = "",
        required: bool = False,
        files: list[str] | None = None,
        **fields: Any,
    ) -> None:
        verifier_id = verifier_id or self.verifier_id_for(command, source)
        self.log_event(
            "verification_started",
            command=command,
            verifier_id=verifier_id,
            source=source,
            transaction_id=transaction_id,
            required=bool(required),
            files=list(files or []),
            **fields,
        )

    def log_verification_result(
        self,
        passed: bool,
        summary: str = "",
        *,
        command: str = "",
        verifier_id: str = "",
        source: str = "",
        transaction_id: str = "",
        required: bool = False,
        files: list[str] | None = None,
        exit_code: int | None = None,
        **fields: Any,
    ) -> None:
        verifier_id = verifier_id or self.verifier_id_for(command, source)
        self.log_event(
            "verification_passed" if passed else "verification_failed",
            command=command,
            verifier_id=verifier_id,
            source=source,
            transaction_id=transaction_id,
            required=bool(required),
            files=list(files or []),
            exit_code=exit_code,
            summary=summary,
            **fields,
        )

    def log_verification_unavailable(
        self,
        summary: str = "",
        *,
        command: str = "",
        verifier_id: str = "",
        source: str = "",
        transaction_id: str = "",
        required: bool = False,
        files: list[str] | None = None,
        **fields: Any,
    ) -> None:
        verifier_id = verifier_id or self.verifier_id_for(command, source)
        self.log_event(
            "verification_unavailable",
            command=command,
            verifier_id=verifier_id,
            source=source,
            transaction_id=transaction_id,
            required=bool(required),
            files=list(files or []),
            summary=summary,
            **fields,
        )

    def log_repair_attempt(
        self,
        *,
        attempt_index: int,
        outcome: str,
        kept: bool,
        files_changed: list[str] | None = None,
        before_signature: str = "",
        after_signature: str = "",
        note: str = "",
        verifier_id: str = "",
        command: str = "",
    ) -> None:
        self.log_event(
            "repair_attempt_finished",
            attempt_index=attempt_index,
            outcome=outcome,
            kept=bool(kept),
            files_changed=list(files_changed or []),
            before_signature=before_signature,
            after_signature=after_signature,
            note=note,
            verifier_id=verifier_id,
            command=command,
        )

    # -- context preview / memory / classification --------------------------

    def log_context_preview(self, preview: dict[str, Any], model_call_id: str = "") -> None:
        if not self.enabled or not self.config.get("log_context_preview", True):
            return
        context_id = self._next_id("_context_seq", "context", 4)
        record = {
            **self._correlation(context_id, model_call_id or self.root_operation_id),
            "context_id": context_id,
            "model_call_id": model_call_id,
            **preview,
        }
        context_path = self.contexts_dir / f"{context_id}.json"
        self._write_json(context_path, record)
        # Backward-compatible latest pointer for old readers and exports.
        self._write_json(self.context_preview_path, record)
        self.log_event(
            "context_pack_built",
            context_id=context_id,
            model_call_id=model_call_id,
            context_path=str(context_path.relative_to(self.run_dir).as_posix()),
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

    def log_feedback_added(self, text: str) -> None:
        self.log_event("feedback_added", feedback_preview=_preview(text, 500))

    def log_cancel_requested(self) -> None:
        self.log_event("run_cancel_requested")

    def log_run_cancelled(self) -> None:
        self.log_event("run_cancelled")

    # -- internal: id sequencing --------------------------------------------

    def _next_id(self, attr: str, prefix: str, width: int) -> str:
        idx = getattr(self, attr)
        setattr(self, attr, idx + 1)
        return f"{prefix}_{idx:0{width}d}"

    def _correlation(self, operation_id: str, parent_operation_id: str = "") -> dict[str, Any]:
        self._sequence += 1
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "operation_id": operation_id,
            "parent_operation_id": parent_operation_id,
            "timestamp": _now(),
            "sequence": self._sequence,
        }

    @staticmethod
    def _elapsed_ms(started: float | None) -> float | None:
        if started is None:
            return None
        return round((time.perf_counter() - started) * 1000, 2)

    def _latest_open_model_call(self, role: str, model: str) -> str:
        records = self._read_jsonl(self.model_calls_path)
        closed = {
            str(record.get("model_call_id", ""))
            for record in records
            if record.get("phase") in {"finished", "failed"}
        }
        for record in reversed(records):
            call_id = str(record.get("model_call_id", ""))
            if record.get("phase") == "started" and call_id not in closed:
                if record.get("role") == role and record.get("model") == model:
                    return call_id
        return ""

    def _close_open_records(self) -> None:
        tool_records = self._read_jsonl(self.tool_calls_path)
        finished_tools = {
            str(record.get("tool_call_id", ""))
            for record in tool_records
            if record.get("phase") == "finished"
        }
        for record in tool_records:
            call_id = str(record.get("tool_call_id", ""))
            if record.get("phase") == "called" and call_id not in finished_tools:
                self.log_tool_result(
                    call_id,
                    str(record.get("tool", "unknown")),
                    False,
                    "Run ended before the tool returned a result.",
                    status="failed",
                )
        model_records = self._read_jsonl(self.model_calls_path)
        finished_models = {
            str(record.get("model_call_id", ""))
            for record in model_records
            if record.get("phase") in {"finished", "failed"}
        }
        for record in model_records:
            call_id = str(record.get("model_call_id", ""))
            if record.get("phase") == "started" and call_id not in finished_models:
                self.log_model_call_finished(
                    str(record.get("role", "unknown")),
                    str(record.get("model", "unknown")),
                    call_id=call_id,
                    error="Run ended before the model call returned.",
                )

    def _write_tool_result_artifact(
        self,
        call_id: str,
        full_result_text: str,
        truncated: bool,
    ) -> str:
        if not truncated or not full_result_text:
            return ""
        safe_call_id = "".join(char for char in call_id if char.isalnum() or char in {"-", "_"})
        path = self.tool_results_dir / f"{safe_call_id or 'tool_result'}.json"
        self._write_text(path, full_result_text)
        return str(path.relative_to(self.run_dir).as_posix())

    def _narrative(self, method: str, *args: Any) -> None:
        """Mirror one step into this run's readable narrative (tier 2).

        Tools are recorded here rather than from the trace because every
        execution path logs them here. Imported lazily so the action_ledger
        package carries no UI dependency at import time; best-effort, because a
        narrative failure must never break a run."""
        if not self.enabled:
            return
        try:
            from shamsu.ui.narrative import NarrativeWriter

            writer = NarrativeWriter(self.run_dir, run_id=self.run_id)
            getattr(writer, method)(*args)
        except Exception:
            pass

    def _write_artifact(self, directory: Path, call_id: str, text: str) -> str:
        """Spill one full-fidelity text artifact and return its relative path.
        Goes through _write_text, so it is redacted like everything else."""
        if not text:
            return ""
        safe_call_id = "".join(char for char in (call_id or "") if char.isalnum() or char in {"-", "_"})
        stem = safe_call_id or "model_unknown"
        path = directory / f"{stem}.txt"
        # Callers without a ledger call id (the plain completion path) all land
        # on the same stem, so never let a second trace silently overwrite one.
        suffix = 2
        while path.exists():
            path = directory / f"{stem}_{suffix}.txt"
            suffix += 1
        try:
            self._write_text(path, text)
        except OSError:
            # Artifact capture is best-effort; a full disk must not kill the run.
            return ""
        return str(path.relative_to(self.run_dir).as_posix())

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

    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        records: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict):
                records.append(record)
        return records

    def _count_records(self, path: Path, key: str, value: Any) -> int:
        return sum(1 for record in self._read_jsonl(path) if record.get(key) == value)

    def _max_sequence(self) -> int:
        paths = [
            self.events_path,
            self.decisions_path,
            self.tool_calls_path,
            self.model_calls_path,
            self.mutations_dir / "mutations.jsonl",
        ]
        sequences = [
            int(record.get("sequence", 0) or 0)
            for path in paths
            for record in self._read_jsonl(path)
        ]
        return max(sequences, default=0)

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


def _format_request(
    prompt: str,
    system: str = "",
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
) -> str:
    """Render a model request as readable text: system prompt, every message in
    order (including assistant tool calls), the raw prompt, then the tool
    schemas. Plain sections rather than JSON so the file can be read directly."""
    if not system and not messages and not tools:
        return prompt or ""
    sections: list[str] = []
    if system:
        sections.append(f"===== SYSTEM =====\n{system}")
    for index, message in enumerate(messages or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "?"))
        block = [f"===== MESSAGE {index} ({role}) =====", str(message.get("content", "") or "")]
        name = str(message.get("name", "") or "")
        if name:
            block.insert(1, f"[name: {name}]")
        tool_calls = message.get("tool_calls")
        if tool_calls:
            block.append(f"--- tool_calls ---\n{json.dumps(tool_calls, indent=2, default=str)}")
        sections.append("\n".join(block))
    if prompt:
        sections.append(f"===== PROMPT =====\n{prompt}")
    if tools:
        sections.append(f"===== TOOL SCHEMAS =====\n{json.dumps(tools, indent=2, default=str)}")
    return "\n\n".join(sections)


def _tool_result_text(ok: bool, message: str, data: Any) -> str:
    return json.dumps(
        {
            "ok": bool(ok),
            "message": message,
            "data": data if data is not None else {},
        },
        ensure_ascii=True,
        default=str,
    )


def _open_narrative(ledger: ActionLedger, prompt: str) -> None:
    """Start this run's readable narrative (tier 2 of the log).

    `start_run` is the single entry point every caller - interactive REPL and
    headless CLI alike - uses to begin a turn, so opening here needs no changes
    at the dozen dispatch sites. Imported lazily to keep the action_ledger
    package free of a UI dependency at import time. Best-effort by design."""
    if not ledger.enabled:
        return
    try:
        from shamsu.ui.narrative import NarrativeWriter, write_layout_readme

        session_dir = None
        if ledger.session_id:
            session_dir = ledger.workspace / ".shamsu" / "sessions" / ledger.session_id
        NarrativeWriter(ledger.run_dir, session_dir, run_id=ledger.run_id).open_turn(prompt)
        write_layout_readme(ledger.workspace)
    except Exception:
        pass


def start_run(
    workspace: Path,
    prompt: str,
    config: dict[str, Any] | None = None,
    session_logger: "SessionLogger | None" = None,
) -> ActionLedger:
    session_id = session_logger.session_id if session_logger is not None else ""
    turn_id = session_logger.current_turn_id if session_logger is not None else ""
    ledger = ActionLedger(
        workspace,
        config=config,
        session_id=session_id,
        turn_id=turn_id,
    )
    ledger.start(prompt)
    _open_narrative(ledger, prompt)
    if session_logger is not None:
        session_logger.log(
            "run.linked",
            {"run_id": ledger.run_id, "turn_id": ledger.turn_id},
            "Prompt linked to canonical run",
            workflow_id=ledger.run_id,
        )
    return ledger
