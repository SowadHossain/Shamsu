"""Model I/O boundary — the single place a raw model response becomes a
normalized :class:`ModelTurn`.

SHAMSU targets models that emit clean structured output (native ``tool_calls``,
byte-perfect unified diffs, well-formed JSON), but runs on 4-7B local models
that frequently don't. Instead of scattering per-symptom band-aids across each
loop (raw ``{"name": "ask_user", ...}`` printed to chat, ``<<<<<<< SEARCH``
blocks dumped instead of applied, ``<think>`` reasoning leaking into answers),
every loop funnels its raw response through :func:`parse_model_turn`, which:

1. Reads native ``message.tool_calls`` when present (preferred, ``salvaged=False``).
2. Otherwise runs a **salvage cascade** over the text content — embedded JSON,
   SEARCH/REPLACE blocks, ``<tool_call>`` XML — stopping at the first form that
   yields at least one call to a *registered* tool.
3. Splits reasoning (``<think>...</think>`` or the Ollama ``thinking`` field)
   out of the visible answer.
4. Strips any leaked tool syntax from the visible text, so the UI never shows
   raw JSON / diff markers even when a real call was also parsed.

This module owns tool-syntax parsing so the loops don't; it is deliberately
free of I/O and easy to unit-test.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from shamsu.types import ToolCall

# Keys a small model uses, interchangeably, for the tool name and its arguments
# when it emits a tool call as plain JSON instead of a native tool_call.
_NAME_KEYS = ("name", "action", "tool", "tool_name", "function")
_ARG_KEYS = ("arguments", "parameters", "args", "params", "input")

# SEARCH/REPLACE conflict-marker block (aider / many small-model edit formats).
# Captures the OLD side and the NEW side; the file path is recovered separately
# from the text immediately preceding the block.
_SEARCH_REPLACE_RE = re.compile(
    r"<{5,}\s*SEARCH\s*?\n(?P<old>.*?)\n={5,}\s*?\n(?P<new>.*?)\n>{5,}\s*REPLACE",
    re.DOTALL,
)

# `<tool_call>{...}</tool_call>` wrappers some chat templates emit.
_XML_TOOL_CALL_RE = re.compile(r"<tool_call>\s*(?P<body>\{.*?\})\s*</tool_call>", re.DOTALL)
# A fabricated tool RESULT echoed by the model (qwen's chat template wraps real
# results in these tags, so the model reproduces them). Non-greedy body; the
# closing tag is optional because echoes are often truncated mid-JSON.
_TOOL_RESPONSE_RE = re.compile(r"<tool_response>\s*\{.*?(?:\}\s*</tool_response>|\}\s*$|$)", re.DOTALL)
_COMMENTED_TOOL_FENCE_RE = re.compile(
    r"```[^\r\n]*\r?\n\s*#\s*(?P<name>[A-Za-z_][\w-]*)\b(?P<body>.*?)```",
    re.DOTALL,
)

# `<think>...</think>` reasoning trace (kept out of the visible answer).
_THINK_RE = re.compile(r"<think>(?P<body>.*?)</think>", re.DOTALL | re.IGNORECASE)
# An unmatched, dangling `<think>` with no closing tag: everything after it is
# reasoning that never terminated. Only stripped when there is no closing tag.
_DANGLING_THINK_RE = re.compile(r"<think>(?P<body>.*)\Z", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ModelTurn:
    """A normalized model turn: the visible answer, the reasoning trace kept out
    of it, and the tool calls parsed from either the native field or salvaged
    from the content."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    salvaged: bool = False

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


