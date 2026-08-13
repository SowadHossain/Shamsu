"""Deciding whether a request is a task at all.

The state machine in `runtime/session.py` runs *tasks*. It inspects, plans,
patches, and refuses to finish without evidence that a tool actually produced.
That is exactly the right shape for "fix the login bug" and exactly the wrong
shape for "hi".

Before this module existed, every line typed at the prompt became a
`TaskRecord`. So a greeting was planned — and a small model asked to plan `hi`
proposes a step like *"Understand the task"*, which inherits
`PlanStepProposal.kind`'s default of `change`, which carries the mandatory
change floor of `file_changed` and `git_diff_reviewed`. Neither is producible
from a greeting, so the gate refused, the runtime re-planned, and the run ended
BLOCKED with a report about missing evidence. Every component behaved as
designed. The input should never have reached them.

**Answering is the default, and only work is opted into.** The first version of
this ran the other way — TASK unless the input matched an enumerated question
shape — and that list never stopped growing: wh-words, then auxiliary verbs,
then informational imperatives, then a trailing question mark, and still
"summarise the architecture" fell through to a plan. Every unlisted phrasing
became a task, and a task can only edit and prove.

The asymmetry is what settles it. "Does this ask for a change?" has a small,
stable vocabulary of action verbs. "Is this a question?" does not, because
there is no closed set of ways to want to know something. So `triage` asks only
the first, and a request that does not clearly ask for work is answered.

Getting that wrong is cheap in the direction it now fails: a misread question
runs a bounded read-only look and reports what it found. A misread task ran
plan → author → verify → repair and ended blocked.

**The CHAT matcher stays tight**, and that direction is not arbitrary. v1
shipped a broad "does this look like a project request?" test and it stole real
questions. A false CHAT answers a real request with pleasantries and does
nothing at all — worse than either other mistake — so it is a positive match
against a closed vocabulary, anchored to the whole message, with a hard
word-count ceiling above which nothing is chat.

`triage` is a pure function over a string: no model, no workspace, no index. It
is index-independent on purpose — v1's equivalent guard was gated behind "is
this workspace indexed?", so the same greeting behaved differently in two
directories.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from enum import StrEnum

from shamsu.agent.planning import asks_for_a_change
from shamsu.interfaces.cancellation import CancellationToken, NullCancellationToken
from shamsu.interfaces.models import (
    ModelClient,
    ModelContractError,
    ModelMessage,
    ModelRequest,
    ModelTimeout,
    ModelUnavailable,
)
from shamsu.interfaces.tools import ToolContract
from shamsu.models.contracts import RequestIntent, schema_hint
from shamsu.models.normalization import normalise as strip_model_wrapping

#: Above this many words, a message is a task whatever it contains. A genuine
#: greeting is short; this is the backstop that keeps a long request from
#: matching on an incidental "thanks" at the end.
MAX_CHAT_WORDS = 8

#: Tokens that open a message without saying anything about the work.
_GREETINGS: frozenset[str] = frozenset(
    {
        "hi",
        "hey",
        "hello",
        "heya",
        "hiya",
        "howdy",
        "yo",
        "sup",
        "hola",
        "salam",
        "salaam",
        "greetings",
    }
)

#: What may legitimately follow a greeting and still be a greeting. Checked
#: only as a *remainder*, never as a whole message: "there" on its own is not
#: small talk, it is someone who hit enter early.
_GREETING_TAILS: frozenset[str] = frozenset(
    {"there", "again", "friend", "buddy", "mate", "man", "all", "everyone", "team", "shamsu"}
)

#: Acknowledgements and sign-offs. A whole message made only of these asks for
#: nothing, so planning one is guaranteed to produce a plan with no work in it.
_ACKS: frozenset[str] = frozenset(
    {
        "thanks",
        "thank you",
        "thanks a lot",
        "thank you very much",
        "thx",
        "ty",
        "cheers",
        "ok",
        "okay",
        "k",
        "kk",
        "cool",
        "nice",
        "great",
        "awesome",
        "perfect",
        "sweet",
        "good",
        "fine",
        "alright",
        "sure",
        "yes",
        "yeah",
        "yep",
        "yup",
        "no",
        "nope",
        "nah",
        "got it",
        "understood",
        "makes sense",
        "sounds good",
        "lol",
        "haha",
        "hmm",
        "bye",
        "goodbye",
        "good bye",
        "see ya",
        "see you",
        "cya",
        "later",
        "good night",
        "goodnight",
        "gn",
    }
)

#: Conversational openers, matched against the *entire* normalised message.
#: `fullmatch` rather than `search` is the whole safety property here: "how
#: does auth work" must not match the "how are you" family, and it does not,
#: because it is not the entire message.
_OPENERS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        # Apostrophes are optional throughout: small models and fast typists
        # both write "whats up", and a contraction is not the signal here.
        r"how are you( doing| going)?",
        r"how('?s| is| are) (it going|things|you doing|everything)",
        r"how have you been",
        r"what('?s| is) up",
        r"what('?s| is) new",
        r"who are you",
        r"what are you",
        r"are you (there|ok|okay|alive|working|awake|ready)",
        r"you (there|up|ready)",
        r"good (morning|afternoon|evening|day)",
        r"nice to meet you",
        r"long time no see",
        r"test(ing)?",
        r"ping",
    )
)

#: Questions about SHAMSU itself. Matched before anything else, because the
#: answer comes from the tool registry rather than from the workspace — asking
#: what the agent can do should not require investigating a repository.
_ABOUT_ITSELF: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r".*\b(what|which)\b.*\b(can|could)\b.*\b(you|shamsu)\b.*\bdo\b.*",
        r".*\bwhat\b.*\b(you|shamsu|your)\b.*"
        r"\b(capab|abilit|feature|tool|command|support|offer)\w*\b.*",
        r".*\b(capab|abilit)\w*\b.*",
        r".*\b(list|show|tell me)\b.*\b(your|the|available)\b.*"
        r"\b(tool|command|capab\w*|feature)s?\b.*",
        r".*\bwhat\b.*\b(tool|command)s?\b.*\b(do|does|are)\b.*",
        r".*\bwhat\b.*\b(are|can)\b.*\byou\b.*\b(able|capable)\b.*",
        r"help|what can i ask|what should i ask",
    )
)

#: A wh-word opens a question on its own.
_WH_QUESTION = re.compile(r"^(what|why|how|where|which|who|whom|whose|when)\b")

#: An auxiliary verb opens a question only when a subject follows it. "do you
#: support X" asks; "do something" orders — and the word after the auxiliary is
#: the only thing that separates them. Without this, every imperative starting
#: "do ..." was answered instead of done.
#: Strips a single leading greeting token and whatever punctuation trails it,
#: so "hey, fix the login bug" is judged on "fix the login bug".
_LEADING_GREETING = re.compile(
    r"^(?:" + "|".join(sorted(_GREETINGS, key=len, reverse=True)) + r")\b[\s,.!—-]*"
)

#: Characters trimmed from the ends before matching. Internal apostrophes are
#: kept — "what's up" has to survive normalisation intact.
_TRIM = " \t\r\n.!?,;:…\"'`*_~()[]{}<>"


class Intent(StrEnum):
    """What kind of input this is, decided before any run begins."""

    #: Nothing was typed, or nothing but punctuation.
    EMPTY = "empty"

    #: Small talk. Gets a short reply and no run.
    CHAT = "chat"

    #: A question about SHAMSU itself — what it can do, which tools it has.
    #: Answered from the tool contracts, so the answer cannot drift from the
    #: truth and needs no model at all.
    CAPABILITIES = "capabilities"

    #: Anything the user wants to know. **The default.** Answered by a
    #: read-only investigation, so it costs a few seconds and changes nothing.
    QUESTION = "question"

    #: Real work. Goes to the state machine. Entered only on a positive signal
    #: that a change was asked for.
    TASK = "task"


def triage(request: str) -> Intent:
    """Classify one input. Pure, deterministic, and index-independent.

    Defaults to `QUESTION`. Only a positive signal that a change was asked for
    routes to the state machine; everything else is answered.
    """
    normalised = normalise_request(request)
    if not normalised:
        return Intent.EMPTY

    # Checked before the word ceiling and before the greeting strip: "hey, what
    # tools do you have?" is still a question about SHAMSU.
    if any(pattern.fullmatch(normalised) for pattern in _ABOUT_ITSELF):
        return Intent.CAPABILITIES

    if len(normalised.split()) <= MAX_CHAT_WORDS and _is_conversational(normalised):
        return Intent.CHAT

    # "hey how are you" is chat; "hey, fix the login bug" is work; "hello, what
    # does this project do" is a question. Exactly one leading greeting is
    # stripped, and everything after this point judges the *remainder* — the
    # greeting is not part of what was asked.
    remainder = _LEADING_GREETING.sub("", normalised, count=1).strip()
    if remainder != normalised:
        if not remainder or remainder in _GREETING_TAILS:
            return Intent.CHAT
        if len(remainder.split()) <= MAX_CHAT_WORDS and _is_conversational(remainder):
            return Intent.CHAT
        normalised = remainder

    # One test, and answering is the default.
    #
    # This used to be the other way round: TASK unless the input matched one of
    # a growing set of question shapes — wh-words, auxiliary+subject,
    # informational imperatives, a trailing question mark. Every phrasing that
    # was not enumerated became a task, and a task can only edit and prove, so
    # "what can you do?" ended BLOCKED on "the failure implicates no editable
    # file". Each miss needed another pattern; there is no end to that list.
    #
    # "Does this ask for a change?" is a small, stable question with a closed
    # vocabulary of action verbs. "Is this a question?" is an open one. So only
    # the first is asked, and everything else is answered.
    #
    # The costs are not symmetric, which is what makes this the safe default:
    # a misread question runs a bounded read-only look and reports what it
    # found, while a misread task runs plan → author → verify → repair and ends
    # blocked. The user can always follow an answer with "now fix it".
    return Intent.TASK if asks_for_a_change(normalised) else Intent.QUESTION


#: Decided without a model, because each is certain and instant. Asking a
#: server whether "hi" is a greeting would make the cheapest interaction the
#: slowest, and `describe_capabilities` reads the tool registry — a model could
#: only be wrong about it.
_SETTLED = (Intent.EMPTY, Intent.CHAT, Intent.CAPABILITIES)

_INTENT_RULES = """\
Decide what the user wants.

