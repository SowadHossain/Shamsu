"""Two Markdown files per session: what happened, and what it actually said.

The log used to be nine places at once. One `report.md` per run held the story,
and beside it `.evidence/` held eight typed subfolders - `prompts/`,
`reasoning/`, `responses/`, `tool-results/`, `commands/`, `contexts/`,
`diagnostics/`, `mutations/` - each holding one flavour of the same turn. To
answer "why did that patch fail" you opened the report, found the tool call,
guessed which numbered file under `tool-results/` was the one, and opened that.
Eight folders is not eight times the information; it is one story cut into
eight piles sorted by the wrong key.

The replacement sorts by the only key a reader has, which is time:

    .shamsu/sessions/<session-id>/
        log-summary.md     every action, one line each, in order
        log-detailed.md    the same actions, with the payloads
        attachments/       flat; only what was too big to inline
        session.json
        messages.jsonl

`log-summary.md` is the index you skim. Each row is an icon, a title, the
surface it came from, an outcome, and a duration - and, when there is more to
see, a link into `log-detailed.md`. `log-detailed.md` is the same sequence with
the prompts, diffs, command output, and reasoning traces attached, each under an
anchor the summary links to. Both are appended as the turn runs, so a session
that is killed mid-turn still has everything up to the moment it died.

Five things the old report could not show, and this one can:

* **Reasoning.** A thinking model's trace is a collapsed sub-panel *inside* the
  model's own entry, not a separate `reasoning/model_0007.txt` in another
  folder. Leaked inline `<think>` blocks are pulled out of the visible answer
  and rendered there too, so the same trace reads the same way whether Ollama
  returned it in its own field or the model wrote it into the text.
* **Approvals.** A request and its resolution are their own row, carrying who
  decided, on which surface, and how long the run sat waiting.
* **Retries.** Consecutive attempts on one file are held back and emitted as a
  single group - "1 of 2 kept" - so a failed patch stays visually attached to
  the retry that superseded it. They were previously two unrelated rows and the
  reader had to notice the filename matched.
* **Surface.** Every row can name where its input came from. One scrollback now
  interleaves cli, telegram, and web; without the badge a steering message from
  a phone looks exactly like the local prompt that started the run.
* **Overflow.** A 900-line file read does not belong inline in either document.
  Over the threshold it is written to `attachments/` and linked, which is the
  one thing the old `tool-results/` folder got right and the only reason a
  subfolder survived at all.

Everything here is best-effort. A logging failure must never break a run, so
every write is wrapped and every failure is dropped. Text is redacted through
`shamsu.safety.commands.redact`, the project's single secret-pattern source,
and nothing written here is ever read back into a model prompt.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shamsu.action_ledger.config import resolve_log_level
from shamsu.safety.commands import redact
from shamsu.ui.trace import EVENT_LABELS

SUMMARY_FILE = "log-summary.md"
DETAILED_FILE = "log-detailed.md"
ATTACHMENTS_DIR = "attachments"

#: Anything longer than this is written to `attachments/` and linked instead of
#: pasted into `log-detailed.md`. Sized so a normal file read, a diff, or a
#: command's output stays inline - those are what you came to read - while a
#: whole log file or a dumped JSON blob does not bury the row after it.
OVERFLOW_CHARS = 2400

#: Bounds on inline prose. The full text is always in `attachments/` once it is
#: over OVERFLOW_CHARS, so these only ever clip something already linked.
_MAX_PROMPT_CHARS = 4000
_MAX_ANSWER_CHARS = 4000
_MAX_TITLE_CHARS = 160
_MAX_MESSAGE_CHARS = 600
_MAX_VALUE_CHARS = 200

_CLOSED_MARKER = "<!-- turn-closed -->"

# Row icons. Deliberately geometric rather than emoji: these are read in a
# terminal as often as in a Markdown viewer, and emoji width is unreliable there.
ICON_MODEL = "◆"      # black diamond
ICON_TOOL = "▤"       # square with horizontal fill
ICON_APPROVAL = "⚑"   # flag
ICON_PATCH = "✎"      # pencil
ICON_RETRY = "↺"      # anticlockwise open circle arrow
ICON_COMMAND = "▶"    # right-pointing triangle
ICON_PASS = "✓"       # check
ICON_FAIL = "✗"       # ballot x
ICON_NOTE = "·"       # middle dot
ICON_STEER = "✉"      # envelope

# `<think>` is Ollama's; `<thought>` and `<reasoning>` are what other local
# models emit. All three mean the same thing and all three leak into the
# visible answer when the model is not asked for structured reasoning.
_THOUGHT_RE = re.compile(
    r"<(think|thought|reasoning)>(?P<body>.*?)</\1>", re.DOTALL | re.IGNORECASE
)
# An unterminated trailing block: everything after it is reasoning. Common when
# a small model runs out of budget mid-trace.
_DANGLING_THOUGHT_RE = re.compile(
    r"<(?:think|thought|reasoning)>(?P<body>.*)\Z", re.DOTALL | re.IGNORECASE
)

# Mutation statuses that mean the attempt survived. Everything else - failed,
# rolled_back, rollback_failed - is an attempt that was superseded or lost.
_KEPT_STATUSES = frozenset({"applied", "committed", "ok"})

# Trace events with nothing to say to a human, plus the tool events: those are
# recorded from the ActionLedger instead, which every execution path funnels
# through. The trace events only fire on the chat loop, so a composite or
# scaffold run would otherwise show no tools at all.
_SKIP_EVENTS = frozenset(
    {"assistant.content", "context.sent", "tool.started", "tool.finished", "tool.failed"}
)

_ESSENTIAL_EVENTS = frozenset(
    {
        "route.detected",
        "plan.created",
        "clarification.needed",
        "clarification.answered",
        "tool.salvaged",
        "verify.result",
        "workflow.blocked",
        "workflow.finished",
        "run.timed_out",
        "run.cancelled",
    }
)

# Tool arguments worth naming in a row title: which file, which command.
_TOOL_ARG_KEYS = ("filepath", "path", "command", "query", "url", "pattern")

# Tools whose name reads better as a verb phrase in the summary. Anything not
# here falls back to the bare tool name, which is still correct - this is
# polish for the handful a reader sees on every single turn.
_TOOL_VERBS = {
    "read_file": "Reading",
    "read_symbol": "Reading symbol",
    "outline": "Outlining",
    "write_file": "Writing",
    "append_file": "Appending to",
    "patch_file": "Editing",
    "replace_symbol": "Replacing symbol in",
    "search_files": "Searching",
    "find_file": "Finding",
    "run_command": "Running",
    "run_tests": "Testing",
    "delete_file": "Deleting",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class TurnLogWriter:
    """Appends one turn to a session's `log-summary.md` and `log-detailed.md`.

    Path-based on purpose, like the writer it replaces: it never imports the
    ActionLedger or the SessionLogger, so it can be pointed at any directory
    and tested without building either.

    Both documents live in *session_dir* when there is one. A headless one-shot
    has no session, and its turn would otherwise be written nowhere, so the run
    directory is the fallback rather than a reason to drop the log.
    """

    def __init__(
        self,
        run_dir: Path,
        session_dir: Path | None = None,
        run_id: str = "",
        log_level: str = "essential",
        turn_id: str = "",
        source: str = "",
    ) -> None:
        self.run_dir = Path(run_dir)
        self.session_dir = Path(session_dir) if session_dir is not None else None
        self.run_id = run_id or self.run_dir.name
        self.turn_id = turn_id or self.run_id
        self.source = (source or "").strip().lower()
        self.log_level = resolve_log_level({"log_level": log_level})
        self.verbose = self.log_level == "verbose"
        # Two of the five additions need to see more than one call at a time: a
        # reasoning trace has to wait for the response it belongs to, and a
        # retry has to wait for the attempt it supersedes. Neither survives in a
        # per-instance buffer, because the caller builds a fresh writer for
        # every single row - see `writer_for`, which is how a turn keeps one.
        self._pending: list[dict[str, Any]] = []
        # Anchors written to `log-detailed.md` so far. None until counted from
        # the file once; see `_anchor_seq`.
        self._anchor_count: int | None = None

    # -- paths -------------------------------------------------------------

    @property
    def home(self) -> Path:
        """Where both documents and `attachments/` live for this turn."""
        return self.session_dir if self.session_dir is not None else self.run_dir

    @property
    def summary_path(self) -> Path:
        return self.home / SUMMARY_FILE

    @property
    def detailed_path(self) -> Path:
        return self.home / DETAILED_FILE

    @property
    def attachments_dir(self) -> Path:
        return self.home / ATTACHMENTS_DIR

    # -- turn lifecycle ----------------------------------------------------

    def open_turn(self, prompt: str, route: str = "", when: str = "") -> None:
        """Start this turn in both documents with the prompt that triggered it.

        The prompt is the one row that is never collapsed and never a link, in
        either file: it is the question being answered.

        *when* overrides the clock, for a turn being replayed from an older
        record rather than happening now - see `chatlog_migrate`. Stamping a
        2026-08-19 turn with today's date would make the document lie about the
        one thing it is ordered by."""
        self._ensure_headers()
        via = f" · via {self.source}" if self.source else ""
        text = _clip(redact(str(prompt or "")).strip(), _MAX_PROMPT_CHARS)
        block = [
            f"## Turn `{self.turn_id}`",
            "",
            f"{when or _stamp()}{via}",
            "",
            _blockquote(text) if text else "> _(no prompt)_",
            "",
        ]
        if route:
            block.extend([f"**Route:** {redact(str(route))}", ""])
        self._append_summary("\n".join(block) + "\n")
        self._append_detailed("\n".join(block) + "\n")

    def close_turn(self, final: str = "", status: str = "", reason: str = "") -> None:
        """Finish the turn: the verdict, then the answer.

        Both are always visible in both documents. The verdict is the pill you
        look for when scanning a long session; the answer is the reply the user
        actually read, and a log that makes you click for it is not a log of the
        conversation. Safe to call more than once - the marker keeps a second
        call from appending a duplicate."""
        if self._is_closed():
            return
        self._flush_pending()
        answer = _clip(redact(str(final or "")).strip(), _MAX_ANSWER_CHARS)
        verdict = _verdict_icon(status)
        note = " ".join(_clip(redact(str(reason or "")), _MAX_TITLE_CHARS).split())
        headline = f"{verdict} **Verdict: {status or 'unknown'}**" + (
            f" — {note}" if note else ""
        )
        body = answer or "_(no final answer recorded)_"
        self._append_summary(
            f"\n{headline}\n\n**Agent final output**\n\n{body}\n\n{_CLOSED_MARKER}\n\n"
        )
        link = self._spill_if_large(body, "final", "md")
        self._append_detailed(
            f"\n{headline}\n\n**Agent final output**\n\n"
            + (link or body)
            + f"\n\n{_CLOSED_MARKER}\n\n"
        )

    # -- rows --------------------------------------------------------------

    def append(
        self, event_type: str, message: str, payload: dict[str, Any] | None = None
    ) -> None:
        """One trace event. Most are noise in a human log; see _SKIP_EVENTS."""
        if event_type in _SKIP_EVENTS:
            return
        if not self.verbose and event_type not in _ESSENTIAL_EVENTS:
            return
        label = EVENT_LABELS.get(event_type, event_type)
        text = " ".join(_clip(redact(str(message or "")).strip(), _MAX_MESSAGE_CHARS).split())
        title = f"**{label}**" + (f" — {text}" if text else "")
        self._row(ICON_NOTE, title, kind="note", detail=payload if self.verbose else None)

    def append_tool_call(self, name: str, arguments: dict[str, Any] | None = None) -> None:
        """Hold the call until its result, so the two are one row.

        Driven from the ActionLedger rather than the trace, because every
        execution path logs its tools there - the chat loop, the composite
        runner, the scaffold pipeline.

        Buffered because a call and its result are one ACTION to a reader, and
        the summary gives one line per action. Nothing is lost if the result
        never comes: `_flush_pending` emits the call on its own."""
        self._flush_pending()
        self._pending.append(
            {"kind": "tool", "name": str(name), "arguments": dict(arguments or {})}
        )

    def append_tool_result(
        self, name: str, ok: bool, message: str = "", data: Any = None
    ) -> None:
        """Close the open call, or stand alone if something interrupted it.

        A tool can be interrupted mid-flight - an approval prompt fires from
        inside `patch_file` and lands between the call and its result. That
        flushes the call as its own row to keep the order honest, so the result
        arrives with nothing to attach to and names its own tool instead."""
        sections: list[tuple[str, str, str]] = []
        if message:
            sections.append(("Result", redact(str(message)), "text"))
        if data not in (None, {}, [], ""):
            sections.append(("Result data", _as_json(data), "json"))
        call = self._take_tool(str(name))
        if call is not None:
            arguments = call.get("arguments") or {}
            if arguments:
                sections.insert(0, ("Arguments", _as_json(arguments), "json"))
            self._write_tool_row(str(name), arguments, ok=ok, sections=sections)
            return
        self._write_tool_row(str(name), {}, ok=ok, sections=sections, detached=True)

    def _write_tool_row(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        ok: bool | None = None,
        sections: list[tuple[str, str, str]] | None = None,
        detached: bool = False,
    ) -> None:
        """One line for one tool action.

        Titles-only by design: the row says which tool, on what, and whether it
        failed. It does not quote the result - `patch_file` answers with a whole
        diff and `contract_assert_pass` with a paragraph, and a summary that
        pastes those stops being skimmable at about the fourth row. The text is
        one click away in `log-detailed.md`, which is the point of having two
        files."""
        target = _tool_target(arguments)
        verb = _TOOL_VERBS.get(name)
        if verb and target:
            title = f"{verb} `{_clip(target, _MAX_VALUE_CHARS)}`"
        elif target:
            title = f"`{redact(name)}` — `{_clip(target, _MAX_VALUE_CHARS)}`"
        else:
            title = f"`{redact(name)}`"
        if detached:
            title = f"{title} — result"
        self._row(
            ICON_TOOL if ok is not False else ICON_FAIL,
            title,
            kind="tool",
            # Only failure earns a marker. Marking every success would put a
            # word on every row that carries no information, which is how a
            # summary turns back into a wall.
            outcome="" if ok is not False else "FAILED",
            sections=sections,
        )

    def append_decision(self, record: dict[str, Any]) -> None:
        decision = redact(str(record.get("decision", "decision")))
        chosen = redact(str(record.get("chosen_action", "")))
        reason = " ".join(redact(str(record.get("reason_summary", ""))).split())
        title = f"**Decision** `{decision}`"
        if chosen:
            title += f" → `{chosen}`"
        if reason:
            title += f": {_clip(reason, _MAX_TITLE_CHARS)}"
        self._row(ICON_NOTE, title, kind="decision", detail=record if self.verbose else None)

    # -- the model ---------------------------------------------------------

    def append_model_call(
        self, call_id: str, role: str, model: str, request_text: str
    ) -> None:
        """The prompt as sent, at every level.

        It used to be verbose-only, on the reasoning that the prompt is
        reconstructable from the transcript. That was true and it was also not
        the whole picture: `.shamsu/chat-logs/` was keeping a full copy at
        every level anyway, unredacted, and deleting that file for the security
        leak it was would have taken the record with it. So the record moves
        here, where it goes through the redactor like everything else, and the
        overflow rule keeps a 12k prompt out of the document body.
        """
        self._detail_only(
            f"Prompt sent — `{redact(str(call_id))}`",
            [("Prompt", redact(str(request_text or "")), "text")],
            slug="prompt",
        )

    def append_model_reasoning(
        self, call_id: str, role: str, model: str, reasoning: str
    ) -> None:
        """Hold the trace until the response lands.

        Reasoning arrives between the call starting and it finishing, so
        writing it on arrival would put the trace *above* the answer it
        produced. It is buffered and rendered as a sub-panel inside the model's
        own entry instead, which is the one place a reader looks for it."""
        if reasoning:
            self._pending.append(
                {"kind": "reasoning", "call_id": str(call_id or ""), "text": str(reasoning)}
            )

    def append_model_result(
        self,
        call_id: str,
        role: str,
        model: str,
        response: str,
        error: str,
        duration_ms: float | None,
    ) -> None:
        """The model's turn, with its reasoning folded in as a sub-panel."""
        visible, inline_thought = split_reasoning(str(response or ""))
        trace = self._take_reasoning(str(call_id or "")) or inline_thought
        title = "**Model responded**" if not error else "**Model failed**"
        sections: list[tuple[str, str, str]] = []
        if role or model:
            sections.append(
                ("Call", _kv_table({"role": role, "model": model, "call": call_id}), "raw")
            )
        if trace:
            sections.append(
                (f"Reasoning trace — {len(trace):,} chars", redact(trace), "collapsed")
            )
        if error:
            sections.append(("Error", redact(str(error)), "text"))
        elif visible:
            sections.append(("Response", redact(visible), "text"))
        note = _duration(duration_ms)
        if trace and not error:
            note = f"{note} · reasoning captured" if note else "reasoning captured"
        self._row(
            ICON_MODEL if not error else ICON_FAIL,
            title,
            kind="model",
            note=note,
            sections=sections,
        )

    def append_context(self, record: dict[str, Any]) -> None:
        """Building the pack the model was about to see.

        The row appears at both levels - what was retrieved, and how much of the
        window it cost, is part of the story of the turn. The pack ITSELF is
        verbose-only: it is the single largest payload a turn produces and it is
        reconstructable from the transcript."""
        tokens = record.get("token_estimate", "unknown")
        snippets = len(record.get("snippets") or [])
        self._row(
            ICON_NOTE,
            f"**Building context** — {snippets} snippet(s), ~{tokens} tokens",
            kind="context",
            sections=(
                [("Context sent to model", _as_json(record), "json")]
                if self.verbose
                else None
            ),
        )

    def append_notice(self, message: str, level: str = "warning") -> None:
        """Something that happened to the TURN, not a step the agent took.

        "context is filling; eliding older tool payloads" explains why the rows
        after it look different, so it is never collapsed and never a link - it
        is the whole content, and it belongs in both documents."""
        text = " ".join(_clip(redact(str(message or "")), _MAX_TITLE_CHARS).split())
        if not text:
            return
        self._flush_pending()
        line = f"- {ICON_FAIL if level == 'error' else '⚠'} _{text}_\n"
        self._append_summary(line)
        self._append_detailed(line)

    # -- approvals ---------------------------------------------------------

    def append_approval(self, record: dict[str, Any]) -> None:
        """One approval, as its own row: asked and answered together.

        These used to be invisible - the request went to `events.jsonl` and the
        only trace in the report was that a tool call happened a while later, so
        a run that sat four minutes waiting for a human looked identical to one
        that spent four minutes thinking.

        The request and its resolution arrive as two separate events, and
        emitting a row for each says the same thing twice: "waiting", then
        "approved". The request is held until the answer lands, so one row
        carries both, and holding it is also what makes the wait measurable."""
        event_type = str(record.get("type") or record.get("event_type") or "")
        if event_type.endswith("remembered"):
            # A note about future turns, not a decision about this one.
            return
        if event_type.endswith("request"):
            self._flush_pending()
            self._pending.append({"kind": "approval", "record": record})
            return
        pending = self._take_approval()
        if pending:
            # The resolution event carries the decision; the request event
            # carries the preview and the risk. Neither alone is the whole row.
            merged = {**pending, **{k: v for k, v in record.items() if v not in (None, "")}}
            merged["requested_at"] = pending.get("timestamp", "")
            merged["resolved_at"] = record.get("timestamp", "")
            record = merged
        self._write_approval(record)

    def _write_approval(self, record: dict[str, Any]) -> None:
        request = record.get("request") or {}
        if not isinstance(request, dict):
            request = {}
        action = str(record.get("action_type") or request.get("action_type") or "action")
        approved = record.get("approved")
        event_type = str(record.get("type") or record.get("event_type") or "")
        if approved is None:
            if event_type.endswith("granted") or event_type.endswith("auto_approved"):
                approved = True
            elif event_type.endswith("denied"):
                approved = False
        targets = [str(path) for path in (request.get("target_paths") or []) if path]
        on = f" on `{redact(targets[0])}`" if targets else ""
        if approved is None:
            state = "**waiting**"
        elif approved:
            state = "**approved**"
        else:
            state = "**DENIED**"
        source = str(record.get("decision_source") or "")
        scope = str(record.get("decision_scope") or "")
        note = " · ".join(part for part in (source, scope) if part and part != "none")
        sections: list[tuple[str, str, str]] = [
            (
                "Approval",
                _kv_table(
                    {
                        "action": action,
                        "risk": request.get("risk_level", ""),
                        "reason": request.get("reason", "") or request.get("description", ""),
                        "targets": ", ".join(targets),
                        "requested": record.get("requested_at", ""),
                        "resolved": record.get("resolved_at", ""),
                        "decided by": source,
                        "scope": scope,
                    }
                ),
                "raw",
            )
        ]
        preview = str(request.get("preview") or "")
        if preview:
            sections.append(("Preview shown for approval", redact(preview), "diff"))
        self._row(
            ICON_APPROVAL,
            f"**Approval** — `{redact(action)}`{on} — {state}",
            kind="approval",
            note=note,
            surface=str(record.get("decision_surface") or ""),
            sections=sections,
        )

    # -- attempts, retries, and what came of them --------------------------

    def append_mutation(
        self, status: str, files: list[str], error: str, transaction_id: str
    ) -> None:
        """Buffer one write attempt so a retry can be grouped with what it fixed.

        Nothing is written here. Attempts on the same file accumulate in
        `_pending` and are flushed as one group by `_flush_pending`, which runs
        before the next row of any other kind and again at close. A lone
        attempt flushes as an ordinary row, so the common case reads exactly as
        it did before."""
        names = [str(path) for path in (files or [])]
        self._flush_if_different(names)
        self._pending.append(
            {
                "kind": "attempt",
                "status": str(status or ""),
                "files": names,
                "error": str(error or ""),
                "transaction_id": str(transaction_id or ""),
                "kept": str(status or "").lower() in _KEPT_STATUSES,
                "verification": [],
            }
        )

    def append_repair_attempt(self, record: dict[str, Any]) -> None:
        """A repair attempt the harness scored itself.

        Distinct from `append_mutation`: the repair loop knows its own attempt
        index and whether the result was kept, which a bare mutation status
        cannot say."""
        files = [str(path) for path in (record.get("files_changed") or [])]
        self._flush_if_different(files)
        self._pending.append(
            {
                "kind": "attempt",
                "status": str(record.get("outcome") or ""),
                "files": files,
                "error": "" if record.get("kept") else str(record.get("note") or ""),
                "transaction_id": "",
                "kept": bool(record.get("kept")),
                "index": record.get("attempt_index"),
                "verification": [],
                "command": str(record.get("command") or ""),
            }
        )

    def append_verification(
        self,
        passed: bool,
        command: str,
        summary: str,
        exit_code: int | None,
        files: list[str],
    ) -> None:
        """Attach to the open attempt when there is one, else stand alone.

        A verification is the reason an attempt was kept or rolled back, so it
        belongs inside that attempt's panel. Outside a repair it is a row of its
        own - a project check, run because the turn asked for one."""
        exit_text = f" (exit {exit_code})" if exit_code is not None else ""
        text = " ".join(_clip(redact(str(summary or "")), _MAX_MESSAGE_CHARS).split())
        line = f"{'passed' if passed else 'FAILED'}{exit_text}" + (f": {text}" if text else "")
        open_attempt = self._open_attempt()
        if open_attempt is not None:
            open_attempt["verification"].append(
                {"passed": bool(passed), "command": str(command or ""), "line": line}
            )
            return
        target = f" `{redact(str(command))}`" if command else ""
        self._row(
            ICON_PASS if passed else ICON_FAIL,
            f"**Verification**{target} — {line}",
            kind="verify",
            sections=(
                [("Files", "\n".join(f"- `{redact(str(name))}`" for name in files), "raw")]
                if files
                else None
            ),
        )

    def append_command(
        self, command: str, exit_code: int, stdout: str, stderr: str
    ) -> None:
        sections: list[tuple[str, str, str]] = []
        if stdout:
            sections.append(("stdout", redact(str(stdout)), "text"))
        if stderr:
            sections.append(("stderr", redact(str(stderr)), "text"))
        self._row(
            ICON_COMMAND,
            f"`{_clip(redact(str(command)), _MAX_TITLE_CHARS)}`",
            kind="command",
            note=f"exit {exit_code}" if exit_code is not None else "",
            outcome="passed" if exit_code == 0 else "FAILED",
            sections=sections,
        )

    def append_steering(self, message: str, source: str = "") -> None:
        """A message injected mid-run, usually from another surface.

        The badge is the whole point: in one scrollback a phone message and the
        local prompt are the same shape, and only the surface tells you the run
        was steered rather than started."""
        badge = source or self.source
        text = " ".join(_clip(redact(str(message or "")), _MAX_MESSAGE_CHARS).split())
        label = f"**Steering message** `{badge}`" if badge else "**Steering message**"
        self._row(ICON_STEER, f"{label} — {text}", kind="steer")

    # -- writing -----------------------------------------------------------

    def _row(
        self,
        icon: str,
        title: str,
        *,
        kind: str = "step",
        note: str = "",
        outcome: str = "",
        surface: str = "",
        detail: Any = None,
        sections: list[tuple[str, str, str]] | None = None,
    ) -> None:
        """One summary line, plus its detail block when there is anything to show."""
        self._flush_pending()
        sections = [pair for pair in (sections or []) if str(pair[1]).strip()]
        if detail not in (None, {}, [], ""):
            sections.append(("Payload", _as_json(detail), "json"))
        anchor = self._anchor(kind) if sections else ""
        parts = [f"- {icon} {title}"]
        if surface:
            parts.append(f"`{surface}`")
        if outcome:
            parts.append(f"**{outcome}**")
        if note:
            parts.append(note)
        if anchor:
            parts.append(f"[detail]({DETAILED_FILE}#{anchor})")
        self._append_summary(" · ".join(parts) + "\n")
        if sections:
            self._write_detail_block(anchor, f"{icon} {title}", sections)

    def _detail_only(
        self,
        title: str,
        sections: list[tuple[str, str, str]],
        slug: str = "detail",
    ) -> None:
        """A payload with no row of its own - it belongs to the row above it."""
        self._write_detail_block(self._anchor(slug), title, sections)

    def _write_detail_block(
        self, anchor: str, title: str, sections: list[tuple[str, str, str]]
    ) -> None:
        lines = [f'<a id="{anchor}"></a>', "", f"### {title}", ""]
        for heading, body, style in sections:
            if not str(body).strip():
                continue
            lines.append(self._render_section(heading, str(body), style))
        lines.append(f"[↑ summary]({SUMMARY_FILE})")
        lines.append("")
        self._append_detailed("\n".join(lines) + "\n")

    def _render_section(self, heading: str, body: str, style: str) -> str:
        """One labelled payload, spilled to `attachments/` when it is too big."""
        link = self._spill_if_large(body, _slug(heading), _extension(style))
        if link:
            return f"**{heading}**\n\n{link}\n"
        if style == "raw":
            return f"**{heading}**\n\n{body}\n"
        if style == "collapsed":
            return (
                f"<details>\n<summary>{heading}</summary>\n\n"
                f"```text\n{_fence_safe(body)}\n```\n\n</details>\n"
            )
        language = {"json": "json", "diff": "diff"}.get(style, "text")
        return f"**{heading}**\n\n```{language}\n{_fence_safe(body)}\n```\n"

    def _spill_if_large(self, body: str, slug: str, extension: str) -> str:
        """Write *body* to `attachments/` and return a link, or "" to inline it.

        The threshold is on the rendered text, not on line count, because what
        makes a document unreadable is its size on screen - a 900-line log and
        a single 40 KB JSON blob are the same problem."""
        if len(body) <= OVERFLOW_CHARS:
            return ""
        name = f"{_safe_name(self.turn_id)}-{self._anchor_seq():04d}-{slug}.{extension}"
        try:
            self.attachments_dir.mkdir(parents=True, exist_ok=True)
            (self.attachments_dir / name).write_text(body, encoding="utf-8")
        except OSError:
            # Cannot spill, so inline a clipped copy rather than losing the row.
            return f"```text\n{_fence_safe(body[:OVERFLOW_CHARS])}\n... [clipped]\n```"
        lines = body.count("\n") + 1
        head = _fence_safe("\n".join(body.splitlines()[:8]))
        return (
            f"```text\n{head}\n```\n\n"
            f"_{lines:,} lines, {len(body):,} chars — over the inline threshold._ "
            f"[{ATTACHMENTS_DIR}/{name}]({ATTACHMENTS_DIR}/{name})\n"
        )

    # -- retry grouping ----------------------------------------------------

    def _open_attempt(self) -> dict[str, Any] | None:
        for item in reversed(self._pending):
            if item.get("kind") == "attempt":
                return item
        return None

    def _take_tool(self, name: str) -> dict[str, Any] | None:
        """Pull out the buffered call this result belongs to, if it is still
        waiting. Matched by name: a nested tool can finish between them."""
        for position, item in enumerate(self._pending):
            if item.get("kind") == "tool" and item.get("name") == name:
                return self._pending.pop(position)
        return None

    def _take_approval(self) -> dict[str, Any]:
        """Pull out the approval request still waiting for an answer."""
        for position, item in enumerate(self._pending):
            if item.get("kind") == "approval":
                return dict(self._pending.pop(position).get("record") or {})
        return {}

    def _flush_if_different(self, files: list[str]) -> None:
        """Flush the buffer when a write lands on a different file.

        Attempts group by target: two tries at `config.py` are a retry, a try at
        `config.py` then one at `views.py` is two pieces of work."""
        open_attempt = self._open_attempt()
        if open_attempt is not None and set(open_attempt.get("files") or []) != set(files):
            self._flush_pending()

    def _flush_pending(self) -> None:
        if not self._pending:
            return
        buffered, self._pending = self._pending, []
        attempts = [item for item in buffered if item.get("kind") == "attempt"]
        if not attempts:
            for item in buffered:
                # An approval nobody answered - the turn ended, or the process
                # died while a human was still deciding. "waiting" is the
                # truthful row, and it is the one worth keeping.
                if item.get("kind") == "approval":
                    self._write_approval(dict(item.get("record") or {}))
                # Reasoning nobody claimed - the call failed before it finished,
                # or a bare completion with no ledger call id. Keep it; a
                # dropped trace is the thing this log exists to stop losing.
                # A call whose result never arrived, or one an approval
                # interrupted. Either way the call happened and the row is true.
                elif item.get("kind") == "tool":
                    arguments = item.get("arguments") or {}
                    self._write_tool_row(
                        str(item.get("name") or ""),
                        arguments,
                        sections=(
                            [("Arguments", _as_json(arguments), "json")]
                            if arguments
                            else None
                        ),
                    )
                elif item.get("kind") == "reasoning" and item.get("text"):
                    self._detail_only(
                        f"Reasoning trace — `{item.get('call_id') or 'unattributed'}`",
                        [("Reasoning", redact(str(item["text"])), "collapsed")],
                        slug="reasoning",
                    )
            return
        if len(attempts) == 1:
            self._write_attempt_row(attempts[0])
            return
        self._write_attempt_group(attempts)

    def _write_attempt_row(self, attempt: dict[str, Any]) -> None:
        rendered = _rendered_files(attempt.get("files") or [])
        status = str(attempt.get("status") or "")
        self._row(
            ICON_PATCH,
            f"**Files changed** — {rendered}",
            kind="patch",
            outcome=status or ("applied" if attempt.get("kept") else ""),
            sections=self._attempt_sections(attempt),
        )

    def _write_attempt_group(self, attempts: list[dict[str, Any]]) -> None:
        """Render consecutive attempts on one file as one visual group.

        `_row` flushes the buffer first, so this cannot call it - it writes the
        header itself and then the attempts underneath, indented."""
        rendered = _rendered_files(attempts[0].get("files") or [])
        kept = sum(1 for attempt in attempts if attempt.get("kept"))
        self._append_summary(
            f"- {ICON_RETRY} **Write attempts** — {rendered} "
            f"· _{kept} of {len(attempts)} kept_\n"
        )
        for position, attempt in enumerate(attempts, start=1):
            index = attempt.get("index")
            label = f"Attempt {index if index is not None else position}"
            status = str(attempt.get("status") or "")
            if attempt.get("kept"):
                title = f"{ICON_PATCH} {label} · **{status or 'applied'}**"
            else:
                title = f"{ICON_PATCH} ~~{label}~~ · {status or 'superseded'}"
            sections = self._attempt_sections(attempt)
            anchor = self._anchor("attempt") if sections else ""
            line = f"    - {title}"
            if anchor:
                line += f" · [detail]({DETAILED_FILE}#{anchor})"
            self._append_summary(line + "\n")
            if sections:
                self._write_detail_block(
                    anchor, f"{ICON_PATCH} {label} — {rendered}", sections
                )

    def _attempt_sections(self, attempt: dict[str, Any]) -> list[tuple[str, str, str]]:
        sections: list[tuple[str, str, str]] = []
        facts = _kv_table(
            {
                "status": attempt.get("status", ""),
                "kept": "yes" if attempt.get("kept") else "no",
                "files": ", ".join(str(path) for path in (attempt.get("files") or [])),
                "transaction": attempt.get("transaction_id", ""),
                "command": attempt.get("command", ""),
            }
        )
        if facts:
            sections.append(("Attempt", facts, "raw"))
        checks = attempt.get("verification") or []
        if checks:
            body = "\n".join(
                (
                    f"- {'PASS' if check['passed'] else 'FAIL'} "
                    f"`{redact(str(check['command']))}` — {check['line']}"
                    if check.get("command")
                    else f"- {check['line']}"
                )
                for check in checks
            )
            sections.append(("Verification", body, "raw"))
        if attempt.get("error"):
            sections.append(("Error", redact(str(attempt["error"])), "text"))
        return sections

    # -- reasoning buffer --------------------------------------------------

    def _take_reasoning(self, call_id: str) -> str:
        """Pull this call's buffered trace out, leaving anything else alone."""
        for position, item in enumerate(self._pending):
            if item.get("kind") != "reasoning":
                continue
            if call_id and item.get("call_id") and item["call_id"] != call_id:
                continue
            return str(self._pending.pop(position).get("text") or "")
        return ""

    # -- io (best-effort throughout) ---------------------------------------

    def _ensure_headers(self) -> None:
        self._ensure(
            self.summary_path,
            "# Session log — summary\n\n"
            "Every action in order, one line each. Follow a `detail` link for the "
            f"payload; the same sequence with everything attached is in "
            f"[`{DETAILED_FILE}`]({DETAILED_FILE}).\n\n",
        )
        self._ensure(
            self.detailed_path,
            "# Session log — detail\n\n"
            f"The same actions as [`{SUMMARY_FILE}`]({SUMMARY_FILE}), with prompts, "
            "diffs, output, and reasoning traces. Anything too large to inline is in "
            f"`{ATTACHMENTS_DIR}/`.\n\n",
        )

    def _ensure(self, path: Path, header: str) -> None:
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text(header, encoding="utf-8")
        except OSError:
            pass

    def _append_summary(self, text: str) -> None:
        self._write(self.summary_path, text)

    def _append_detailed(self, text: str) -> None:
        self._write(self.detailed_path, text)

    def _write(self, path: Path, text: str) -> None:
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                self._ensure_headers()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError:
            pass

    def _is_closed(self) -> bool:
        """True when this turn already wrote its result block.

        Scoped to this turn's own heading: the summary accumulates every turn of
        the session, so an earlier turn's marker must not make a later one look
        already closed."""
        try:
            text = self.summary_path.read_text(encoding="utf-8")
        except OSError:
            return False
        start = text.rfind(f"`{self.turn_id}`")
        return _CLOSED_MARKER in (text[start:] if start >= 0 else text)

    def _anchor(self, kind: str) -> str:
        return f"doc-{_safe_name(self.turn_id)}-{_slug(kind)}-{self._anchor_seq():04d}"

    def _anchor_seq(self) -> int:
        """Next free sequence number, counted once from the file then carried.

        It has to start from the file: a session spans many turns and many
        processes, and a counter that started at 1 each time would write the
        same anchor id twice into one document - which is a summary link that
        jumps to the wrong row.

        It must not KEEP reading the file. `log-detailed.md` grows all session,
        and re-counting it per row is quadratic in the session's own length.
        This writer is the only thing appending to it, so counting once and
        incrementing is exact."""
        if self._anchor_count is None:
            try:
                text = self.detailed_path.read_text(encoding="utf-8")
            except OSError:
                text = ""
            self._anchor_count = text.count('<a id="doc-')
        self._anchor_count += 1
        return self._anchor_count


