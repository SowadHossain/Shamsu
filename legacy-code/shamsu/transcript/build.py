"""Drive a build as a conversation: plan, implement, next, next, next.

This is the loop a person runs by hand in ``ollama run`` — ask for a plan, say
"implement", then say "next" until the project exists — with a file-writer and a
verifier attached. The measured 2026-08-16 PRD run spent 610 of its first 611
seconds before the first model call, wrote 18 template stubs the model never
asked for, re-planned three times through a JSON schema, and completed zero
milestones in 14 minutes. Nothing here does any of that.

What it keeps from the existing harness: the tool registry (so the write gate,
path sandbox, approval layer and ledger all still apply), the model-output
salvage in ``shamsu/llm/output.py``, and ``verify_only`` from the verify gate.
What it drops: the rebuilt state frame, the phase machine, one-action-per-call,
boilerplate pre-writes and the JSON planner.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from shamsu.llm.output import parse_model_turn, strip_thinking
from shamsu.transcript.blocks import normalize_file_headers
from shamsu.transcript.session import Transcript
from shamsu.verify.gate import verify_only

# Tools reachable through the prose channel. Everything else stays unavailable
# here on purpose: this loop's unit of work is "write the files for a milestone",
# and a model that can shell out mid-milestone starts debugging instead of
# building. Verification is the loop's job, run between turns, not the model's.
WRITE_TOOLS = ("write_file", "append_file", "edit_file")

MAX_REPAIRS_PER_SLICE = 2
MAX_SLICES = 24

# Phrases a model uses when it believes the build is finished. Checked only when
# a turn also wrote nothing — text alone never ends a run, or a chatty summary
# after a successful milestone would stop the build one milestone early.
_DONE_MARKERS = (
    "all milestones complete",
    "all milestones",
    "project is complete",
    "build is complete",
    "implementation is complete",
    "nothing left to implement",
    "no further milestones",
    "no remaining milestones",
)


def build_system_prompt(workspace_name: str) -> str:
    """The frozen system message.

    Everything here is true for the whole session. Anything that varies per turn
    belongs in the newest user message — putting it here would move the cache
    boundary to token zero and cost a full re-prefill on every call, which is the
    failure this route exists to remove.
    """
    return f"""You are a senior engineer building a project in the workspace `{workspace_name}`.

You work the way a developer works in a chat: you keep your own plan in mind, you
build one milestone at a time, and you continue from what you already wrote.

To create or replace a file, write a `# write_file:` header on its own line and
put the complete file in the fenced block directly below it:

# write_file: relative/path/to/file.py
```python
<the complete file content>
```

Rules for file blocks:
- The path is relative to the workspace root. Never absolute, never `..`.
- Write the COMPLETE file. No `...`, no "rest unchanged", no placeholder bodies.
- One block per file. Emit as many blocks as the milestone needs.
- For .md/.markdown targets use four backticks, so the file's own fences do not
  close the block early.

Between milestones you will be told "next", or given an error to fix. When you
are given an error, fix it by rewriting the affected file(s) in full.

Do not ask permission to continue. Do not summarise what you are about to do
instead of doing it. Write the files."""


PLAN_INSTRUCTION = """Read the requirements below and write a build plan in markdown.

Break the work into milestones. For each milestone give it a number, a short
title, and the list of files it creates. Order them so each milestone only
depends on earlier ones. Keep it concrete and buildable.

Write the plan only — do not write any code yet.