'change' means they are asking you to modify the repository — write, edit,
delete, rename, refactor, fix, add, configure, or otherwise leave it different
from how you found it.

'question' means they want to know something about THIS PROJECT — its code, its
structure, why a test fails, what a file does, what is in a document, how it
should be built, what the plan for it ought to be. Asking you to *plan* or
*describe an approach* is a question: it wants an answer, not an edit.

'chatter' means the project is irrelevant to the request. Jokes, the weather,
the news, greetings, language trivia, opinions, personal questions. Use it only
when reading the repository could not possibly help.

Examples:
  "can you plan how to build this project"  -> question
  "what is in the PRD"                      -> question
  "why is the parser slow"                  -> question
  "tell me a joke"                          -> chatter
  "what is the weather"                     -> chatter
  "what is python"                          -> chatter
  "fix the login bug"                       -> change
  "add a health check endpoint"             -> change

If you are not certain between 'question' and 'change', answer 'question'.
Answering is cheap and reversible; attempting a change that was never wanted is
not. If you are not certain whether the project is relevant, answer
'question' — investigating and finding nothing is better than refusing."""


async def classify(
    request: str,
    model: ModelClient | None = None,
    *,
    cancel: CancellationToken | None = None,
) -> Intent:
    """Triage, using the model for the one decision worth a round trip.

    The deterministic answers are returned first and unchanged — empty input,
    small talk, and questions about SHAMSU's own tools cost nothing and cannot
    be improved on. Everything else is the change-or-question boundary, and
    that is where a pattern list stops scaling: "the login is broken, sort it
    out" names no verb any vocabulary would list, and a model reads it at once.

    Falls back to `triage` whenever the model cannot answer — unavailable,
    timed out, or off-contract. A classifier that fails closed would make an
    unreachable server look like a broken agent, and the deterministic rule is
    a perfectly serviceable second opinion.
    """
    settled = triage(request)
    if settled in _SETTLED or model is None:
        return settled

    try:
        answer = await model.generate_typed(
            ModelRequest(
                messages=(
                    ModelMessage(
                        role="user",
                        content=(
                            f"{_INTENT_RULES}\n\n{schema_hint(RequestIntent)}\n\n"
                            f"User: {request.strip()}"
                        ),
                    ),
                ),
                max_output_tokens=INTENT_OUTPUT_TOKENS,
            ),
            RequestIntent,
            cancel or NullCancellationToken(),
        )
    except (ModelContractError, ModelUnavailable, ModelTimeout):
        return settled

    if answer.intent == "change":
        return Intent.TASK
    if answer.intent == "chatter":
        # Searching a repository for a joke or the weather produces exactly the
        # answer it deserves — "the repository does not contain any information
        # about the weather today" — which reads as a malfunction rather than a
        # decline. Chatter gets a short reply and no tools.
        return Intent.CHAT
    return Intent.QUESTION


def normalise_request(request: str) -> str:
    """Lowercase, collapse whitespace, and trim surrounding punctuation."""
    collapsed = " ".join((request or "").split())
    return collapsed.strip(_TRIM).lower().strip()


def _is_conversational(text: str) -> bool:
    """Whether the whole message is a greeting, an acknowledgement, or an opener."""
    if text in _GREETINGS or text in _ACKS:
        return True
    return any(pattern.fullmatch(text) for pattern in _OPENERS)


# ---------------------------------------------------------------------------
# Replying
# ---------------------------------------------------------------------------

#: Used when the model cannot be reached. A session that can still answer "hi"
#: with something true is better than one that reports an outage for a
#: greeting — and this text promises nothing the runtime cannot do.
FALLBACK_REPLY = "Ready. Describe a change you want made, or type /help."

#: Small talk needs a sentence, not a plan. Capping this low also bounds what a
#: model can spend on a message that asked for nothing.
CHAT_OUTPUT_TOKENS = 160

#: One word and a short reason. Kept tight so classification stays a fast call
#: rather than something a user waits on before every request.
INTENT_OUTPUT_TOKENS = 120

_CHAT_RULES = """\
You are SHAMSU, a local-first coding agent that works inside a user's
repository.