_LAYOUT_README = """# .shamsu/

SHAMSU's workspace-local state and logs. Safe to delete: nothing here is needed
to run SHAMSU again, and none of it is ever fed back into a model prompt.

## Start here

    sessions/<session-id>/log-summary.md    every action, one line each
    sessions/<session-id>/log-detailed.md   the same actions, with the payloads
    sessions/<session-id>/attachments/      anything too big to inline

Two files per conversation, appended as it runs. `log-summary.md` is what you
skim: the prompt, then one row per model turn, tool call, approval, write, and
check, in the order they happened. Each row that has more to show links into
`log-detailed.md` at an anchor, where the same step carries its prompt, diff,
output, or reasoning trace. A tool result too large to inline is written to
`attachments/` and linked from there.

They replaced `report.md` and the eight typed folders under `.evidence/`
(`prompts/`, `reasoning/`, `responses/`, `tool-results/`, `commands/`,
`contexts/`, `diagnostics/`, `mutations/`), which held one story in eight piles
sorted by payload type - the one key nobody searches on.

## One-file debug log

    logs/<session-id>/agent-development-log.jsonl

The first file to open when debugging SHAMSU itself. Prompt history, chat
messages, session events, run evidence, tool and model call metadata, and in
verbose mode the redacted prompt, reasoning, and response text.

## Machine evidence

    runs/<run-id>/.evidence/
      events.jsonl  decisions.jsonl  tool-calls.jsonl  model-calls.jsonl
      manifest.json  summary.json  final-output.md
      attachments/    every spilled payload, flat, one folder

Structured records for tooling, not for reading. Run ids sort chronologically,
so the newest run is the last folder.

## Reading it from the REPL

    /logs              where everything is, and the current detail level
    /run report        the last run's rows from log-summary.md
    /runs              list recent runs
    /run export <id>   zip one run up to share

## Detail level

`essential` is the default: every action, with previews rather than full
per-model prompt, reasoning, response, and context payloads. `verbose` adds
those, plus tool payloads, command output, and context details.

Change it with `/logs mode essential|verbose` or `SHAMSU_LOG_LEVEL`.
The legacy names `compact` and `full` remain accepted as aliases. Runs older
than `retention_days` (30) are removed by `/run clean`.

## Secrets

Everything is redacted on the way to disk through a single shared
secret-pattern list. Redaction is pattern-based, so treat these files as
sensitive anyway - they contain your prompts and your source code.

## Other folders

    audit/        a separate per-session event trail (see /audit-log)
    plans/        saved plans from `plan <task>`
    action_ledger/config.json   logging configuration
"""


