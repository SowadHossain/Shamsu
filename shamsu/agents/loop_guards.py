"""Degenerate-behaviour detectors, as objects rather than inline branches.

Adapted from smallcode `src/governor/early_stop.js` and
`src/governor/quality_monitor.js`.

**Why a module.** Simple mode already had eight of these - prose nudges, promise
nudges, empty-turn nudges, the contract nudge, truncation refusals, the
unproductive-edit ceiling, repeated-read warnings, the per-file edit ceiling -
every one written inline in a `_run_turn` that is now past 2,600 lines. They
work. None of them can be tested without standing up a whole loop, and each new
one makes that function longer. smallcode's real structural advantage is not any
single detector; it is that each lives in a small object with a
``record() -> signal | None`` shape and its own tests.

The detectors here are the ones simple mode did NOT have. The existing eight
stay where they are for now: moving working, tested code is pure risk with no
behaviour to show for it, and it can follow once something needs to change in
them anyway.

**Almost every signal is a nudge, never a stop.** These detect a model that has
lost the thread, not one that has failed - and this project's rule is that a
guard the model cannot get past is a deadlock waiting for a user to notice. The
loop owns stopping, and still does: the one signal that ends a turn
(``READ_LOOP_EXHAUSTED``) is returned like any other and the loop decides.

That exception was bought with evidence. "Nudge, never stop" combined with
one-shot flags meant that past the second nudge NOTHING was counting, so a
model that ignored both could read forever - and one did, for 21m52s, changing
no file and then reporting success. A nudge that cannot escalate is not a
gentler guard than a ceiling; it is no guard at all after the second sentence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Reads with nothing produced before a gentle word, and before a firm one.
# smallcode's numbers (`early_stop.js`: soft at 5, hard at 8) and they are sound
# - five reads is a model gathering context, eight is a model that has forgotten
# it was asked for something.
READS_BEFORE_NUDGE = 5
READS_BEFORE_INSISTING = 8

#: Whether the reads were all of DIFFERENT things changes what to say, not when
#: to say it.
#:
#: Live 2026-08-24, `demo-3/asteroid`: the defect spanned seven source files -
#: `initGame()` never called in one, no default export in two more, a dead
#: `let scene;` in four - and this guard interrupted at five reads, 17 times
#: across the session, with "You probably have enough ... do not keep reading."
#: It did not have enough; it had not yet opened every file the bug lived in.
#: And it obeyed: every fix it shipped that session was scoped to whichever
#: file it happened to have read by the time it was told to stop.
#:
#: Raising the ceiling was the wrong correction - nine different files read to
#: no purpose is exactly the open-ended "review X" this detector exists for, and
#: no count separates that from the eight files above. What was wrong was the
#: INSTRUCTION. A model circling one file should stop reading. A model opening
#: files it has never seen should be asked what it is looking for, not told it
#: already has the answer.

#: Reads AFTER the firm word before the loop is told to end the turn.
#: The detector used to fire twice per turn and then go permanently silent, so
#: past eight reads there was no ceiling at all. Live 2026-08-22: one turn read
#: `js/PlayerShip.js` fifteen times, ran 21m52s, changed nothing, and reported
#: SUCCESS. `read 5 things without producing anything` was the only word said
#: about it, and it was said once.
READS_AFTER_INSISTING_BEFORE_STOPPING = 8

#: `Signal.reason` the loop must END the turn on, rather than append and
#: continue. This is the one signal that is not a nudge - see the note in the
#: module docstring about who owns stopping.
READ_LOOP_EXHAUSTED = "read_loop_exhausted"

# Tools that only LOOK. A call to any of these advances the streak; anything
# else - a write, a command, a memory note - ends it, because the model has
# produced something.
LOOKING_TOOLS = frozenset(
    {
        "read_file", "read_symbol", "list_files", "find_files", "search_files",
        "find_and_read", "search_and_read", "graph_search", "explain_symbol",
        "memory_load", "memory_list", "history_search", "git_status",
        "git_diff", "git_log",
    }
)

# What a model says when it has lost the conversation and thinks it is turn one.
# Deliberately whole phrases rather than "hello": a reply that opens "Hi - I've
# added the handler" is a normal, friendly answer, and nudging it would be the
# guard misfiring on good behaviour.
_GREETING_PATTERNS = (
    re.compile(r"\bhow can i (help|assist)\b", re.IGNORECASE),
    re.compile(r"\bwhat would you like (me )?to (do|work on)\b", re.IGNORECASE),
    re.compile(r"\bwhat can i do for you\b", re.IGNORECASE),
    re.compile(r"\bhow may i (help|assist)\b", re.IGNORECASE),
    re.compile(r"\bi'?m ready to (help|assist|start)\b", re.IGNORECASE),
    re.compile(r"\bhi there[!.]? what\b", re.IGNORECASE),
)


@dataclass(frozen=True)
class Signal:
    """One detector firing: why, and what to say about it."""

    reason: str
    #: Shown to the user as activity, so a nudge is never invisible.
    activity: str
    #: Appended to the conversation as a user-role message.
    correction: str


@dataclass
class ReadLoopDetector:
    """Is the model reading instead of answering?

    The failure this is for has no natural end: *"review X"* has no terminal
    state, so a model can keep gathering context forever and never be wrong to.
    Simple mode already catches an IDENTICAL read repeated three times, which is
    a different fault - that one is the model losing track of what it has. This
    one is eight DIFFERENT reads that produce nothing, which no existing counter
    saw at all.

    Reset by producing anything, not just by writing: an answer is production
    too, and so is running a command.
    """

    streak: int = 0
    nudged: bool = False
    insisted: bool = False
    #: Reads counted since the firm word. Both flags above are one-shot, and
    #: with nothing counting past them the detector fell silent exactly when
    #: the model had proved it was not listening.
    since_insisting: int = 0
    #: Distinct things read this turn. What separates a model opening a
    #: seven-file project from a model reading one file seven times.
    seen: set[str] = field(default_factory=set)

    @property
    def exploring(self) -> bool:
        """Was every read a thing it had not read before?

        Then it is looking, not circling, and telling it that it "probably has
        enough" is telling it something false.
        """
        return bool(self.seen) and len(self.seen) >= self.streak

    def record(
        self,
        tool_names: list[str],
        produced_something: bool,
        targets: list[str] | None = None,
    ) -> Signal | None:
        """Note one round's tool calls. Returns a nudge, or ``None``."""
        if produced_something:
            self.streak = 0
            self.since_insisting = 0
            self.seen.clear()
            return None
        self.seen.update(target for target in (targets or []) if target)
        looked = [name for name in tool_names if name in LOOKING_TOOLS]
        if not looked:
            # Something that was not a read. Whatever it was, it was not this
            # fault, and the streak is about CONSECUTIVE looking.
            if tool_names:
                self.streak = 0
            return None
        self.streak += len(looked)
        if self.insisted:
            # Past the firm word. It was told to stop reading and answer, and
            # it is still reading, so counting resumes toward a ceiling instead
            # of toward another sentence it has already ignored.
            self.since_insisting += len(looked)
            if self.since_insisting >= READS_AFTER_INSISTING_BEFORE_STOPPING:
                self.since_insisting = 0
                return Signal(
                    READ_LOOP_EXHAUSTED,
                    f"read {READS_AFTER_INSISTING_BEFORE_STOPPING} more things after "
                    "being asked to stop; ended the turn",
                    "I have stopped this turn. After being asked twice to answer, I "
                    f"made {READS_AFTER_INSISTING_BEFORE_STOPPING} more read calls "
                    "without writing anything, running anything, or answering.\n\n"
                    "Everything I read is still in the conversation, so nothing is "
                    "lost. Ask for one concrete thing - a single function to change, "
                    "or one question to answer - and I will do that instead of "
                    "looking for more context.",
                )
            return None
        if self.streak >= READS_BEFORE_INSISTING:
            self.insisted = True
            count = self.streak
            exploring = self.exploring
            self.streak = 0
            if exploring:
                # "You have enough to go on" is a claim about the code, and it
                # is not one this detector is in any position to make. Said to a
                # model eight files into a bug spread across seven of them, it
                # is simply false - and it was believed.
                return Signal(
                    "read_loop",
                    f"read {count} different things without producing anything; "
                    "asked it to say what it is after",
                    f"You have now opened {count} different files in this turn and "
                    "produced nothing - no answer, no file changed, no command run."
                    "\n\nWrite down what you have found so far and what is still "
                    "missing, in the reply, before reading anything else. If what "
                    "is missing is one specific thing, fetch exactly that and then "
                    "answer. If you already know the cause, make the change now.",
                )
            return Signal(
                "read_loop",
                f"read {count} things without producing anything; asked it to answer",
                f"You have now made {count} read or search calls in this turn and "
                "produced nothing - no answer, no file changed, no command run, and "
                "you are re-reading things you already have.\n\n"
                "Stop reading. Write your answer, or make the change you were asked "
                "for, in this turn. If one specific thing is genuinely still missing, "
                "fetch exactly that one thing and then answer immediately.",
            )
        if self.streak >= READS_BEFORE_NUDGE and not self.nudged:
            self.nudged = True
            if self.exploring:
                return Signal(
                    "read_loop_warning",
                    f"read {self.streak} different things without producing anything",
                    f"You have opened {self.streak} different files and produced "
                    "nothing yet - no answer, no change, nothing run.\n\n"
                    "If you already know what is wrong, make the change now. If you "
                    "are still looking, say in one line WHAT you are looking for "
                    "before your next read, and read only the thing that would "
                    "settle it. Do not open more files to see what is in them.",
                )
            return Signal(
                "read_loop_warning",
                f"read {self.streak} things without producing anything",
                f"You have read {self.streak} things and produced nothing yet, and "
                "you are re-reading what you already have. You probably have "
                "enough. After your next call, write the answer or make the change "
                "- do not keep reading.",
            )
        return None