def parse_model_turn(
    response: Any,
    registered: Iterable[str] | None = None,
    *,
    allow_salvage: bool = True,
) -> ModelTurn:
    """Normalize a raw Ollama chat *response* into a :class:`ModelTurn`.

    ``registered`` is the set of tool names the caller can actually execute; it
    gates *salvaged* calls (a name that isn't registered is treated as an example
    in prose, not a real call) but not native ``tool_calls`` (those are passed
    through so the loop can surface an honest "unknown tool" result). Pass
    ``allow_salvage=False`` to disable the content salvage cascade entirely.
    """
    registered_set = {str(name) for name in registered} if registered is not None else None
    message = _message_of(response)
    raw_content = str(_get(message, "content", "") or "")
    native_thinking = str(_get(message, "thinking", "") or "").strip()

    native_calls = _native_tool_calls(message)
    if native_calls:
        text, inline_thinking = _split_thinking(raw_content)
        text = _strip_tool_artifacts(text, [])
        return ModelTurn(
            text=text.strip(),
            thinking=_join_thinking(native_thinking, inline_thinking),
            tool_calls=native_calls,
            salvaged=False,
        )

    # No native calls — try to salvage calls from the text, stopping at the first
    # form that yields at least one valid (registered) call.
    salvaged_calls: list[ToolCall] = []
    salvaged_spans: list[str] = []
    if allow_salvage:
        for salvager in (
            _salvage_commented_tool_fences,
            _salvage_embedded_json,
            _salvage_search_replace,
            _salvage_xml_tool_call,
        ):
            calls, spans = salvager(raw_content, registered_set)
            if calls:
                salvaged_calls = calls
                salvaged_spans = spans
                break

    text, inline_thinking = _split_thinking(raw_content)
    text = _strip_tool_artifacts(text, salvaged_spans)
    return ModelTurn(
        text=text.strip(),
        thinking=_join_thinking(native_thinking, inline_thinking),
        tool_calls=salvaged_calls,
        salvaged=bool(salvaged_calls),
    )


# ---------------------------------------------------------------------------
# Native tool calls
# ---------------------------------------------------------------------------