#: Written by `shamsu/agents/simple_log.py` until 2026-08-21. Deleted, because
#: it was the one path in the project that put a prompt on disk without going
#: through the redactor - see `legacy_chat_logs`.
LEGACY_CHAT_LOGS = "chat-logs"


def legacy_chat_logs(workspace: Path) -> Path | None:
    """The old unredacted transcript folder, if this workspace still has one.

    `.shamsu/chat-logs/` held the exact prompt and the raw reply for every turn
    of every session, and `simple_log.py` contained no calls to `redact` at
    all. Every other path that writes text to disk goes through one shared
    secret-pattern list; that one did not, so any key, token or password a
    prompt mentioned is sitting in there in clear text.

    SHAMSU does not delete it. The files may be the only record of sessions
    someone still wants, and silently removing a user's logs to fix our bug is
    not ours to decide - so the workspace is told, and the choice stays theirs.
    """
    directory = Path(workspace) / ".shamsu" / LEGACY_CHAT_LOGS
    try:
        if directory.is_dir() and any(directory.iterdir()):
            return directory
    except OSError:
        return None
    return None


def legacy_chat_logs_warning(workspace: Path) -> str:
    """One paragraph for the console, or "" when there is nothing to say."""
    directory = legacy_chat_logs(workspace)
    if directory is None:
        return ""
    try:
        count = len([path for path in directory.glob("*.md") if path.is_file()])
    except OSError:
        count = 0
    files = f"{count} file{'s' if count != 1 else ''}" if count else "files"
    return (
        f"Old chat logs found: {directory}\n"
        f"These {files} were written before 2026-08-21 by a logger that did NOT "
        "redact secrets, so any API key or password mentioned in a prompt is in "
        "them in clear text. Nothing writes there any more - the readable log "
        "now lives in .shamsu/sessions/<id>/ and is redacted. Delete the folder "
        "when you no longer need the history."
    )