def greeting_regression(text: str, *, work_happened: bool) -> Signal | None:
    """Did the model greet the user in the middle of a task?

    A model that answers *"How can I help you?"* after eleven tool calls has
    lost the conversation, and this project has the receipts: a session
    accumulated ten identical harness-status turns and then wrote nothing for
    the rest of its life. The reply reads as polite and is a context failure.

    Only mid-task. `work_happened` is what separates a genuine opening - the
    right answer to "hi" - from the same words arriving after the model has
    already been working.
    """
    if not work_happened:
        return None
    body = (text or "").strip()
    if not body:
        return None
    if not any(pattern.search(body) for pattern in _GREETING_PATTERNS):
        return None
    return Signal(
        "greeting_regression",
        "greeted the user mid-task; asked it to carry on",
        "You have already been working on something in this turn, and that reply "
        "greets the user as if the conversation had just started.\n\n"
        "Look back at what you were doing and carry on from there. Do not restart "
        "the conversation, and do not ask what to work on - you were told.",
    )


#: A reply that is nothing but a tool-call object. `{"name": ..., "arguments":
#: ...}`, optionally fenced. Anchored at both ends on purpose - see
#: `leaked_tool_call`.
_BARE_TOOL_CALL = re.compile(
    r"^\s*(?:```(?:json)?\s*)?\{\s*\"(?:name|tool|function)\"\s*:\s*\"([A-Za-z0-9_.]+)\""
    r".*\}\s*(?:```)?\s*$",
    re.DOTALL,
)


