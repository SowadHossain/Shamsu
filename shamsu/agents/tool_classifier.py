"""Guess which KIND of tool this request needs, before spending anything.

Adapted from smallcode `src/compiled/tool_router.js` (`classifyToolCategory`),
a weighted regex scorer over the user's message. Zero LLM calls, zero round
trips, zero latency - which is the whole point, because the alternative already
exists here and costs a turn.

**The hole this fills.** Simple mode had two ways to shrink the tool roster and
neither covers the model this project is actually shipping on:

* `select_category` - the model picks, which costs a full round trip. Engaged
  only at or below `TWO_STAGE_CTX_THRESHOLD` (16,384).
* `_without_unavailable_families` - withholds families with nothing to answer
  from. Zero cost, but it asks *"could this tool answer at all?"*, never *"does
  THIS request need it?"*

Above 16k the catalogue therefore goes out whole. Measured on a 32k window:
**26 schemas, 3,196 tokens, every single turn** - about a tenth of the window
spent describing tools before a line of code is read, and it grows with every
tool added. That is the tax that made adding `delete_file`, `ask_user`, git and
web tools look expensive.

**One deliberate difference from smallcode.** Their direct mode has no escape:
a wrong guess strands the model with the wrong tools and it cannot say so. Ours
always keeps `select_category` in the roster, so a misclassified turn costs one
round trip to correct rather than failing. A cheap guess with an escape hatch is
a different proposition from a cheap guess without one - and it is what lets the
scorer be aggressive enough to be worth having.

Low confidence sends everything. A classifier unsure what the request wants is
not evidence that the request wants little.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Below this many characters, and with no action verb in sight, a message is a
# greeting or an acknowledgement rather than a task. smallcode's fast path.
SHORT_MESSAGE_CHARS = 24

# Above this, a request is doing several things and should not be narrowed to
# one category. smallcode boosts `plan` at this length; here the plan ANCHOR
# handles a long multi-part request (see `agents/plan_anchor.py`), so length
# widens the roster instead - the safer direction when a request has parts the
# scorer cannot see.
LONG_MESSAGE_CHARS = 300

# Below this margin between the top two categories, the winner is not a winner.
# Send everything rather than guess.
MIN_CONFIDENCE = 0.15

# Words that mean a short message is still a task. "run it", "fix it", "ls".
#
# The question words are here because leaving them out was a real defect, not a
# nicety: "where is login?" is fifteen characters and "who calls parse_config?"
# is twenty-three, so both were being read as greetings and handed the entire
# catalogue. Including them cannot misfire - a genuine greeting that happens to
# contain "how" scores nothing, and a zero score sends everything anyway. The
# fast path is an optimisation for "hi", not a classifier in its own right.
_ACTION_IN_A_SHORT_MESSAGE = re.compile(
    r"\b(run|fix|read|show|find|builds?|tests?|git|npm|pip|go|cd|ls|rm|mv|cp|"
    r"add|edit|patch|write|rename|delete|continue|next|"
    r"where|what|which|who|whose|why)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Signal:
    """One regex and what matching it is worth. Negative weights are evidence
    AGAINST a category, which is what stops "fix the search bug" reading as a
    search task."""

    pattern: re.Pattern[str]
    weight: float


def _signals(*pairs: tuple[str, float]) -> tuple[Signal, ...]:
    return tuple(Signal(re.compile(p, re.IGNORECASE), w) for p, w in pairs)


# These are `simple_router.TOOL_CATEGORIES`, not smallcode's eight: their
# `code_intel` lives inside our `search`, we have no web tools wired, and their
# `respond` has no equivalent. Keeping the two lists identical is what makes a
# category a usable answer from `select_category` as well as from this scorer.
CATEGORY_SIGNALS: dict[str, tuple[Signal, ...]] = {
    "read": _signals(
        (r"\b(read|show|cat|display|print|view|open|look\s+at|see|inspect)\b", 3.0),
        (r"\b(what'?s\s+in|what\s+is\s+in|contents?\s+of|what\s+files|list\s+files)\b", 3.0),
        (r"\b(review|analyse|analyze|examine|audit|go\s+over|walk\s+me\s+through)\b", 3.0),
        (r"\b(file|\.\w{1,4})\b", 1.5),
        (r"\b(fix|change|update|modify|add|remove|delete|create|write|rename)\b", -2.0),
        (r"\b(run|execute|test|build|install)\b", -1.5),
    ),
    "write": _signals(
        (r"\b(fix|change|update|modify|edit|refactor|rename|replace|patch|move)\b", 3.0),
        (r"\b(add|insert|append|prepend|implement|create|write|make|build\s+a)\b", 2.5),
        (r"\b(remove|delete|strip|clean\s*up|drop)\b", 2.0),
        (r"\b(bug|error|typo|issue|broken|wrong|incorrect|failing|fail|crash)\b", 2.0),
        (r"\b(file|function|class|method|variable|import|export)\b", 1.0),
        (r"\b(explain|why|how\s+does|tell\s+me)\b", -1.5),
        (r"\b(search|find|grep|look\s+for)\b", -1.5),
        # "how should we approach the refactor?" is asking for advice. Without
        # this, `refactor` alone outweighed the question and handed the model
        # the write tools for a turn that was meant to produce words.
        (r"\b(how should|what'?s the best way|should (we|i)\b|would you)", -3.0),
    ),
    "search": _signals(
        (r"\b(find|search|grep|look\s+for|locate|where\s+is|where\s+are)\b", 3.0),
        (r"\b(all\s+uses?\s+of|all\s+references?|who\s+calls?|who\s+uses?|imports?\s+of)\b", 3.0),
        (r"\b(pattern|regex|match|occurrences?)\b", 2.0),
        (r"\b(across|everywhere|all\s+files|codebase|project)\b", 1.5),
        (r"\b(fix|change|update|create|write)\b", -2.0),
    ),
    "run": _signals(
        (r"\b(run|execute|start|launch|invoke)\b", 3.0),
        (r"\b(tests?|specs?|pytest|jest|mocha|vitest|unittest)\b", 3.0),
        (r"\b(build|compile|make|bundle|webpack|tsc|cargo)\b", 2.5),
        (r"\b(install|npm|pip|yarn|pnpm|poetry|uv)\b", 2.5),
        (r"\b(does\s+it\s+work|check\s+it\s+works|verify\s+it)\b", 2.0),
    ),
    "verify": _signals(
        (r"\b(definition\s+of\s+done|acceptance|criteria|checklist)\b", 3.0),
        (r"\b(contract|assert|prove|confirm\s+that)\b", 2.0),
        (r"\b(tests?\s+pass|all\s+green|is\s+it\s+done)\b", 2.0),
    ),
    "plan": _signals(
        (r"\b(plan|approach|strategy|design|outline the|how should (we|i))\b", 3.0),
        (r"\b(before (you|we) start|first work out|think through|scope)\b", 2.5),
        (r"\b(step by step|in order|one at a time|break (it|this) down)\b", 2.0),
        # Asking for a plan is asking for WORDS. A request that also says
        # "and then do it" is not this category.
        (r"\b(now|just|go ahead and) (do|fix|write|add|make)\b", -2.5),
    ),
    "recall": _signals(
        (r"\b(remember|recall|note\s+that|keep\s+in\s+mind|forget)\b", 3.0),
        (r"\b(earlier|last\s+time|previously|yesterday|we\s+discussed|you\s+said)\b", 3.0),
        (r"\b(what\s+did\s+(we|you|i)|history|past\s+session)\b", 2.5),
    ),
}

# Ties break toward the category that fails most gracefully. `write` first
# because a write-shaped request handed read tools cannot act at all, while a
# read-shaped request handed write tools simply does not use them.
PRIORITY: tuple[str, ...] = ("write", "read", "search", "run", "plan", "recall", "verify")


@dataclass(frozen=True)
class Classification:
    """What the scorer thinks, and how strongly."""

    category: str
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def certain_enough(self) -> bool:
        """Is this worth acting on, or should the model get everything?"""
        return bool(self.category) and self.confidence >= MIN_CONFIDENCE


def _score(message: str, signals: tuple[Signal, ...]) -> float:
    return sum(signal.weight for signal in signals if signal.pattern.search(message))


def classify_request(message: str) -> Classification:
    """Which category of tool this request most likely needs.

    Pure and deterministic: same string in, same answer out, no model, no
    network, no clock. That is what makes it safe to run on every turn and
    testable without a GPU.

    An empty category means "no idea" and the caller must send everything.
    """
    text = (message or "").strip()
    if not text:
        return Classification("", 0.0, {})
    if len(text) <= SHORT_MESSAGE_CHARS and not _ACTION_IN_A_SHORT_MESSAGE.search(text):
        # "hi", "thanks", "ok" - no tools needed, but this function does not get
        # to decide that on its own. Saying so with no confidence lets the
        # caller keep the full roster for a greeting that turns out to be one.
        return Classification("", 0.0, {})
    if len(text) > LONG_MESSAGE_CHARS:
        # Several things at once. Narrowing a multi-part request to one category
        # is how the third part finds its tool missing.
        return Classification("", 0.0, {})

    scores = {name: _score(text, signals) for name, signals in CATEGORY_SIGNALS.items()}
    ranked = sorted(scores.items(), key=lambda item: (-item[1], PRIORITY.index(item[0])))
    top_name, top_score = ranked[0]
    if top_score <= 0:
        return Classification("", 0.0, scores)
    runner_up = max(ranked[1][1], 0.0) if len(ranked) > 1 else 0.0
    # How DOMINANT the winner is, not how high it scored. A 6.0 beaten down to a
    # 5.5 second place is a coin flip; a 3.0 with nothing behind it is not.
    confidence = min(1.0, (top_score - runner_up) / max(top_score, 3.0))
    return Classification(top_name, confidence, scores)


def categories_for(message: str) -> tuple[str, ...]:
    """The categories worth sending for this request, widest-first.

    Empty means "send everything". Never a single category on its own: the
    scorer is a guess, and a guess should narrow the roster rather than replace
    it. `read` rides along with `write` because every edit starts with a read,
    and `verify` rides along with `run` because a test result is what a
    contract assertion is made of.
    """
    verdict = classify_request(message)
    if not verdict.certain_enough:
        return ()
    companions = {
        "write": ("write", "read"),
        "read": ("read", "search"),
        "search": ("search", "read"),
        "run": ("run", "verify"),
        "verify": ("verify", "run"),
        "recall": ("recall", "read"),
        # Planning reads and writes the plan down; it never edits code. Pairing
        # it with `read` rather than `write` is what makes the category mean
        # something - a planning turn that can call `write_file` is just a
        # normal turn with a different label.
        "plan": ("plan", "read"),
    }
    return companions.get(verdict.category, (verdict.category,))