The message below is small talk, not a coding task. Reply directly in one or
two short sentences. Do not narrate your reasoning, do not describe yourself in
the third person, and do not mention context, tools, files, or plans unless the
user asked about them. Do not offer a list of capabilities."""


async def respond(
    model: ModelClient,
    request: str,
    cancel: CancellationToken | None = None,
) -> str:
    """Answer small talk with one short line.

    Deliberately given **no workspace context**. v1 passed the agent context
    into this prompt and the model dutifully narrated *about* it — "The
    assistant has no specific context or action to take..." — because a model
    handed a file listing will talk about the file listing. The fix is not a
    sterner instruction; it is having nothing to narrate.

    Never raises for an unreachable model: a greeting is not worth ending a
    session over.
    """
    token = cancel or NullCancellationToken()
    token.raise_if_cancelled()

    prompt = f"{_CHAT_RULES}\n\nUser: {request.strip()}\nReply:"

    try:
        response = await model.generate(
            ModelRequest(
                messages=(ModelMessage(role="user", content=prompt),),
                max_output_tokens=CHAT_OUTPUT_TOKENS,
            ),
            token,
        )
    except (ModelUnavailable, ModelTimeout):
        return FALLBACK_REPLY

    return _first_reply(response.text) or FALLBACK_REPLY


#: A response that is structured data rather than prose. Small models
#: occasionally answer small talk with the JSON shape they were asked for on
#: the *previous* call, and the scripted client emits a bare `{}` when its
#: queue is empty. Showing either to a user as a greeting is worse than saying
#: nothing, so both fall back.
_LOOKS_LIKE_DATA = re.compile(r"^[\[{].*[\]}]$", re.DOTALL)


def describe_capabilities(contracts: Sequence[ToolContract]) -> str:
    """What SHAMSU can do, read off the tool contracts.

    Deterministic and model-free. The answer to "what can you do?" is a fact
    about the registry, and asking a model to describe its own tools invites a
    confident list of ones it does not have — invariant 8, one layer up.

    Grouped by whether a tool changes anything, because that is the distinction
    a person asking this actually wants.
    """
    reading = [c for c in contracts if not c.mutating]
    writing = [c for c in contracts if c.mutating]

    lines = ["I am SHAMSU, a local-first coding agent. I work in one repository at a time.", ""]

    if reading:
        lines.append("To understand a project:")
        lines.extend(f"  {c.name:<16} {c.purpose.splitlines()[0]}" for c in reading)
    if writing:
        lines.extend(["", "To change it:"])
        lines.extend(f"  {c.name:<16} {c.purpose.splitlines()[0]}" for c in writing)

    lines.extend(
        [
            "",
            "Ask me a question about the code and I will investigate and answer.",
            "Ask for a change and I will plan it, make it, and prove it — a task is",
            "only complete when a tool has produced evidence for every step.",
            "Type / for commands.",
        ]
    )
    return "\n".join(lines)


def _first_reply(text: str) -> str:
    """The reply itself, with reasoning spans and a leading role label removed.

    Ollama ≥0.9 returns reasoning in a separate field, but not every model or
    version does, and a leaked `<think>` block is the single most common way
    casual chat looks broken.

    Returns an empty string for anything that is not prose, which the caller
    turns into `FALLBACK_REPLY`. Honest failure over fabrication: a greeting
    answered with `{}` is not an answer.
    """
    body = strip_model_wrapping(text).text.strip()
    body = re.sub(r"^(?:reply|answer|assistant|shamsu)\s*:\s*", "", body, flags=re.IGNORECASE)
    body = " ".join(body.split())
    return "" if _LOOKS_LIKE_DATA.match(body) else body


__all__ = [
    "CHAT_OUTPUT_TOKENS",
    "FALLBACK_REPLY",
    "INTENT_OUTPUT_TOKENS",
    "MAX_CHAT_WORDS",
    "Intent",
    "classify",
    "describe_capabilities",
    "normalise_request",
    "respond",
    "triage",
]