def leaked_tool_call(text: str) -> str:
    """The tool name in a reply that is ONLY a tool call, or ``""``.

    `parse_model_turn` salvages a call from prose only when the name is exactly
    a registered tool, and that gate is right: an unregistered name in the
    middle of an explanation is an example, not a call, and executing prose
    would be worse than ignoring it.

    But when the ENTIRE reply is the object, there is no prose for it to be an
    example in. Live 2026-08-20 a fresh turn answered
    `{"name": "run_file", "arguments": {"filepath": "hello.py"}}` - raw JSON,
    handed to the user as the finished answer, for a tool that does not exist.
    The closest-match correction `_execute` offers never fired, because the
    call never reached dispatch.

    So: only the whole-reply case, which cannot be prose, and it produces a
    NUDGE rather than an execution - the model is told the name is not a tool
    and asked to make a real call.
    """
    match = _BARE_TOOL_CALL.match(text or "")
    return match.group(1) if match else ""


def closest_tool_names(wanted: str, known: list[str], limit: int = 3) -> list[str]:
    """The registered names most like one the model invented.

    A hallucinated tool used to come back as `There is no tool called X.
    Available: <thirty names>` - a list the model has already been shown, in a
    prompt it apparently did not read, offered again at the exact moment it is
    confused. smallcode answers with the nearest few instead, which is a
    correction rather than a repetition.
    """
    import difflib

    bare = (wanted or "").strip().lower().rsplit(".", 1)[-1]
    if not bare or not known:
        return []
    if bare in known:
        # `functions.read_file` names a real tool and only the prefix was wrong.
        # Offering three lookalikes there would be the correction introducing a
        # choice where the model had already got it right - every SHAMSU tool
        # ends in `_file`, so fuzzy matching returns the whole family.
        return [bare]
    close = difflib.get_close_matches(bare, known, n=limit, cutoff=0.6)
    if close:
        return close
    # A model reaching for a Claude-shaped name ("Edit", "Bash") lands nowhere
    # near a SHAMSU one by edit distance, but usually shares a word with it.
    return [name for name in known if bare in name or name in bare][:limit]