def write_layout_readme(workspace: Path) -> Path | None:
    """Explain the .shamsu folder, in the folder itself.

    Someone who opens `.shamsu/` in a file browser should not have to guess what
    the logs are. Written once and then left alone, so a user's own edits
    survive. Best-effort; returns the path, or None."""
    root = Path(workspace).resolve() / ".shamsu"
    path = root / "README.md"
    try:
        if path.exists():
            return path
        root.mkdir(parents=True, exist_ok=True)
        path.write_text(_LAYOUT_README, encoding="utf-8")
        return path
    except OSError:
        return None


# -- one writer per turn ---------------------------------------------------

#: Live writers, keyed by the turn they are writing. Bounded: a long REPL
#: session runs hundreds of turns and none of the finished ones are ever
#: written to again.
_WRITERS: dict[tuple[str, str], TurnLogWriter] = {}
_MAX_LIVE_WRITERS = 8


def writer_for(
    run_dir: Path,
    session_dir: Path | None = None,
    run_id: str = "",
    log_level: str = "essential",
    turn_id: str = "",
    source: str = "",
) -> TurnLogWriter:
    """The writer for this turn, reused across calls.

    The ActionLedger mirrors each step by building a writer, calling one method
    and dropping it, which is the right shape for a stateless appender and the
    wrong one for these documents: a trace buffered on the way in would be
    discarded before the response it belongs to arrived, and every retry would
    be an unrelated row again. Keyed by turn rather than by session so two turns
    cannot interleave their pending attempts into one group.

    Evicting flushes, so a writer that ages out still emits what it was holding
    rather than swallowing it."""
    writer = TurnLogWriter(
        run_dir,
        session_dir,
        run_id=run_id,
        log_level=log_level,
        turn_id=turn_id,
        source=source,
    )
    key = (str(writer.home), writer.turn_id)
    live = _WRITERS.get(key)
    if live is not None:
        # Keep the buffer, take the fresh call's settings: log level and source
        # can both change between turns of one session.
        live.log_level = writer.log_level
        live.verbose = writer.verbose
        live.source = writer.source or live.source
        return live
    while len(_WRITERS) >= _MAX_LIVE_WRITERS:
        _, evicted = _WRITERS.popitem()
        evicted._flush_pending()
    _WRITERS[key] = writer
    return writer