def _native_tool_calls(message: Any) -> list[ToolCall]:
    calls = _get(message, "tool_calls", []) or []
    out: list[ToolCall] = []
    for call in calls:
        function = _get(call, "function", {}) or {}
        name = str(_get(function, "name", "") or "")
        if not name:
            continue
        out.append(
            ToolCall(
                id=str(_get(call, "id", "") or name),
                name=name,
                arguments=_normalize_tool_arguments(
                    name,
                    _coerce_args(_get(function, "arguments", {})),
                ),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Salvage cascade
# ---------------------------------------------------------------------------


def _salvage_commented_tool_fences(
    content: str,
    registered: set[str] | None,
) -> tuple[list[ToolCall], list[str]]:
    """Recover explicit ``# write_file``/``# run_command`` fenced calls.

    Qwen coder models commonly emit this dialect after being told to call a
    tool. The comment must name a registered tool, so ordinary source fences
    such as ``# models.py`` remain visible code and are never executed.
    """
    calls: list[ToolCall] = []
    spans: list[str] = []
    for match in _COMMENTED_TOOL_FENCE_RE.finditer(content):
        name = match.group("name")
        if registered is not None and name not in registered:
            continue
        body = match.group("body").strip()
        arguments: dict[str, Any] | None = None
        parsed = _load_json(body) if body.startswith("{") else None
        if isinstance(parsed, dict):
            arguments = parsed
        elif name == "run_command" and body:
            arguments = {"command": body}
        if arguments is None:
            continue
        calls.append(
            ToolCall(
                id=f"salvaged_{name}_{len(calls) + 1}",
                name=name,
                arguments=_normalize_tool_arguments(name, arguments),
            )
        )
        spans.append(match.group(0))
    return calls, spans


def _salvage_embedded_json(content: str, registered: set[str] | None) -> tuple[list[ToolCall], list[str]]:
    """Brace-scan *content* for JSON objects that describe a tool call — even
    inside prose or ``` fences — and map them to registered tools. This is the
    direct fix for the ``{"name": "ask_user", ...}`` leak."""
    calls: list[ToolCall] = []
    spans: list[str] = []
    for span in _iter_json_objects(content):
        obj = _load_json(span)
        if not isinstance(obj, dict):
            continue
        call = _tool_call_from_obj(obj, registered)
        if call is not None:
            calls.append(call)
            spans.append(span)
    return calls, spans


def _salvage_search_replace(content: str, registered: set[str] | None) -> tuple[list[ToolCall], list[str]]:
    """Turn ``<<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE`` blocks into
    ``edit_file`` calls, recovering the target path from the text just before
    each block."""
    if registered is not None and "edit_file" not in registered:
        return [], []
    calls: list[ToolCall] = []
    spans: list[str] = []
    for match in _SEARCH_REPLACE_RE.finditer(content):
        filepath = _path_before(content, match.start())
        if not filepath:
            continue
        old_string = match.group("old")
        new_string = match.group("new")
        calls.append(
            ToolCall(
                id="salvaged_edit_file",
                name="edit_file",
                arguments={
                    "filepath": filepath,
                    "old_string": old_string,
                    "new_string": new_string,
                },
            )
        )
        spans.append(match.group(0))
    return calls, spans


def _salvage_xml_tool_call(content: str, registered: set[str] | None) -> tuple[list[ToolCall], list[str]]:
    """Parse ``<tool_call>{...}</tool_call>`` wrappers (some chat templates emit
    tool calls this way)."""
    calls: list[ToolCall] = []
    spans: list[str] = []
    for match in _XML_TOOL_CALL_RE.finditer(content):
        obj = _load_json(match.group("body"))
        if not isinstance(obj, dict):
            continue
        call = _tool_call_from_obj(obj, registered)
        if call is not None:
            calls.append(call)
            spans.append(match.group(0))
    return calls, spans


def _tool_call_from_obj(obj: dict[str, Any], registered: set[str] | None) -> ToolCall | None:
    """Map a parsed JSON object to a :class:`ToolCall` when it names a registered
    tool and carries an arguments dict. Conservative on purpose: a JSON example
    in an answer must not be mistaken for a real call (see the salvager
    false-positive risk in the reliability design)."""
    name = _first_str(obj, _NAME_KEYS)
    # OpenAI-style {"function": {"name": ..., "arguments": {...}}}.
    if not name and isinstance(obj.get("function"), dict):
        inner = obj["function"]
        name = _first_str(inner, ("name",))
        obj = {**obj, **inner}
    if not name:
        return None
    if registered is not None and name not in registered:
        return None
    args = _first_dict(obj, _ARG_KEYS)
    if args is None:
        # Allow a bare ``{"name": "git_status"}`` only when the object has no
        # keys beyond the name/label — i.e. it is clearly a call, not prose that
        # merely mentions a tool. Missing args default to {}.
        extra = set(obj) - set(_NAME_KEYS)
        if extra:
            return None
        args = {}
    return ToolCall(
        id=f"salvaged_{name}",
        name=name,
        arguments=_normalize_tool_arguments(name, args),
    )


# ---------------------------------------------------------------------------
# Thinking / text cleaning
# ---------------------------------------------------------------------------


def _split_thinking(content: str) -> tuple[str, str]:
    """Return ``(visible_text, thinking)`` by pulling ``<think>...</think>``
    (and a dangling unclosed ``<think>``) out of the content."""
    thoughts: list[str] = []

    def _capture(match: re.Match[str]) -> str:
        thoughts.append(match.group("body").strip())
        return ""

    text = _THINK_RE.sub(_capture, content)
    dangling = _DANGLING_THINK_RE.search(text)
    if dangling:
        thoughts.append(dangling.group("body").strip())
        text = text[: dangling.start()]
    return text, "\n\n".join(part for part in thoughts if part).strip()


def _strip_tool_artifacts(text: str, salvaged_spans: Iterable[str]) -> str:
    """Remove salvaged tool syntax (and any ``<tool_call>`` wrappers) from the
    visible answer so the UI never shows raw JSON / diff markers."""
    for span in salvaged_spans:
        text = text.replace(span, "")
    text = _XML_TOOL_CALL_RE.sub("", text)
    # Remove any orphan <tool_call>/</tool_call> tags left after the inner JSON
    # body was stripped by an earlier salvager.
    text = re.sub(r"</?tool_call\s*>", "", text)
    # Qwen-family models are TRAINED on <tool_response> wrappers, and echo
    # fabricated ones as their answer ('<tool_response>{"ok": true, "message":
    # "Overwrote..."}' - observed live, presented as if a tool had run). An
    # echoed result is never a real answer: strip the spans, so a turn that was
    # ONLY echo becomes empty and trips the loop's empty-response correction
    # instead of standing as a final answer that claims fake success.
    text = _TOOL_RESPONSE_RE.sub("", text)
    text = re.sub(r"</?tool_response\s*>", "", text)
    text = _strip_empty_fences(text)
    text = _collapse_blank_lines(text)
    # A turn whose entire visible answer is fence markers (e.g. a single
    # unpaired "```" left behind once its body was salvaged - observed live on
    # the light tier) is not an answer. Return empty so the loop's
    # empty-response correction fires instead of the user seeing "```".
    if _is_fence_only(text):
        return ""
    return text


def _is_fence_only(text: str) -> bool:
    """True when every non-blank line is a bare fence marker (```` ``` ````,
    optionally with a language tag). Real answers that merely CONTAIN an
    unclosed fence keep their non-fence lines and stay untouched."""
    non_blank = [line.strip() for line in text.splitlines() if line.strip()]
    return bool(non_blank) and all(
        re.fullmatch(r"`{3,}[\w+-]*", line) for line in non_blank
    )


def _strip_empty_fences(text: str) -> str:
    """Drop ``` fences left empty after their tool-JSON body was stripped."""
    lines = text.splitlines()
    out: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if line.strip().startswith("```"):
            # Look ahead: a fence whose body is now only whitespace is dropped.
            close = idx + 1
            while close < len(lines) and not lines[close].strip().startswith("```"):
                close += 1
            body = [lines[j] for j in range(idx + 1, min(close, len(lines)))]
            if close < len(lines) and all(not b.strip() for b in body):
                idx = close + 1
                continue
        out.append(line)
        idx += 1
    return "\n".join(out)


def _collapse_blank_lines(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n")


def _join_thinking(*parts: str) -> str:
    return "\n\n".join(part.strip() for part in parts if part and part.strip()).strip()


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _iter_json_objects(text: str) -> list[str]:
    """Yield balanced ``{...}`` substrings from *text*, respecting JSON string
    quoting/escapes so braces inside strings don't break the scan. Only the
    outermost object of each nesting is returned.

    An unbalanced ``{`` earlier in the text must not hide a real tool call
    later in it. A single scan gives up the moment depth stops returning to
    zero, so one truncated code fence swallowed everything after it: live
    2026-08-01, a 7B repair reply held a cut-off ``TEMPLATES = [{...`` Python
    fence followed by a valid ``{"name": "run_command", ...}`` call, and the
    call was lost - the loop saw no tool calls, nagged, and the milestone
    failed with the correct fix sitting in the reply. So when a scan ends
    inside an unterminated object, restart just past that opening brace and
    keep recovering.
    """
    objects: list[str] = []
    search_from = 0
    while search_from < len(text):
        found, unmatched = _scan_json_objects(text, search_from)
        objects.extend(found)
        if unmatched < 0:
            break
        search_from = unmatched + 1
    return objects


def _scan_json_objects(text: str, start: int) -> tuple[list[str], int]:
    """Scan from *start*; return (balanced outermost objects, index of the
    first opening brace left unterminated, or -1 when everything closed)."""
    objects: list[str] = []
    depth = 0
    span_start = -1
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            if depth == 0:
                span_start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and span_start >= 0:
                    objects.append(text[span_start : i + 1])
                    span_start = -1
    return objects, span_start if depth > 0 else -1


def _load_json(span: str) -> Any:
    try:
        return json.loads(span)
    except (ValueError, TypeError):
        pass
    # Small coding models often emit Python apostrophe escapes (\') directly
    # inside a JSON string. JSON does not recognize that escape, and generic
    # repair libraries commonly drop the backslash, silently turning valid
    # intended Python into a syntax error. Preserve it as a literal backslash
    # before using broader JSON repair.
    apostrophe_safe = re.sub(r"(?<!\\)\\'", r"\\\\'", span)
    if apostrophe_safe != span:
        try:
            return json.loads(apostrophe_safe)
        except (ValueError, TypeError):
            pass
    try:  # Repair the near-miss JSON small models routinely emit.
        from json_repair import repair_json

        return repair_json(span, return_objects=True)
    except Exception:
        return None


def _coerce_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = _load_json(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_tool_arguments(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Repair one extra layer of JSON escaping in model-authored file payloads.

    Some local models return a decoded tool-call object but leave file content
    with literal ``\\n`` separators. Writing that value verbatim produces a
    one-line, invalid source file and traps the model in read/edit retries.
    Only payloads with no real line breaks are repaired, and escape sequences
    inside quoted source strings are preserved.
    """
    fields = {
        "write_file": ("content",),
        "append_file": ("content",),
        "edit_file": ("old_string", "new_string"),
    }.get(name, ())
    if not fields:
        return arguments
    normalized = dict(arguments)
    for field_name in fields:
        value = normalized.get(field_name)
        if isinstance(value, str) and "\n" not in value and "\\n" in value:
            normalized[field_name] = _decode_escaped_layout(value)
    return normalized


def _decode_escaped_layout(value: str) -> str:
    """Decode escaped layout outside quoted source literals.

    This keeps code such as ``print("\\n")`` intact while turning
    ``import os\\nprint(1)`` into two physical lines.
    """
    out: list[str] = []
    quote = ""
    escaped = False
    index = 0
    while index < len(value):
        char = value[index]
        if quote:
            if char == "\\" and index + 1 < len(value) and value[index + 1] == "\\":
                out.append("\\")
                index += 2
                escaped = False
                continue
            out.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            index += 1
            continue
        if char in {"'", '"', "`"}:
            quote = char
            out.append(char)
            index += 1
            continue
        if value.startswith("\\r\\n", index):
            out.append("\n")
            index += 4
            continue
        if value.startswith("\\n", index):
            out.append("\n")
            index += 2
            continue
        if value.startswith("\\t", index):
            out.append("\t")
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _first_str(obj: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _first_dict(obj: dict[str, Any], keys: Iterable[str]) -> dict[str, Any] | None:
    for key in keys:
        value = obj.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = _load_json(value)
            if isinstance(parsed, dict):
                return parsed
    return None


def _path_before(content: str, block_start: int) -> str:
    """Find the file path a SEARCH/REPLACE block applies to: the nearest
    non-empty line before the block that reads like a workspace path (optionally
    on a ``` fence header or after a label like ``File:``)."""
    preceding = content[:block_start].splitlines()
    for line in reversed(preceding):
        candidate = line.strip()
        if not candidate:
            continue
        candidate = candidate.lstrip("`").strip()
        candidate = re.sub(r"^(?:file|path|filename)\s*[:=]\s*", "", candidate, flags=re.IGNORECASE)
        candidate = candidate.strip("`*:# ").strip()
        if _looks_like_path(candidate):
            return candidate
        # Stop at the first substantive non-path line so we don't reach across a
        # paragraph of prose for a stray path.
        return ""
    return ""


def _looks_like_path(text: str) -> bool:
    if not text or " " in text.strip():
        return False
    if len(text) > 200:
        return False
    return "/" in text or "\\" in text or bool(re.search(r"\.\w{1,8}$", text))


def tool_call_to_message_dict(call: ToolCall) -> dict[str, Any]:
    """Render a :class:`ToolCall` back into the ``{"id", "function": {...}}``
    dict shape the loops / Ollama message history expect. Shared by every loop so
    there is one round-trip, not one per loop."""
    return {
        "id": call.id or call.name,
        "function": {"name": call.name, "arguments": call.arguments},
    }


def strip_reasoning(content: str) -> str:
    """Return *content* with any ``<think>...</think>`` reasoning removed — the
    visible text only. For non-tool outputs (e.g. a unified diff) where a
    reasoning model would otherwise corrupt the payload with an inline trace."""
    text, _thinking = _split_thinking(content or "")
    return text.strip()


def _message_of(response: Any) -> Any:
    return _get(response, "message", response)


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