#: Invented names that describe a CAPABILITY rather than misspell a tool.
#:
#: `closest_tool_names` cannot help with these. `plan` is not a near miss for
#: anything in the registry - no edit-distance match, no shared word - so it
#: fell through to the branch that answers with all thirty-odd names, which is
#: exactly what the function above exists to avoid. Observed live 2026-08-22:
#: `✗ plan FAILED There is no tool called plan. Available: append_file,
#: ask_user, contra...`, and the model spent the next round no better informed.
#:
#: Only `plan` has been seen in a real run here; the rest are the names the
#: same model families reach for when they were trained against a different
#: harness, and each one names a real SHAMSU tool as the answer.
INVENTED_CAPABILITIES: dict[str, str] = {
    # Reached only when `closest_tool_names` finds nothing, which since
    # `contract_from_plan` shipped it does - it shares the word. Kept correct
    # anyway: this text used to say "There is no planning tool", and there is.
    "plan": "To start a phase of a plan document, call contract_from_plan with "
            "its number. To write down what done means directly, contract_create. "
            "To save the plan as a file, write_file.",
    "todo": "There is no todo tool. Keep the list in your answer, or write it "
            "to a file with write_file.",
    "todo_write": "There is no todo tool. Keep the list in your answer, or "
                  "write it to a file with write_file.",
    "task": "There is no task tool. Do the work yourself with the tools you have.",
    "think": "There is no think tool - think in your reply, then call a tool "
             "or answer.",
    "finish": "There is no completion tool. When you are done, just answer.",
    "done": "There is no completion tool. When you are done, just answer.",
    "attempt_completion": "There is no completion tool. When you are done, "
                          "just answer.",
    "bash": "Use run_command.",
    "shell": "Use run_command.",
    "terminal": "Use run_command.",
    "exec": "Use run_command.",
    "grep": "Use search_files.",
    "rg": "Use search_files.",
    "ls": "Use list_files.",
    "dir": "Use list_files.",
    "cat": "Use read_file.",
    "view": "Use read_file.",
    "open": "Use read_file.",
    "str_replace": "Use patch_file.",
    "str_replace_editor": "Use patch_file.",
    "apply_patch": "Use patch_file.",
}


def invented_capability_hint(wanted: str) -> str:
    """What to do instead of the tool the model wished it had, or "".

    Separate from `closest_tool_names` because it answers a different question.
    That one handles a name that is nearly right; this one handles a name that
    is nowhere near anything, which is a model asking for a capability rather
    than fumbling a spelling. Handing it a list of every tool answers neither.
    """
    bare = (wanted or "").strip().lower().rsplit(".", 1)[-1]
    return INVENTED_CAPABILITIES.get(bare, "")


# How far a retry moves the temperature. smallcode's default, and applied as a
# DELTA on the configured value rather than an absolute, so a user who set a
# temperature still owns the anchor.
TEMPERATURE_STEP = 0.15


def adapted_temperature(base: float, repair_streak: int) -> float:
    """Colder on the first retry, warmer on the second, then back.

    Ported from smallcode `src/model/adaptive_temp.js`. Their reasoning, and it
    matches what this project keeps seeing: at one fixed temperature a retry
    produces "same strategy, same mistake" - and three byte-identical broken
    patches in a row is a failure mode with its own guard here already
    (`IDENTICAL_FAILURES_BEFORE_REFUSING`). Moving the dial is the cheapest way
    to stop generating the same wrong answer.

    Attempt 1 goes DOWN, not up: the first retry has an exact error message to
    work from, and wants determinism. Only when that has failed too is
    exploration worth paying for.
    """
    if repair_streak <= 0:
        return base
    phase = repair_streak % 3
    if phase == 1:
        delta = -TEMPERATURE_STEP
    elif phase == 2:
        delta = TEMPERATURE_STEP
    else:
        delta = 0.0
    return round(min(1.0, max(0.0, base + delta)), 3)


@dataclass
class TrustDecay:
    """Tools that keep failing get demoted, then withheld - reads only.

    Ported from smallcode `src/tools/trust_decay.js` with one hard difference:
    **a writing tool is never dropped.** Theirs may drop any tool after five
    consecutive failures, and dropping `patch_file` would leave a model unable
    to edit anything at all - which is a worse state than the loop it prevents.
    A search that keeps returning nothing can be taken away; the ability to
    change a file cannot.

    Reset by any success, because the question is "is this tool working HERE,
    now", not "has it ever failed".
    """

    consecutive: dict[str, int] = field(default_factory=dict)
    warn_after: int = 3
    drop_after: int = 5
    #: Never withheld however often they fail.
    protected: frozenset[str] = frozenset()

    def record(self, tool: str, ok: bool) -> None:
        if not tool:
            return
        if ok:
            self.consecutive.pop(tool, None)
        else:
            self.consecutive[tool] = self.consecutive.get(tool, 0) + 1

    def level(self, tool: str) -> str:
        """``"ok"``, ``"warn"`` or ``"drop"``."""
        if tool in self.protected:
            return "ok"
        failures = self.consecutive.get(tool, 0)
        if failures >= self.drop_after:
            return "drop"
        if failures >= self.warn_after:
            return "warn"
        return "ok"

    def dropped(self) -> frozenset[str]:
        """Every tool currently withheld."""
        return frozenset(
            tool for tool in self.consecutive if self.level(tool) == "drop"
        )