--- REQUIREMENTS ---
{request}"""


@dataclass
class SliceOutcome:
    """One milestone turn: what it wrote, whether it verified, what it cost."""

    index: int
    instruction: str
    files_written: list[str] = field(default_factory=list)
    verify_status: str = "skipped"
    verify_error: str = ""
    repairs: int = 0
    model_calls: int = 0
    duration_s: float = 0.0
    cached_prefix_tokens: int = 0
    prompt_tokens: int = 0
    done_signal: bool = False

    @property
    def ok(self) -> bool:
        return self.verify_status in {"passed", "unverifiable", "skipped"}


@dataclass
class BuildReport:
    plan: str = ""
    slices: list[SliceOutcome] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    completed: bool = False
    stopped_reason: str = ""
    duration_s: float = 0.0
    declared_milestones: int = 0
    # Every call including planning. Summing the slices would silently omit the
    # planner, which is exactly the accounting that let the old route re-plan
    # three times in one run without it showing up anywhere.
    model_calls: int = 0

    @property
    def cache_reuse_ratio(self) -> float:
        """Share of prompt tokens served from an already-cached prefix.

        The headline number for this route. A rebuilt state frame scores ~0.0
        because its prefix changes at token one; an append-only transcript should
        sit high and climb as the conversation grows.
        """
        total = sum(s.prompt_tokens for s in self.slices)
        if total <= 0:
            return 0.0
        return sum(s.cached_prefix_tokens for s in self.slices) / total


class TranscriptBuilder:
    """Runs a build as one growing conversation."""

    def __init__(
        self,
        workspace: Path | str,
        registry: Any,
        llm: Any,
        model: str,
        *,
        max_tokens: int = 32768,
        on_progress: Callable[[str], None] | None = None,
        temperature: float = 0.2,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.registry = registry
        self.llm = llm
        self.model = model
        self.max_tokens = int(max_tokens)
        self.on_progress = on_progress
        self.temperature = float(temperature)
        self.transcript = Transcript(
            build_system_prompt(self.workspace.name), max_tokens=self.max_tokens
        )
        self._last_payload: list[dict[str, Any]] = []
        self._all_files: list[str] = []
        self._model_calls = 0

    # -- plumbing ------------------------------------------------------------

    def _progress(self, message: str) -> None:
        if self.on_progress is not None:
            self.on_progress(message)

    async def _call_model(self, outcome: SliceOutcome | None = None) -> str:
        """Send the transcript, append the answer verbatim, return it.

        Verbatim matters: the fenced code the model just wrote has to be in the
        context for the next turn, because "continue what you wrote" is the whole
        mechanism. Only the ``<think>`` span is stripped — the next turn would
        otherwise re-read and pay for reasoning it has already acted on.
        """
        if self.transcript.needs_compaction():
            dropped = self.transcript.compact()
            if dropped:
                self._progress(f"compacted transcript (dropped {dropped} turns)")
        payload = self.transcript.messages()
        shared = self.transcript.shared_prefix_tokens(self._last_payload)
        prompt_tokens = self.transcript.token_estimate()
        response = await self.llm.chat_with_tools(
            model=self.model,
            messages=payload,
            tools=None,
            temperature=self.temperature,
            num_ctx=self.max_tokens,
            role="transcript-builder",
            workflow_id="transcript.build",
        )
        self._last_payload = payload
        self._model_calls += 1
        message = response.get("message") if isinstance(response, dict) else None
        raw = str((message or {}).get("content") or "")
        answer = strip_thinking(raw).strip()
        self.transcript.append_assistant(answer)
        if outcome is not None:
            outcome.model_calls += 1
            outcome.cached_prefix_tokens += shared
            outcome.prompt_tokens += prompt_tokens
        return answer

    def _apply_writes(self, answer: str) -> tuple[list[str], list[str]]:
        """Execute the file blocks in *answer*. Returns (written, errors)."""
        turn = parse_model_turn(
            {"message": {"content": normalize_file_headers(answer)}}, WRITE_TOOLS
        )
        written: list[str] = []
        errors: list[str] = []
        for call in turn.tool_calls:
            path = str(call.arguments.get("filepath") or call.arguments.get("path") or "")
            result = self.registry.execute(call.name, dict(call.arguments))
            if result.ok:
                if path and path not in written:
                    written.append(path)
            else:
                errors.append(f"{path or call.name}: {result.message}")
        for failure in turn.parse_failures:
            # A malformed block is not "the model wrote prose" — telling it so is
            # how a correct call got discarded three times in a row on 2026-08-03.
            detail = failure.error or failure.kind
            errors.append(f"{failure.path or failure.tool}: could not parse block ({detail})")
        return written, errors

    async def _verify(self, files: list[str]) -> tuple[str, str]:
        """Run the verify gate off the event loop. Returns (status, error)."""
        if not files:
            return "skipped", ""
        try:
            outcome = await asyncio.to_thread(
                verify_only, self.workspace, list(files), lightweight=True
            )
        except Exception as exc:  # a broken verifier must not end the build
            return "unverifiable", f"{type(exc).__name__}: {exc}"
        # status() is a method on VerifyOutcome, not a property.
        status = outcome.status()
        if status == "verified":
            return "passed", ""
        if status == "unverifiable":
            return "unverifiable", ""
        detail = outcome.summary.strip()
        if not detail:
            detail = f"`{outcome.command}` exited {outcome.exit_code}"
        return "failed", detail

    # -- the loop ------------------------------------------------------------

    async def plan(self, request: str) -> str:
        """One model call. Prose in, prose out, kept verbatim and pinned.

        No JSON schema. The 8/16 run put the same model through a nested
        milestones schema three times (~78s) and the call still came back
        ``failed``; asked for markdown in a chat it produces a usable plan first
        try. The answer is pinned because every later "next" continues it.
        """
        self._progress("planning")
        self.transcript.append_user(PLAN_INSTRUCTION.format(request=request.strip()))
        answer = await self._call_model()
        self.transcript.pin_last()
        return answer

    async def implement_slice(self, index: int, instruction: str) -> SliceOutcome:
        """One milestone: write the files, verify, repair in place on failure."""
        started = time.perf_counter()
        outcome = SliceOutcome(index=index, instruction=instruction)
        self.transcript.append_user(instruction)
        answer = await self._call_model(outcome)
        written, errors = self._apply_writes(answer)
        outcome.files_written = list(written)

        if not written and not errors:
            outcome.done_signal = _looks_done(answer)
            outcome.duration_s = time.perf_counter() - started
            return outcome

        status, error = ("failed", "\n".join(errors)) if errors else await self._verify(written)

        while status == "failed" and outcome.repairs < MAX_REPAIRS_PER_SLICE:
            outcome.repairs += 1
            self._progress(f"repair {outcome.repairs} for milestone {index}")
            # The feedback is a plain user turn holding the verbatim error — the
            # thing that worked by hand. It appends, so the cache survives it.
            self.transcript.append_tool_result(
                f"That failed. Fix it by rewriting the affected file(s) in full.\n\n"
                f"--- ERROR ---\n{error.strip()[:4000]}"
            )
            answer = await self._call_model(outcome)
            repaired, repair_errors = self._apply_writes(answer)
            for path in repaired:
                if path not in written:
                    written.append(path)
            outcome.files_written = list(written)
            if repair_errors:
                status, error = "failed", "\n".join(repair_errors)
                continue
            if not repaired:
                break
            status, error = await self._verify(written)

        outcome.verify_status = status
        outcome.verify_error = error
        outcome.duration_s = time.perf_counter() - started
        return outcome

    async def run(
        self,
        request: str,
        *,
        max_slices: int = MAX_SLICES,
        plan_text: str | None = None,
    ) -> BuildReport:
        """Plan, then implement/next until the model stops producing files."""
        started = time.perf_counter()
        report = BuildReport()
        if plan_text is None:
            report.plan = await self.plan(request)
        else:
            # A plan the user already reviewed and approved. Seeded as a normal
            # exchange so the model reads it as its own prior answer — which is
            # what makes "next" continue it.
            self.transcript.append_user(PLAN_INSTRUCTION.format(request=request.strip()))
            self.transcript.append_assistant(plan_text, pinned=True)
            report.plan = plan_text

        # The plan enumerates its own milestones, so the loop length is a fact to
        # read rather than a judgement to ask for. `max_slices` stays a hard
        # ceiling over whatever the plan claims.
        declared = count_milestones(report.plan)
        report.declared_milestones = declared
        total = min(max_slices, declared) if declared else max_slices

        for index in range(1, total + 1):
            # One job per message. The milestone number is named every time
            # rather than sending a bare "next" (which returned prose and no
            # files on the 2026-08-17 run), and the "are you finished?" question
            # is NOT folded in with it: asked both at once on milestone 1, the
            # same model answered ALL MILESTONES COMPLETE before writing a single
            # line. A small model offered an exit will take it.
            instruction = (
                f"Implement milestone {index} from your plan now. "
                "Write the complete files for it."
            )
            self._progress(f"milestone {index} of {total}")
            outcome = await self.implement_slice(index, instruction)
            report.slices.append(outcome)
            for path in outcome.files_written:
                if path not in self._all_files:
                    self._all_files.append(path)

            if outcome.done_signal:
                report.completed = True
                report.stopped_reason = "model reported the build complete"
                break
            if outcome.verify_status == "failed":
                report.stopped_reason = (
                    f"milestone {index} still failing after {outcome.repairs} repair(s)"
                )
                break
            if not outcome.files_written:
                # Not a failure by itself: a milestone whose work an earlier turn
                # already covered legitimately writes nothing. Only a second
                # empty turn in a row means the model has actually stopped
                # building, and ending on the first one truncated a 5-milestone
                # plan at milestone 2 on 2026-08-17.
                if report.slices[-2:-1] and not report.slices[-2].files_written:
                    report.stopped_reason = (
                        "two milestones in a row produced no files — stopping"
                    )
                    break
        else:
            # "Completed" means every milestone the plan DECLARED was attempted.
            # Comparing against `total` instead would call a run capped at 2 of 8
            # a complete build.
            report.completed = bool(declared) and total >= declared
            report.stopped_reason = (
                f"implemented all {declared} planned milestone(s)"
                if report.completed
                else f"reached the {total}-milestone cap"
            )

        report.files = list(self._all_files)
        report.model_calls = self._model_calls
        report.duration_s = time.perf_counter() - started
        return report


def _looks_done(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in _DONE_MARKERS)


# "## Milestone 3:", "### Milestone 3 -", "3. Milestone:" and friends. Counting
# the plan is deterministic work, so the harness does it rather than asking the
# model "are you done yet?" — a question a 7B answers yes to far too eagerly.
_MILESTONE_HEADING_RE = re.compile(
    r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?[ \t]*milestone[ \t]*#?(?P<number>\d+)\b",
    re.IGNORECASE | re.MULTILINE,
)


def count_milestones(plan: str) -> int:
    """How many distinct milestones the plan declares. 0 when it says nothing."""
    numbers = {int(m.group("number")) for m in _MILESTONE_HEADING_RE.finditer(plan or "")}
    if not numbers:
        return 0
    # The count, not the maximum: a plan that skips or restarts numbering should
    # not drive the loop past what it actually described.
    return len(numbers)
