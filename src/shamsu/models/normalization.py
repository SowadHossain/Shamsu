"""Normalising model output before it meets its contract.

Migrated from `legacy-code/shamsu/llm/output.py` — and the migration is mostly
a *deletion*. That file is 1,159 lines with six salvage strategies, greedy
string repair, and quote-repair passes that tried to reconstruct what the model
probably meant. Plan §8.4 names that class of code as something not to carry
forward, and the contract module already states why: a response that does not
satisfy its contract is a failure, not something to be creatively
reinterpreted.

So this module draws a line that v1 never did.

**Normalisation removes wrapping. It never edits content.**

Removing wrapping is deterministic and information-preserving: a `<think>`
span is not part of the answer, a ```json fence is not part of the JSON, and
prose around a balanced object is not part of the object. Every one of those
transformations has exactly one correct result.

Editing content is guessing. Repairing an unescaped quote means deciding what
the model meant to say, and being wrong there produces a *parseable* wrong
answer — which is worse than a parse failure, because a parse failure is
visible and a silently wrong argument is not.

What is kept from v1 is the balanced-brace scanner, including the fix behind
it: a single left-to-right scan gives up the moment depth stops returning to
zero, so one truncated code fence swallowed everything after it. A real 7B
reply once held a cut-off Python fence followed by a valid call, and the call
was lost. The scanner restarts past an unterminated opening brace instead.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: Reasoning spans emitted by small local models. Removed because they are not
#: the answer -- and removed *first*, since a `<think>` block routinely
#: contains a draft JSON object that would otherwise be parsed instead of the
#: real one.
_THINK = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)
_DANGLING_THINK = re.compile(r"<think\b[^>]*>.*\Z", re.DOTALL | re.IGNORECASE)

#: A fenced block, with or without a language tag.
_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\s*\n(?P<body>.*?)\n?```", re.DOTALL)


@dataclass(frozen=True)
class Normalised:
    """The result of normalising one model response.

    `changed` and `steps` exist so a caller can tell whether the model produced
    clean output or output that needed unwrapping. That distinction is a
    quality signal about the model, and v1 discarded it — which is part of why
    nobody could tell whether the salvage layer was still needed.
    """

    text: str
    steps: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.steps)


def normalise(text: str) -> Normalised:
    """Strip reasoning spans and code fences from a model response.

    Applied in order, because the order matters: a `<think>` block often
    contains a draft fenced block, and unwrapping before stripping would
    promote the draft over the answer.
    """
    steps: list[str] = []
    working = text

    stripped = _DANGLING_THINK.sub("", _THINK.sub("", working))
    if stripped != working:
        steps.append("removed reasoning spans")
        working = stripped

    unwrapped = _unwrap_fence(working)
    if unwrapped is not None:
        steps.append("unwrapped a code fence")
        working = unwrapped

    working = working.strip()
    if working != text:
        return Normalised(text=working, steps=tuple(steps) or ("trimmed whitespace",))
    return Normalised(text=working)


def _unwrap_fence(text: str) -> str | None:
    """The body of the only fenced block, or None.

    Only when there is exactly one. Two fences means the response contains
    something this module cannot disambiguate, and picking one would be a
    guess — which is the thing this module exists not to do.
    """
    matches = _FENCE.findall(text)
    if len(matches) != 1:
        return None
    body = matches[0].strip()
    return body or None


def extract_json_object(text: str) -> str | None:
    """The single balanced JSON object in `text`, or None.

    None when there are none *and* when there is more than one: two candidate
    objects is ambiguity, and resolving it by taking the first is how a
    model's draft gets parsed instead of its answer.
    """
    objects = iter_json_objects(text)
    return objects[0] if len(objects) == 1 else None


def iter_json_objects(text: str) -> list[str]:
    """Every balanced `{...}` span, outermost only, respecting string quoting.

    Braces inside JSON strings do not affect depth. An unterminated object
    earlier in the text does not hide a complete one after it: the scan
    restarts just past the offending brace rather than giving up, which is the
    v1 fix worth carrying forward.
    """
    objects: list[str] = []
    start = 0
    while start < len(text):
        found, unterminated = _scan(text, start)
        objects.extend(found)
        if unterminated < 0:
            break
        start = unterminated + 1
    return objects


def _scan(text: str, start: int) -> tuple[list[str], int]:
    """Scan from `start`.

    Returns the balanced outermost objects found, and the index of the first
    opening brace left unterminated (-1 when everything closed).
    """
    objects: list[str] = []
    depth = 0
    span_start = -1
    in_string = False
    escape = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                span_start = index
            depth += 1
        elif char == "}" and depth > 0:
            depth -= 1
            if depth == 0 and span_start >= 0:
                objects.append(text[span_start : index + 1])
                span_start = -1

    return objects, span_start if depth > 0 else -1


def parse_json_response(text: str) -> tuple[dict[str, Any] | None, str]:
    """Normalise, locate the JSON object, and parse it exactly once.

    Returns `(payload, reason)`. `payload` is None on any failure and `reason`
    says which one, in words a model can act on — that string goes back into
    the next frame, so "expected a JSON object" beats "parse error".

    There is deliberately no retry, no repair, and no second attempt with
    different rules. A response that is not valid JSON after wrapping is
    removed is a contract failure, and the runtime decides what that means.
    """
    normalised = normalise(text)
    if not normalised.text:
        return None, "the response was empty"

    candidate = normalised.text if normalised.text.startswith("{") else None
    if candidate is None:
        objects = iter_json_objects(normalised.text)
        if not objects:
            return None, "no JSON object was found in the response"
        if len(objects) > 1:
            return None, (
                f"the response contained {len(objects)} JSON objects; respond with exactly one"
            )
        candidate = objects[0]

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return None, f"the JSON object could not be parsed: {exc.msg} at position {exc.pos}"

    if not isinstance(payload, dict):
        # A type guard rather than an expected path: every candidate here
        # started with `{`, so it parsed to a dict or raised. Kept because
        # `json.loads` returns `Any`, and the alternative is asserting a shape
        # the type system was never shown.
        return None, f"expected a JSON object, got {type(payload).__name__}"

    return payload, ""


def strip_reasoning(text: str) -> str:
    """Remove `<think>` spans only. For display, where fences are meaningful."""
    return _DANGLING_THINK.sub("", _THINK.sub("", text)).strip()


__all__ = [
    "Normalised",
    "extract_json_object",
    "iter_json_objects",
    "normalise",
    "parse_json_response",
    "strip_reasoning",
]
