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

**Every signal is a nudge, never a stop.** These detect a model that has lost
the thread, not one that has failed - and this project's rule is that a guard
the model cannot get past is a deadlock waiting for a user to notice. The loop
owns stopping.
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

    def record(self, tool_names: list[str], produced_something: bool) -> Signal | None:
        """Note one round's tool calls. Returns a nudge, or ``None``."""
        if produced_something:
            self.streak = 0
            return None
        looked = [name for name in tool_names if name in LOOKING_TOOLS]
        if not looked:
            # Something that was not a read. Whatever it was, it was not this
            # fault, and the streak is about CONSECUTIVE looking.
            if tool_names:
                self.streak = 0
            return None
        self.streak += len(looked)
        if self.streak >= READS_BEFORE_INSISTING and not self.insisted:
            self.insisted = True
            count = self.streak
            self.streak = 0
            return Signal(
                "read_loop",
                f"read {count} things without producing anything; asked it to answer",
                f"You have now made {count} read or search calls in this turn and "
                "produced nothing - no answer, no file changed, no command run. You "
                "have enough to go on.\n\n"
                "Stop reading. Write your answer, or make the change you were asked "
                "for, in this turn. If one specific thing is genuinely still missing, "
                "fetch exactly that one thing and then answer immediately.",
            )
        if self.streak >= READS_BEFORE_NUDGE and not self.nudged:
            self.nudged = True
            return Signal(
                "read_loop_warning",
                f"read {self.streak} things without producing anything",
                f"You have read {self.streak} things and produced nothing yet. You "
                "probably have enough. After your next call, write the answer or "
                "make the change - do not keep reading.",
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
