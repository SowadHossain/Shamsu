"""Does this request need a plan, and what should the model be shown of it?

Adapted from smallcode `src/session/plan_tracker.js` - the *heuristic* and the
*re-injection*, not the machinery. Theirs carries a parallel plan object with
its own step cursor, parser and store. SHAMSU already has all of that under
another name: `agents/simple_contract.py` holds an ordered list of checkable
items, each `pending|passed|failed|skipped`, persisted per workspace, with a
done-guard that will not let the model claim finished while one is unresolved.

So the gap was never the data structure. It was two things:

1. **Nothing ever asked for a plan.** `contract_create` is offered and a model
   that does not think to call it never does.
2. **Nothing ever showed the plan again.** The contract sat on disk and reached
   the model only if it called `contract_status` - so the thing meant to keep a
   multi-step task on the rails was invisible unless the model remembered to
   ask, which is exactly what a model losing the thread stops doing. smallcode
   re-injects `ACTIVE PLAN (step 3 of 5)` on every single turn, and that is the
   half worth copying.

On smallcode's step cursor: their own code says *"Don't auto-advance - let the
model explicitly mark completion. Auto-advance leads to drift in long traces."*
Same conclusion here, reached the same way - which is why this leans on
`contract_assert_pass` rather than inventing a cursor to guess with.
"""
from __future__ import annotations

import os
import re

# Below this, a request is one thing. smallcode uses 300 for the "definitely
# multi-step" signal and a second, weaker test at 150 with three sentences.
LONG_REQUEST_CHARS = 300
SEVERAL_SENTENCES_CHARS = 150
SEVERAL_SENTENCES = 3

# The re-injected block is a tax on every turn of a long task, so it is capped.
# Enough for about a dozen steps; past that the model is being handed a document
# rather than an anchor.
MAX_ANCHOR_CHARS = 900

# Phrases that mean "this has parts". Kept to things that state SEQUENCE or
# MULTIPLICITY, not merely difficulty: "fix the hard bug" is one step.
_PLAN_HINTS = (
    re.compile(r"\b(then|after that|afterwards|finally|lastly|next,)\b", re.IGNORECASE),
    re.compile(r"\b(and then|followed by|once (that|it)'?s? done)\b", re.IGNORECASE),
    re.compile(r"\b(step by step|one at a time|in order|each of)\b", re.IGNORECASE),
    # No bare `port`: it matched "remember: the port is 8080" and asked a
    # twenty-six character note to write itself a plan. In this domain the noun
    # is everywhere and the verb is rare, and `migrate`/`rewrite` already carry
    # the intent. A false positive here costs a round AND anchors the model to
    # a plan it had no reason to write.
    re.compile(r"\b(refactor|migrate|rewrite|redesign|restructure)\b", re.IGNORECASE),
    re.compile(r"\b(implement|build|create) (a|an|the) \w+ (feature|system|module|api|page)\b", re.IGNORECASE),
    re.compile(r"^\s*\d+[.)]\s+\S", re.MULTILINE),  # the user numbered it themselves
)

# A request that is explicitly asking for words, not work. Planning THIS would
# be planning to plan.
_ASKS_FOR_WORDS = re.compile(
    r"^\s*(what|why|how does|how do|explain|describe|tell me|is |are |does |can you tell)",
    re.IGNORECASE,
)


def plan_disabled() -> bool:
    """``SHAMSU_PLAN=0`` turns the whole anchor off, smallcode's switch."""
    return os.environ.get("SHAMSU_PLAN", "").strip().lower() in {"0", "false", "no", "off"}


def should_plan(request: str) -> bool:
    """Is this a job with parts, worth writing down before starting?

    Deliberately conservative. A false positive costs a round and anchors the
    model to a plan it wrote badly; a false negative costs nothing, because the
    model can still call `contract_create` itself. So the bar is evidence of
    SEQUENCE, not evidence of difficulty.
    """
    if plan_disabled():
        return False
    text = (request or "").strip()
    if not text or _ASKS_FOR_WORDS.match(text):
        return False
    if len(text) > LONG_REQUEST_CHARS:
        return True
    if any(hint.search(text) for hint in _PLAN_HINTS):
        return True
    if len(text) > SEVERAL_SENTENCES_CHARS:
        sentences = [s.strip() for s in re.split(r"[.!?]+", text) if len(s.strip()) > 10]
        return len(sentences) >= SEVERAL_SENTENCES
    return False


def ask_for_a_plan(request: str) -> str:
    """The one-shot instruction, or ``""``.

    Phrased as the call to make rather than as a rule to remember. "Plan before
    you start" has been in this project's prompts before and did not survive
    contact with a 3B; naming the tool and its argument does.
    """
    if not should_plan(request):
        return ""
    return (
        "This has several parts, so write them down before starting: call "
        "contract_create with a short title and one assertion per part, in the "
        "order you will do them. Then work through them, recording each with "
        "contract_assert_pass and the evidence that shows it.\n\n"
        "Do that first, in this turn, then begin."
    )


def anchor(rendered_contract: str) -> str:
    """The contract, re-shown as the turn's standing plan.

    smallcode's `ACTIVE PLAN (step 3 of 5)`. Without this the contract exists
    and is invisible: it reaches the model only when it calls
    `contract_status`, and a model that has lost the thread is exactly the one
    that stops asking.
    """
    body = (rendered_contract or "").strip()
    if not body:
        return ""
    if len(body) > MAX_ANCHOR_CHARS:
        body = body[:MAX_ANCHOR_CHARS].rstrip() + "\n  ... (call contract_status for the rest)"
    return "ACTIVE PLAN - you wrote this. Keep working through it:\n" + body