def release_writer(writer: TurnLogWriter) -> None:
    """Drop a finished turn's writer after flushing anything still buffered."""
    writer._flush_pending()
    _WRITERS.pop((str(writer.home), writer.turn_id), None)


# -- module helpers --------------------------------------------------------


def split_reasoning(text: str) -> tuple[str, str]:
    """Return ``(visible, reasoning)`` by pulling thought blocks out of *text*.

    Models that are not asked for structured reasoning write it into the answer,
    and the answer is what a reader sees first. Separating them here means the
    trace lands in its own panel whichever way the model returned it."""
    if not text:
        return ("", "")
    traces: list[str] = []

    def _capture(match: re.Match[str]) -> str:
        traces.append(match.group("body").strip())
        return ""

    visible = _THOUGHT_RE.sub(_capture, text)
    dangling = _DANGLING_THOUGHT_RE.search(visible)
    if dangling is not None:
        traces.append(dangling.group("body").strip())
        visible = visible[: dangling.start()]
    return (visible.strip(), "\n\n".join(trace for trace in traces if trace))


def _rendered_files(files: list[str]) -> str:
    return ", ".join(f"`{redact(str(path))}`" for path in files) or "none"


def _tool_target(arguments: dict[str, Any] | None) -> str:
    for key in _TOOL_ARG_KEYS:
        value = (arguments or {}).get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(redact(value).split())
    return ""


