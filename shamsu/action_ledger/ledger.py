"""ActionLedger: the writer side of SHAMSU's local run/debug log.

Storage layout (see agent context/prompts/audit_log.md):

    <workspace>/.shamsu/runs/<run-id>/
        report.md
        .evidence/
            manifest.json
            events.jsonl
            decisions.jsonl
            tool-calls.jsonl
            model-calls.jsonl
            prompts/model_NNNN.txt
            reasoning/model_NNNN.txt
            responses/model_NNNN.txt
            commands/cmd_NNN.stdout.log, cmd_NNN.stderr.log
            diagnostics/error_packet_NNN.json
            tool-results/tool_NNNN.json
            mutations/mutations.jsonl
            context-preview.json
            final-output.md
            summary.json

The JSONL rows stay small. In verbose mode, full prompt / reasoning / response
text is spilled to `.evidence/prompts/`, `.evidence/reasoning/`, and
`.evidence/responses/` and referenced by relative path, the same way command
and large tool results work. `essential` is the default; set `log_level` to
`verbose` (or `SHAMSU_LOG_LEVEL=verbose`) for full evidence.

Every write path funnels through _append_jsonl / _write_json / _write_text,
which redact before touching disk - this is the single enforcement point for
"no secrets on disk", rather than relying on every call site to remember.

This module never reads from Graphiti, Codebase-Memory MCP, or the context
pipeline, and nothing here is ever fed back into a model prompt.
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from shamsu.action_ledger.config import (
    DEFAULT_CONFIG,
    load_config,
    resolve_log_level,
    wants_full_artifacts,
)
from shamsu.action_ledger.ids import new_run_id
from shamsu.action_ledger.redaction import redact_text, redact_value
from shamsu.context.budget import count_tokens
from shamsu.safety.sandbox import Sandbox

if TYPE_CHECKING:
    from shamsu.session.manager import SessionLogger

PREVIEW_CHARS = 800
MAX_LIST_ITEMS = 20
TOOL_RESULT_ARTIFACT_TOKEN_THRESHOLD = 1000
COMMAND_INLINE_CHARS = 4000
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
        source: str = "",
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.sandbox = Sandbox(self.workspace)
        self.run_id = run_id or new_run_id()
        suffix = self.run_id.removeprefix("run_")
        self.session_id = session_id or f"session_{suffix}"
        self.turn_id = turn_id or f"turn_{suffix}"
        # Which surface asked for this turn: "cli" | "telegram" | "web". One
        # scrollback now interleaves all three, and the badge on each row is
        # the only thing that distinguishes a message steered in from a phone
        # from the local prompt that started the run.
        self.source = (source or "cli").strip().lower()
        self.root_operation_id = "op_root"
        self.run_dir = self.sandbox.validate(Path(".shamsu") / "runs" / self.run_id)
        self.config = config or load_config(self.workspace)
        self.enabled = bool(self.config.get("enabled", True))
        self.log_level = resolve_log_level(self.config)
        self.full_artifacts = wants_full_artifacts(self.config)
        self._max_inline = int(self.config.get("max_inline_event_size", DEFAULT_CONFIG["max_inline_event_size"]))
        self.friendly_transcript = bool(self.config.get("friendly_model_transcript", True))
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
        self._friendly_model_started: dict[str, dict[str, Any]] = {}
        self._friendly_model_contexts: dict[str, dict[str, str]] = {}

    # -- paths ----------------------------------------------------------------

    @property
    def evidence_dir(self) -> Path:
        return self.run_dir / ".evidence"

    @property
    def manifest_path(self) -> Path:
        return self.evidence_dir / "manifest.json"

    @property
    def events_path(self) -> Path:
        return self.evidence_dir / "events.jsonl"

    @property
    def decisions_path(self) -> Path:
        return self.evidence_dir / "decisions.jsonl"

    @property
    def tool_calls_path(self) -> Path:
        return self.evidence_dir / "tool-calls.jsonl"

    @property
    def model_calls_path(self) -> Path:
        return self.evidence_dir / "model-calls.jsonl"

    @property
    def attachments_dir(self) -> Path:
        """One flat folder for every spilled payload.

        This used to be eight: `commands/`, `contexts/`, `diagnostics/`,
        `mutations/`, `prompts/`, `reasoning/`, `responses/`, `tool-results/`.
        Sorting by payload type is sorting by the one key nobody searches on -
        a reader arrives knowing *when* something happened and wants the thing
        that happened next, which put every answer one folder away from every
        other part of its own story.

        Flat, with the type in the filename instead, so `ls` shows the whole
        run in one screen and the sequence numbers still sort. Every path
        recorded in the JSONL rows stays relative to `run_dir`, so `store.py`
        and the export path resolve them exactly as before."""
        return self.evidence_dir / "attachments"

    # The eight names below are what the rest of the codebase already asks for.
    # They stay as properties rather than becoming a single call site edit
    # sweep: a spill's *kind* still decides its filename prefix, and keeping the
    # names means `executor.py` and the tests read unchanged.

    @property
    def commands_dir(self) -> Path:
        return self.attachments_dir

    @property
    def diagnostics_dir(self) -> Path:
        return self.attachments_dir

    @property
    def tool_results_dir(self) -> Path:
        return self.attachments_dir

    @property
    def prompts_dir(self) -> Path:
        return self.attachments_dir

    @property
    def cot_dir(self) -> Path:
        return self.attachments_dir

    @property
    def responses_dir(self) -> Path:
        return self.attachments_dir

    @property
    def session_log_dir(self) -> Path:
        """Where this turn's readable log lives.

        The session, when there is one - a turn only means something inside the
        conversation it belongs to. A headless one-shot has no session and would
        otherwise write its log nowhere, so the run directory is the fallback."""
        if self.session_id:
            return self.workspace / ".shamsu" / "sessions" / self.session_id
        return self.run_dir

    @property
    def summary_log_path(self) -> Path:
        return self.session_log_dir / "log-summary.md"

    @property
    def detail_log_path(self) -> Path:
        return self.session_log_dir / "log-detailed.md"

    @property
    def log_attachments_dir(self) -> Path:
        return self.session_log_dir / "attachments"

    @property
    def mutations_dir(self) -> Path:
        return self.attachments_dir

    @property
    def context_preview_path(self) -> Path:
        return self.evidence_dir / "context-preview.json"

    @property
    def contexts_dir(self) -> Path:
        return self.attachments_dir

    @property
    def mutations_path(self) -> Path:
        return self.attachments_dir / "mutations.jsonl"

    @property
    def final_output_path(self) -> Path:
        return self.evidence_dir / "final-output.md"

    @property
    def summary_path(self) -> Path:
        return self.evidence_dir / "summary.json"

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
        self.attachments_dir.mkdir(parents=True, exist_ok=True)
        for path in (
            self.events_path,
            self.decisions_path,
            self.tool_calls_path,
            self.model_calls_path,
            self.mutations_path,
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
                "events": ".evidence/events.jsonl",
                "decisions": ".evidence/decisions.jsonl",
                "tool_calls": ".evidence/tool-calls.jsonl",
                "model_calls": ".evidence/model-calls.jsonl",
                "attachments": ".evidence/attachments/",
                "mutations": ".evidence/attachments/mutations.jsonl",
                "summary": f"sessions/{self.session_id}/log-summary.md",
                "detail": f"sessions/{self.session_id}/log-detailed.md",
                "final_output": ".evidence/final-output.md",
            },
        }
        self._write_json(self.summary_path, summary)
        self._narrative("close_turn", final_output, status, verdict_reason(summary))
        return summary

    def fail(self, error: str) -> dict[str, Any]:
        return self.finish(final_output=error, status="failed")

    def record_final_response(self, final_output: str) -> None:
        if not self.enabled:
            return
        self._write_text(self.final_output_path, final_output or "")
        self.log_event(
            "final_response_written",
            final_output_path=".evidence/final-output.md",
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
        elif self._has_unrecovered_denial(events):
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
            if tool not in {"read_file", "edit_file", "append_file", "write_file"}:
                return True
            failed_args = arguments.get(str(record.get("tool_call_id", "")), {})
            failed_path = str(failed_args.get("filepath") or "").replace("\\", "/")
            recovered = False
            for later in records[failure_index + 1 :]:
                if later.get("phase") != "finished" or not bool(later.get("ok")):
                    continue
                if later.get("tool") not in {"edit_file", "append_file", "write_file"}:
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
    def _has_unrecovered_denial(events: list[dict[str, Any]]) -> bool:
        """A denial the agent worked AROUND is not a failed run.

        Any `approval_denied` used to end the run as `denied`, forever, whatever
        happened next. Live 2026-08-19 a headless run repaired a truncated file
        in one turn - one patch, braces 8/3 to 26/26, `node --check` clean - and
        reported `denied`, exit 1, because the model had earlier tried
        `cat js/main.js | wc -l`, been refused, and simply used read_file
        instead.

        Every other failure kind in this method already asks whether the agent
        recovered; approval denial never did. It does now, on the same rule: a
        denial counts only if NOTHING succeeded after it. `denied` should mean
        the run could not proceed, not that something was refused once.

        Deliberately positional. A denial early in a run that then does its work
        is recovered; a denial that ends the run - the shape of a genuinely
        blocked one - has nothing after it and still reports `denied`.
        """
        last_denial = -1
        for index, event in enumerate(events):
            if str(event.get("type", "")) == "approval_denied":
                last_denial = index
        if last_denial < 0:
            return False
        recovered_by = {
            "patch_apply_succeeded",
            "verification_passed",
            "mutation_finished",
        }
        for event in events[last_denial + 1:]:
            event_type = str(event.get("type", ""))
            if event_type in recovered_by:
                return False
            if event_type == "command_finished":
                try:
                    if int(event.get("exit_code", 1)) == 0:
                        return False
                except (TypeError, ValueError):
                    continue
        return True

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
        if event_type.startswith("approval"):
            # Approvals were previously visible only as a line in events.jsonl,
            # so a turn that sat four minutes waiting for a human read exactly
            # like one that spent four minutes thinking. They get their own row.
            self._narrative("append_approval", event)
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
        self._narrative("append_decision", record)
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
        self._narrative("append_tool_result", name, bool(ok), message, data)
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
            record["prompt_path"] = self._write_artifact(self.prompts_dir, call_id, request_text, "prompt")
        friendly_prompt_path = self._write_friendly_model_artifact(call_id, "prompt", request_text)
        self._friendly_model_started[call_id] = {
            "timestamp": record["timestamp"],
            "role": role,
            "model": model,
            "prompt_path": friendly_prompt_path,
            "prompt_preview": _preview(request_text, 240),
        }
        if self.config.get("log_model_prompts", True):
            record["prompt_preview"] = _preview(request_text)
        self._append_jsonl(self.model_calls_path, record)
        self._narrative("append_model_call", call_id, role, model, request_text)
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
            record["response_path"] = self._write_artifact(self.responses_dir, call_id, response, "response")
        friendly_response_path = self._write_friendly_model_artifact(
            call_id,
            "response",
            response or error or "The model call ended without a response.",
        )
        record["response_chars"] = len(response or "")
        if self.config.get("log_model_responses", True):
            record["response_preview"] = _preview(response or "")
        self._append_jsonl(self.model_calls_path, record)
        self._append_friendly_model_transcript(
            call_id=call_id,
            role=role,
            model=model,
            response=response,
            response_path=friendly_response_path,
            error=error,
            duration_ms=record["duration_ms"],
            meta=meta or {},
        )
        self._narrative(
            "append_model_result",
            call_id,
            role,
            model,
            response or "",
            error,
            record["duration_ms"],
        )
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
            cot_path = self._write_artifact(self.cot_dir, call_id or "model_unknown", thinking, "reasoning")
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
        self._narrative("append_model_reasoning", call_id, role, model, thinking)
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
        keep_output_files = self.full_artifacts or exit_code != 0 or any(
            len(text or "") > COMMAND_INLINE_CHARS for text in (stdout, stderr)
        )
        stdout_relative = ""
        stderr_relative = ""
        if keep_output_files:
            self._write_text(stdout_path, stdout or "")
            self._write_text(stderr_path, stderr or "")
            stdout_relative = str(stdout_path.relative_to(self.run_dir).as_posix())
            stderr_relative = str(stderr_path.relative_to(self.run_dir).as_posix())
        event = self.log_event(
            "command_finished",
            cmd_id=cmd_id,
            command=command,
            cwd=str(cwd),
            exit_code=exit_code,
            stdout_preview=_preview(stdout or "", COMMAND_INLINE_CHARS),
            stderr_preview=_preview(stderr or "", COMMAND_INLINE_CHARS),
            stdout_path=stdout_relative,
            stderr_path=stderr_relative,
            diagnostics_path=diagnostics_path,
        )
        self._narrative(
            "append_command",
            command,
            exit_code,
            stdout or "",
            stderr or "",
        )
        return event

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

    def record_unparsed_response(
        self,
        role: str,
        model: str,
        raw_response: str,
        *,
        reason: str,
        round_index: int = 0,
        parse_error: str = "",
    ) -> str:
        """Persist a model response the harness could not turn into a tool call.

        Written at EVERY log level, unlike ``responses/<call_id>.txt``. ``essential``
        is the default, so the one response most needed for diagnosis - the
        mutation round that produced no file - was precisely the one never kept.
        That is why the 2026-08-03 failure was misread as "the model returned
        prose" for 128 runs: the evidence proving otherwise was being discarded.

        Lands under ``.evidence/diagnostics`` beside exception tracebacks, which
        are also always retained, and goes through ``_write_text`` so it is
        redacted like everything else.
        """
        if not self.enabled:
            return ""
        idx = self._diagnostics_seq
        self._diagnostics_seq += 1
        safe_reason = "".join(
            char for char in reason.lower() if char.isalnum() or char in {"-", "_"}
        )
        path = self.diagnostics_dir / f"unparsed_response_{idx:03d}_{safe_reason or 'unknown'}.txt"
        self._write_text(path, raw_response or "The model returned an empty response.")
        relative = str(path.relative_to(self.run_dir).as_posix())
        self.log_event(
            "unparsed_model_response",
            role=role,
            model=model,
            reason=reason,
            parse_error=parse_error,
            response_chars=len(raw_response or ""),
            round=round_index,
            path=relative,
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
        self._narrative(
            "append_mutation",
            status,
            list(touched_files or []),
            error,
            transaction_id,
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
        self._narrative(
            "append_verification",
            passed,
            command,
            summary,
            exit_code,
            list(files or []),
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
        record = {
            "attempt_index": attempt_index,
            "outcome": outcome,
            "kept": bool(kept),
            "files_changed": list(files_changed or []),
            "before_signature": before_signature,
            "after_signature": after_signature,
            "note": note,
            "verifier_id": verifier_id,
            "command": command,
        }
        self.log_event("repair_attempt_finished", **record)
        # The repair loop is the one caller that knows its own attempt number
        # and whether the result survived, which is what turns two adjacent
        # rows into one "1 of 2 kept" group.
        self._narrative("append_repair_attempt", record)

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
        context_relative = ""
        if self.full_artifacts:
            self._write_json(context_path, record)
            context_relative = str(context_path.relative_to(self.run_dir).as_posix())
        if model_call_id:
            friendly_context_path = self._write_friendly_model_artifact(
                model_call_id,
                "context",
                json.dumps(record, indent=2, default=str),
            )
            self._friendly_model_contexts[model_call_id] = {
                "path": friendly_context_path,
                "preview": _context_summary(record),
            }
        # Backward-compatible latest pointer for old readers and exports.
        self._write_json(self.context_preview_path, record)
        self._narrative("append_context", record)
        self.log_event(
            "context_pack_built",
            context_id=context_id,
            model_call_id=model_call_id,
            context_path=context_relative or ".evidence/context-preview.json",
            task_id=preview.get("task_id"),
            specialist=preview.get("specialist"),
            token_estimate=preview.get("token_estimate"),
            snippet_count=len(preview.get("snippets") or []),
        )

    def log_notice(self, message: str, level: str = "warning") -> None:
        """Something that happened TO the turn - an elision, a timeout warning.

        Not a step the agent took, so it gets a row of its own rather than
        being folded into whatever ran next."""
        self.log_event("turn_notice", message=message, level=level)
        self._narrative("append_notice", message, level)

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
        """Mirror one step into this session's readable log.

        Tools are recorded here rather than from the trace because every
        execution path logs them here. Imported lazily so the action_ledger
        package carries no UI dependency at import time; best-effort, because a
        logging failure must never break a run.

        `writer_for` rather than a bare constructor: two of the rows this
        produces - a reasoning sub-panel and a grouped retry - need to see more
        than one call, and a writer built fresh per call cannot."""
        if not self.enabled:
            return
        try:
            self._turn_log_writer().__getattribute__(method)(*args)
        except Exception:
            pass

    def _turn_log_writer(self) -> Any:
        from shamsu.ui.turnlog import writer_for

        session_dir = None
        if self.session_id:
            session_dir = self.workspace / ".shamsu" / "sessions" / self.session_id
        return writer_for(
            self.run_dir,
            session_dir,
            run_id=self.run_id,
            log_level=self.log_level,
            turn_id=self.turn_id,
            source=self.source,
        )

    def _write_artifact(
        self, directory: Path, call_id: str, text: str, kind: str = ""
    ) -> str:
        """Spill one full-fidelity text artifact and return its relative path.
        Goes through _write_text, so it is redacted like everything else.

        *kind* is what used to be the folder. One model call spills a prompt, a
        reasoning trace, and a response, all under the same call id - in three
        separate folders that was three files named `model_0007.txt`, and in one
        flat folder it would have been a silent overwrite (or, thanks to the
        de-duplicating loop below, three files whose names no longer said which
        was which). The kind moves into the filename so a flat folder loses
        nothing the typed folders carried."""
        if not text:
            return ""
        safe_call_id = "".join(char for char in (call_id or "") if char.isalnum() or char in {"-", "_"})
        safe_kind = "".join(char for char in (kind or "") if char.isalnum() or char in {"-", "_"})
        stem = safe_call_id or "model_unknown"
        if safe_kind:
            stem = f"{stem}.{safe_kind}"
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
        self._mirror_session_development_log(path, safe)

    def _mirror_session_development_log(self, source_path: Path, record: dict[str, Any]) -> None:
        if not self.session_id or self.session_id.startswith("session_"):
            return
        log_dir = self.workspace / ".shamsu" / "logs" / self.session_id
        try:
            stream = source_path.relative_to(self.run_dir).as_posix()
        except ValueError:
            stream = source_path.name
        entry = {
            "timestamp": str(record.get("timestamp") or _now()),
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "run_id": self.run_id,
            "kind": "run.evidence",
            "stream": stream,
            "record": record,
        }
        if self.full_artifacts:
            artifact_text = self._inline_artifact_text(record)
            if artifact_text:
                entry["artifact_text"] = artifact_text
        path = log_dir / "agent-development-log.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")
        except OSError:
            pass

    def _inline_artifact_text(self, record: dict[str, Any]) -> dict[str, str]:
        texts: dict[str, str] = {}
        for key, label in (
            ("prompt_path", "prompt"),
            ("response_path", "response"),
            ("cot_path", "reasoning"),
        ):
            relative = str(record.get(key) or "")
            if not relative:
                continue
            try:
                path = (self.run_dir / relative).resolve()
                path.relative_to(self.run_dir.resolve())
                texts[label] = path.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                continue
        return texts

    @property
    def friendly_log_dir(self) -> Path:
        return self.sandbox.validate(Path(".shamsu") / "logs" / self.session_id)

    @property
    def friendly_model_transcript_path(self) -> Path:
        return self.friendly_log_dir / "model-transcript.md"

    @property
    def friendly_model_transcript_csv_path(self) -> Path:
        return self.friendly_log_dir / "model-transcript.csv"

    @property
    def friendly_model_artifacts_dir(self) -> Path:
        return self.friendly_log_dir / "model-transcript"

    def _write_friendly_model_artifact(self, call_id: str, kind: str, text: str) -> str:
        if not self.enabled or not self.friendly_transcript:
            return ""
        safe_call_id = "".join(char for char in (call_id or "model_unknown") if char.isalnum() or char in {"-", "_"})
        safe_kind = "".join(char for char in kind if char.isalnum() or char in {"-", "_"})
        path = self.friendly_model_artifacts_dir / f"{safe_call_id}.{safe_kind or 'artifact'}.txt"
        try:
            self._write_text(path, text or "")
            return str(path.relative_to(self.friendly_log_dir).as_posix())
        except OSError:
            return ""

    def _append_friendly_model_transcript(
        self,
        *,
        call_id: str,
        role: str,
        model: str,
        response: str,
        response_path: str,
        error: str,
        duration_ms: float | None,
        meta: dict[str, Any],
    ) -> None:
        if not self.enabled or not self.friendly_transcript:
            return
        started = self._friendly_model_started.get(call_id, {})
        context = self._friendly_model_contexts.get(call_id, {})
        thinking_chars = _int_from_meta(meta, "thinking_chars")
        cot = f"captured ({thinking_chars} chars)" if thinking_chars > 0 else "not captured"
        row = {
            "timestamp": str(started.get("timestamp") or _now()),
            "run_id": self.run_id,
            "model_call_id": call_id,
            "role": str(started.get("role") or role),
            "model": str(started.get("model") or model),
            "duration_ms": "" if duration_ms is None else str(duration_ms),
            "cot": cot,
            "context": _link_with_preview(str(context.get("path") or ""), str(context.get("preview") or "")),
            "prompt": _link_with_preview(
                str(started.get("prompt_path") or ""),
                str(started.get("prompt_preview") or ""),
            ),
            "model_response": _link_with_preview(
                response_path,
                error or _preview(response or "", 240),
            ),
        }
        try:
            self._append_friendly_markdown_row(row)
            self._append_friendly_csv_row(row)
        except OSError:
            return

    def _append_friendly_markdown_row(self, row: dict[str, str]) -> None:
        path = self.friendly_model_transcript_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(
                "\n".join(
                    [
                        "# SHAMSU Model Transcript",
                        "",
                        "Readable index of model calls. Full prompt, context, and response text lives beside this file under `model-transcript/`.",
                        "",
                        "| timestamp | run | call | role | model | cot | context | prompt | model response |",
                        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
        cells = [
            row["timestamp"],
            row["run_id"],
            row["model_call_id"],
            row["role"],
            row["model"],
            row["cot"],
            row["context"],
            row["prompt"],
            row["model_response"],
        ]
        with path.open("a", encoding="utf-8") as handle:
            handle.write("| " + " | ".join(_markdown_cell(cell) for cell in cells) + " |\n")

    def _append_friendly_csv_row(self, row: dict[str, str]) -> None:
        path = self.friendly_model_transcript_csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "timestamp",
            "run_id",
            "model_call_id",
            "role",
            "model",
            "duration_ms",
            "cot",
            "context",
            "prompt",
            "model_response",
        ]
        exists = path.exists()
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if not exists:
                writer.writeheader()
            writer.writerow({field: row.get(field, "") for field in fieldnames})

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
            self.mutations_path,
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
    """Start this turn in the session's readable log.

    `start_run` is the single entry point every caller - interactive REPL and
    headless CLI alike - uses to begin a turn, so opening here needs no changes
    at the dozen dispatch sites. Imported lazily to keep the action_ledger
    package free of a UI dependency at import time. Best-effort by design."""
    if not ledger.enabled:
        return
    try:
        from shamsu.ui.turnlog import write_layout_readme

        ledger._turn_log_writer().open_turn(prompt)
        write_layout_readme(ledger.workspace)
    except Exception:
        pass


def start_run(
    workspace: Path,
    prompt: str,
    config: dict[str, Any] | None = None,
    session_logger: "SessionLogger | None" = None,
    source: str = "",
) -> ActionLedger:
    session_id = session_logger.session_id if session_logger is not None else ""
    turn_id = session_logger.current_turn_id if session_logger is not None else ""
    ledger = ActionLedger(
        workspace,
        config=config,
        session_id=session_id,
        turn_id=turn_id,
        source=source,
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


def verdict_reason(summary: dict[str, Any] | None) -> str:
    """The one line that goes beside the verdict: what the turn actually did.

    "success" on its own does not distinguish a turn that changed two files and
    verified them from one that answered a question - and those are the two
    things you most want to tell apart when scanning a session."""
    summary = summary or {}
    tools = summary.get("tools") or []
    changed = [str(path) for path in (summary.get("changed_files") or [])]
    parts: list[str] = []
    if changed:
        parts.append(f"{len(changed)} file{'s' if len(changed) != 1 else ''} changed")
    elif tools:
        # Always say this when it is true. Live 2026-08-21 a failed turn read
        # "Verdict: failed - checks passed", because the syntax check on a file
        # it merely READ was the only fact the reason had. "nothing changed" is
        # the fact that actually explains the verdict.
        parts.append(f"{len(tools)} tool call{'s' if len(tools) != 1 else ''}, nothing changed")
    checks = [item for item in (summary.get("verification") or []) if isinstance(item, dict)]
    failed = [item for item in checks if str(item.get("type", "")).endswith("failed")]
    if failed:
        parts.append(f"{len(failed)} check{'s' if len(failed) != 1 else ''} failed")
    elif checks and changed:
        # Only a claim about something that moved. "checks passed" on a turn
        # that wrote nothing says less than it appears to.
        parts.append("checks passed")
    return ", ".join(parts)


def _context_summary(record: dict[str, Any]) -> str:
    specialist = str(record.get("specialist") or "")
    token_estimate = record.get("token_estimate")
    messages = record.get("messages")
    message_count = len(messages) if isinstance(messages, list) else int(record.get("message_count", 0) or 0)
    snippets = record.get("snippets") or []
    snippet_count = len(snippets) if isinstance(snippets, list) else 0
    parts = []
    if specialist:
        parts.append(specialist)
    if message_count:
        parts.append(f"{message_count} message(s)")
    if token_estimate:
        parts.append(f"~{token_estimate} token(s)")
    if snippet_count:
        parts.append(f"{snippet_count} snippet(s)")
    return ", ".join(parts) or "context captured"


def _int_from_meta(meta: dict[str, Any], key: str) -> int:
    try:
        return int(meta.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _link_with_preview(path: str, preview: str) -> str:
    preview = " ".join((preview or "").split())
    preview = _preview(preview, 240)
    if path:
        label = path.rsplit("/", 1)[-1]
        if preview:
            return f"[{label}]({path})<br>{preview}"
        return f"[{label}]({path})"
    return preview or "-"


def _markdown_cell(text: str) -> str:
    clean = redact_text(str(text or ""))
    clean = clean.replace("|", "\\|").replace("\r\n", "\n").replace("\r", "\n")
    return clean.replace("\n", "<br>")