def _kv_table(fields: dict[str, Any]) -> str:
    rows = [
        f"| {key} | {' '.join(redact(str(value)).split())} |"
        for key, value in fields.items()
        if str(value or "").strip()
    ]
    if not rows:
        return ""
    return "\n".join(["| | |", "| --- | --- |", *rows])


def _as_json(value: Any) -> str:
    try:
        return redact(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return redact(str(value))


def _blockquote(text: str) -> str:
    return "\n".join(f"> {line}" if line else ">" for line in (text or "").splitlines())


def _clip(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [clipped {len(text) - limit} chars]"


def _fence_safe(text: str) -> str:
    """Keep a payload's own fences from ending the block that contains it."""
    return (text or "").replace("```", "'''")


def _duration(duration_ms: float | None) -> str:
    if not isinstance(duration_ms, (int, float)):
        return ""
    if duration_ms < 1000:
        return f"{duration_ms:.0f}ms"
    if duration_ms < 60_000:
        seconds = duration_ms / 1000
        return f"{seconds:.0f}s" if seconds >= 10 else f"{seconds:.1f}s"
    minutes, seconds = divmod(duration_ms / 1000, 60)
    return f"{minutes:.0f}m {seconds:.0f}s"


def _verdict_icon(status: str) -> str:
    normalized = (status or "").strip().lower()
    if normalized.startswith("success") or normalized in {"complete", "completed", "ok"}:
        return ICON_PASS
    if normalized in {"partial", "needs_input", "dry_run", "cancelled"}:
        return ICON_NOTE
    return ICON_FAIL


def _extension(style: str) -> str:
    return {"json": "json", "diff": "diff", "raw": "md"}.get(style, "txt")


def _slug(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return cleaned[:32] or "item"


def _safe_name(text: str) -> str:
    return (
        "".join(char for char in (text or "") if char.isalnum() or char in {"-", "_"})
        or "turn"
    )
