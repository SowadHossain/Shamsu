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

# A RAW-payload tool envelope: a fenced block whose FIRST line names a mutation
# tool AND its target path, with everything up to the closing fence written to
# disk verbatim. Nothing inside is JSON, so a 7B never has to escape source code
# — the single largest cause of a lost mutation turn (see _repair_unescaped_quotes
# for the 2026-08-03 incident this exists to make impossible rather than
# recoverable).
#
# Every piece is load-bearing:
#  * `(?P=fence)` requires the closing fence to be the SAME length as the opening
#    one, so a file whose own body contains ``` can be written by opening with
#    four backticks.
#  * The header must be the FIRST line inside the fence (`[ \t]*`, never `\s*`,
#    which would cross newlines under DOTALL) so the payload's byte range is
#    unambiguous.
#  * The tool name is a closed literal alternation, so an ordinary source comment
#    fence such as `# models.py` can never match.
#  * The `[:=]` separator is REQUIRED. That is what keeps the older
#    `# write_file` + JSON-body dialect falling through to the commented-fence
#    salvager untouched.
_RAW_TOOL_FENCE_RE = re.compile(
    r"^[ \t]*(?P<fence>`{3,}|~{3,})[^\r\n]*\r?\n"
    r"[ \t]*(?:\#+|//+|--|<!--|/\*|\*)?[ \t]*"
    r"(?P<name>write_file|append_file|edit_file)"
    r"[ \t]*[:=][ \t]*"
    r"(?P<path>[^\s\r\n]+?)"
    r"[ \t]*(?:-->|\*/)?[ \t]*\r?\n"
    r"(?P<body>.*?)"
    r"^[ \t]*(?P=fence)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
# Markdown-family targets cannot be carried safely by a 3-backtick envelope: the
# non-greedy body stops at the file's OWN first fence line and would write a
# truncated file with no way to detect it after the fact. Require 4+ instead.
_FENCE_UNSAFE_SUFFIXES = (".md", ".mdx", ".markdown")

# --- Quote repair (mutation tool calls only) -------------------------------
# A `"` inside a JSON string can only be the terminator when the next
# non-whitespace character is one of these; anything else means the model forgot
# to escape a literal quote in the payload.
_AFTER_STRING_RE = re.compile(r"[ \t\r\n]*[,}\]:]")
# Only tools whose arguments carry source code get the repair. read_file /
# run_command arguments are bare strings that always parse, so a rewrite there
# could only ever corrupt a value that was already fine.
_QUOTE_REPAIR_TOOLS = frozenset({"write_file", "append_file", "edit_file"})
# Fields whose length decides which candidate reading of an ambiguous payload to
# keep: always prefer the LONGEST, so a wrong terminator cannot silently write a
# truncated file.
_QUOTE_REPAIR_PAYLOAD_KEYS = ("content", "new_string", "old_string")
_TOOL_CALL_SHAPE_RE = re.compile(
    r'"(?:name|action|tool|tool_name|function)"\s*:\s*"(?P<tool>[A-Za-z_][\w-]*)"'
)
_MAX_QUOTE_REPAIR_ATTEMPTS = 16
_MAX_QUOTE_REPAIR_CHARS = 400_000

# `<think>...</think>` reasoning trace (kept out of the visible answer).
_THINK_RE = re.compile(r"<think>(?P<body>.*?)</think>", re.DOTALL | re.IGNORECASE)
# An unmatched, dangling `<think>` with no closing tag: everything after it is
# reasoning that never terminated. Only stripped when there is no closing tag.
_DANGLING_THINK_RE = re.compile(r"<think>(?P<body>.*)\Z", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class ParseFailure:
    """Why a tool-call-shaped span in the content did NOT become a call.

    Exists so a loop can tell an unparseable *attempt* from no attempt at all.
    Before this, both produced the same "the model returned prose" correction —
    a misdiagnosis with teeth: on 2026-08-03 a complete, correct 736-char
    ``write_file`` call for ``templates/my_orders.html`` was discarded because
    the model escaped the opening ``"`` of ``href=\\"{% url ... %}">`` and not
    the closing one. The loop told it to stop returning prose, so at
    temperature 0.1 it re-emitted the identical call three times and the run
    ended with nothing written.

    ``error`` carries the verbatim ``json.loads`` message; a model handed the
    real parse error can fix it, a model told it wrote prose cannot.
    """

    kind: str
    tool: str = ""
    path: str = ""
    error: str = ""
    span_preview: str = ""
    repaired: bool = False


@dataclass(frozen=True)
class ModelTurn:
    """A normalized model turn: the visible answer, the reasoning trace kept out
    of it, and the tool calls parsed from either the native field or salvaged
    from the content."""

    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    salvaged: bool = False
    # Defaults to () so whole-instance equality assertions on an empty turn keep
    # passing (tests/test_model_output_boundary.py compares ModelTurn objects).
    parse_failures: tuple[ParseFailure, ...] = ()

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
    failures: list[ParseFailure] = []
    if allow_salvage:
        # ASYMMETRY, DELIBERATE — do not "tidy" this into one source string.
        # The raw envelope writes its body to disk verbatim, so a block sitting
        # inside a <think> trace would turn the model's musings into a file. It
        # therefore sees think-stripped content. The other salvagers keep seeing
        # raw_content: they parse structured payloads rather than copying bytes,
        # and models that wrap everything in a dangling <think> would otherwise
        # lose real calls.
        raw_fence_source = _strip_think_spans(raw_content)
        for salvager in (
            # Raw first: it is the only form that names the tool AND the path AND
            # delimits the payload, and its body must not be re-interpreted. If
            # _salvage_embedded_json ran first it would brace-scan a .json/.js
            # payload and invent a second call from the file's own contents.
            _salvage_raw_tool_fences,
            _salvage_commented_tool_fences,
            _salvage_embedded_json,
            _salvage_search_replace,
            _salvage_xml_tool_call,
        ):
            source = (
                raw_fence_source
                if salvager is _salvage_raw_tool_fences
                else raw_content
            )
            calls, spans = salvager(source, registered_set, failures)
            if calls:
                salvaged_calls = calls
                salvaged_spans = spans
                break
        if not salvaged_calls and not failures:
            # Nothing parsed and nothing explained it: check for a call that was
            # cut off before its braces closed, so the loop can say so instead
            # of calling a real attempt "prose".
            truncated = _truncated_mutation_failure(raw_content, registered_set)
            if truncated is not None:
                failures.append(truncated)

    text, inline_thinking = _split_thinking(raw_content)
    text = _strip_tool_artifacts(text, salvaged_spans)
    return ModelTurn(
        text=text.strip(),
        thinking=_join_thinking(native_thinking, inline_thinking),
        tool_calls=salvaged_calls,
        salvaged=bool(salvaged_calls),
        parse_failures=tuple(failures),
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


def _strip_think_spans(content: str) -> str:
    """Remove ``<think>`` reasoning, including an unterminated trailing one."""
    return _DANGLING_THINK_RE.sub("", _THINK_RE.sub("", content))


def _is_tool_call_envelope(obj: dict[str, Any], registered: set[str] | None) -> bool:
    """True when a ``{...}`` body is a leaked tool-call envelope, not file content.

    Deliberately keyed on the name being a REGISTERED TOOL, not merely present:
    ``package.json`` has a ``"name"`` field, and treating that as an envelope
    would unwrap a real file into nonsense.
    """
    name = _first_str(obj, _NAME_KEYS)
    return bool(name) and (registered is None or name in registered)


def _salvage_raw_tool_fences(
    content: str,
    registered: set[str] | None,
    failures: list[ParseFailure],
) -> tuple[list[ToolCall], list[str]]:
    """Recover ``# write_file: <path>`` fenced blocks whose body is raw bytes.

    This is the primary mutation channel. The body is copied to disk exactly as
    the model typed it, so there is no escaping step that can fail.
    """
    calls: list[ToolCall] = []
    spans: list[str] = []
    for match in _RAW_TOOL_FENCE_RE.finditer(content):
        name = match.group("name")
        if registered is not None and name not in registered:
            continue
        path = match.group("path")
        fence = match.group("fence")
        if not _looks_like_path(path) or path.startswith(("/", "\\")):
            failures.append(
                ParseFailure(kind="raw_envelope_bad_path", tool=name, path=path)
            )
            continue
        if ".." in re.split(r"[/\\]", path):
            failures.append(
                ParseFailure(kind="raw_envelope_bad_path", tool=name, path=path)
            )
            continue
        if len(fence) == 3 and path.lower().endswith(_FENCE_UNSAFE_SUFFIXES):
            failures.append(
                ParseFailure(
                    kind="raw_envelope_fence_collision",
                    tool=name,
                    path=path,
                    error=(
                        "a markdown target needs a 4-backtick envelope; a "
                        "3-backtick one closes at the file's own first fence"
                    ),
                )
            )
            continue
        # Trailing newlines are normalized, but NOT trailing indentation: the
        # byte range here is exact, so whitespace on the final line is content.
        body = match.group("body").rstrip("\r\n")
        if not body.strip():
            failures.append(
                ParseFailure(kind="raw_envelope_empty_body", tool=name, path=path)
            )
            continue
        body += "\n"

        arguments: dict[str, Any] | None = None
        # A model that wraps the JSON envelope inside the raw fence still gets its
        # call. Complements agent_tools._unwrap_serialized_tool_call, which can
        # only recover `content`; the header path survives here too.
        if body.lstrip().startswith("{"):
            parsed, _error, _repaired = _load_tool_call_json(body, registered)
            if isinstance(parsed, dict) and _is_tool_call_envelope(parsed, registered):
                inner = _first_dict(parsed, _ARG_KEYS)
                if inner:
                    arguments = {"filepath": path, **inner}

        if arguments is None:
            if name == "edit_file":
                # One grammar for the model to learn: the edit body is the
                # SEARCH/REPLACE dialect it already emits, with the path taken
                # from the header instead of guessed from surrounding prose.
                pairs = list(_SEARCH_REPLACE_RE.finditer(body))
                if not pairs:
                    failures.append(
                        ParseFailure(
                            kind="edit_envelope_without_search_replace",
                            tool=name,
                            path=path,
                            error=(
                                "edit_file needs <<<<<<< SEARCH / ======= / "
                                ">>>>>>> REPLACE pairs in the block body"
                            ),
                        )
                    )
                    continue
                for pair in pairs:
                    calls.append(
                        ToolCall(
                            id=f"raw_edit_file_{len(calls) + 1}",
                            name=name,
                            arguments={
                                "filepath": path,
                                "old_string": pair.group("old"),
                                "new_string": pair.group("new"),
                            },
                        )
                    )
                spans.append(match.group(0))
                continue
            arguments = {"filepath": path, "content": body}

        calls.append(
            ToolCall(
                id=f"raw_{name}_{len(calls) + 1}",
                name=name,
                arguments=_normalize_tool_arguments(name, arguments, raw=True),
            )
        )
        spans.append(match.group(0))
    return calls, spans


def _salvage_commented_tool_fences(
    content: str,
    registered: set[str] | None,
    failures: list[ParseFailure],
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
        repaired = False
        error = ""
        if body.startswith("{"):
            parsed, error, repaired = _load_tool_call_json(body, registered)
            if isinstance(parsed, dict):
                arguments = parsed
        elif name == "run_command" and body:
            arguments = {"command": body}
        if arguments is None:
            if name in _QUOTE_REPAIR_TOOLS and error:
                failures.append(
                    ParseFailure(
                        kind="fence_body_not_json",
                        tool=name,
                        path=_probable_filepath(body),
                        error=error,
                        span_preview=body[:240],
                    )
                )
            continue
        if repaired:
            failures.append(
                ParseFailure(
                    kind="quote_repaired",
                    tool=name,
                    path=str(arguments.get("filepath") or ""),
                    error=error,
                    repaired=True,
                )
            )
        calls.append(
            ToolCall(
                id=f"salvaged_{name}_{len(calls) + 1}",
                name=name,
                arguments=_normalize_tool_arguments(name, arguments),
            )
        )
        spans.append(match.group(0))
    return calls, spans


def _salvage_embedded_json(
    content: str,
    registered: set[str] | None,
    failures: list[ParseFailure],
) -> tuple[list[ToolCall], list[str]]:
    """Brace-scan *content* for JSON objects that describe a tool call — even
    inside prose or ``` fences — and map them to registered tools. This is the
    direct fix for the ``{"name": "ask_user", ...}`` leak."""
    calls: list[ToolCall] = []
    spans: list[str] = []
    for span in _iter_json_objects(content):
        obj, error, repaired = _load_tool_call_json(span, registered)
        if not isinstance(obj, dict):
            # A span that named a mutation tool and still would not parse is the
            # failure the loop must report honestly instead of calling it prose.
            tool = _names_quote_repair_tool(span, registered)
            if tool and error:
                failures.append(
                    ParseFailure(
                        kind="json_decode",
                        tool=tool,
                        path=_probable_filepath(span),
                        error=error,
                        span_preview=span[:240],
                    )
                )
            continue
        call = _tool_call_from_obj(obj, registered)
        if call is not None:
            calls.append(call)
            spans.append(span)
            if repaired:
                failures.append(
                    ParseFailure(
                        kind="quote_repaired",
                        tool=call.name,
                        path=str(call.arguments.get("filepath") or ""),
                        error=error,
                        repaired=True,
                    )
                )
    return calls, spans


def _salvage_search_replace(
    content: str,
    registered: set[str] | None,
    failures: list[ParseFailure],
) -> tuple[list[ToolCall], list[str]]:
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


def _salvage_xml_tool_call(
    content: str,
    registered: set[str] | None,
    failures: list[ParseFailure],
) -> tuple[list[ToolCall], list[str]]:
    """Parse ``<tool_call>{...}</tool_call>`` wrappers (some chat templates emit
    tool calls this way)."""
    calls: list[ToolCall] = []
    spans: list[str] = []
    for match in _XML_TOOL_CALL_RE.finditer(content):
        body = match.group("body")
        obj, error, repaired = _load_tool_call_json(body, registered)
        if not isinstance(obj, dict):
            tool = _names_quote_repair_tool(body, registered)
            if tool and error:
                failures.append(
                    ParseFailure(
                        kind="json_decode",
                        tool=tool,
                        path=_probable_filepath(body),
                        error=error,
                        span_preview=body[:240],
                    )
                )
            continue
        call = _tool_call_from_obj(obj, registered)
        if call is not None:
            calls.append(call)
            spans.append(match.group(0))
            if repaired:
                failures.append(
                    ParseFailure(
                        kind="quote_repaired",
                        tool=call.name,
                        path=str(call.arguments.get("filepath") or ""),
                        error=error,
                        repaired=True,
                    )
                )
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


def json_object_from_text(text: str) -> str:
    """Return the JSON object embedded in *text*, or ``""`` when there is none.

    Ollama streams a reasoning model on two channels, and a ``format`` schema
    constrains only the answer one. Asked to both think and fill a schema, a 9B
    will sometimes satisfy the request entirely in the FREE reasoning channel
    and leave the constrained channel empty - so a complete, valid answer is
    generated and then dropped, because nothing ever looks in the other
    channel. Live 2026-08-17: a PRD plan came back as
    ``{"plan_summary": "This plan builds a Browser-Based 3D Asteroid
    Shooter...", ...}`` in `thinking`, and the run died on "planner did not
    return a JSON object".

    Recovering it costs one scan. Regenerating it costs another minute of a
    small model's time and can fail exactly the same way.

    The LARGEST balanced object wins: reasoning text routinely carries small
    fragments (``{"id": "M-001"}``) alongside the real answer.
    """
    body = (text or "").strip()
    if not body:
        return ""
    try:  # Strict first - never let a repair library invent an object from prose.
        whole = json.loads(body)
    except (ValueError, TypeError):
        whole = None
    if isinstance(whole, dict) and whole:
        return body
    best = ""
    for span in _iter_json_objects(body):
        if len(span) <= len(best):
            continue
        parsed = _load_json(span)
        if isinstance(parsed, dict) and parsed:
            best = span
    return best


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


def _greedy_string_repair(
    span: str, force_escape: frozenset[int] = frozenset()
) -> tuple[str, list[int]]:
    """Escape every in-string ``"`` that cannot be a terminator.

    Rule: while inside a JSON string, a ``"`` whose next non-whitespace
    character is not one of ``, } ] :`` cannot close the string, so it must be a
    literal quote the model forgot to escape — emit ``\\"`` and stay in the
    string. Indices in *force_escape* are treated as literal even when they do
    look like terminators, which is how the caller explores the readings where
    the greedy choice was wrong.

    Returns the rewritten text plus the indices this pass ACCEPTED as
    terminators, so the caller knows which choices are open to revision.
    """
    out: list[str] = []
    accepted: list[int] = []
    in_string = False
    index = 0
    length = len(span)
    while index < length:
        char = span[index]
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        # Inside a string: a backslash escape is copied whole so `\"` is never
        # mistaken for a terminator and `\\` never swallows the next quote.
        if char == "\\" and index + 1 < length:
            out.append(span[index : index + 2])
            index += 2
            continue
        if char == '"':
            if index not in force_escape and _AFTER_STRING_RE.match(span, index + 1):
                out.append(char)
                accepted.append(index)
                in_string = False
            else:
                out.append('\\"')
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out), accepted


def _payload_length(obj: Any) -> int:
    """Total length of the code-carrying values in a parsed tool call.

    Used to choose between competing readings of an ambiguous payload.
    """
    if not isinstance(obj, dict):
        return 0
    total = 0
    for candidate in (obj, *(value for value in obj.values() if isinstance(value, dict))):
        for key in _QUOTE_REPAIR_PAYLOAD_KEYS:
            value = candidate.get(key)
            if isinstance(value, str):
                total += len(value)
    return total


def _repair_unescaped_quotes(span: str) -> dict[str, Any] | None:
    """Recover a tool call whose code payload under-escaped its double quotes.

    This is the 2026-08-03 failure: one missing backslash in a 736-char
    ``write_file`` call discarded the whole mutation, and at temperature 0.1 the
    retry reproduced it byte for byte.

    Every candidate reading is validated by ``json.loads``, so this can only
    ever return structurally valid JSON — never a half-parsed dict. When more
    than one reading parses, the one with the LONGEST payload wins: a wrong
    terminator shortens the content, and silently writing a truncated file is
    far worse than failing loudly.
    """
    if len(span) > _MAX_QUOTE_REPAIR_CHARS:
        return None
    candidates: list[dict[str, Any]] = []
    repaired, accepted = _greedy_string_repair(span)
    first = _try_json_object(repaired)
    if first is not None:
        candidates.append(first)
    # A parse failure means one of the accepted terminators was really a literal
    # quote. Re-run forcing each in turn; escaping an earlier one EXTENDS the
    # payload, which is the direction we want to explore.
    for forced in accepted[:_MAX_QUOTE_REPAIR_ATTEMPTS]:
        retry, _ = _greedy_string_repair(span, frozenset({forced}))
        parsed = _try_json_object(retry)
        if parsed is not None:
            candidates.append(parsed)
    if not candidates:
        return None
    return max(candidates, key=_payload_length)


def _try_json_object(span: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(span)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _names_quote_repair_tool(span: str, registered: set[str] | None) -> str:
    """The mutation tool this span claims to call, or ``""``.

    Gates the quote repair: it must not run on arbitrary JSON, only on a span
    that already identifies itself as a code-carrying tool call.
    """
    match = _TOOL_CALL_SHAPE_RE.search(span)
    if match is None:
        return ""
    tool = match.group("tool")
    if tool not in _QUOTE_REPAIR_TOOLS:
        return ""
    if registered is not None and tool not in registered:
        return ""
    return tool


def _truncated_mutation_failure(
    content: str, registered: set[str] | None
) -> ParseFailure | None:
    """Report a mutation call that was cut off mid-payload.

    A truncated call never balances its braces, so ``_iter_json_objects`` skips
    it entirely and no salvager ever sees it — meaning the loop would fall
    through to "the model returned prose" for output that was in fact a correct
    call the model ran out of room to finish. That is a distinct diagnosis with a
    distinct fix (raise the output budget), so it gets its own failure kind
    rather than being folded into the escaping case.
    """
    balanced = list(_iter_json_objects(content))
    for match in _TOOL_CALL_SHAPE_RE.finditer(content):
        tool = match.group("tool")
        if tool not in _QUOTE_REPAIR_TOOLS:
            continue
        if registered is not None and tool not in registered:
            continue
        # Inside a balanced object? Then it was parsed (or reported) already.
        if any(span in content and match.start() >= content.find(span)
               and match.end() <= content.find(span) + len(span)
               for span in balanced):
            continue
        return ParseFailure(
            kind="json_truncated",
            tool=tool,
            path=_probable_filepath(content),
            error="tool call ended mid-payload: the JSON never closed",
            span_preview=content[match.start() : match.start() + 240],
        )
    return None


def _probable_filepath(span: str) -> str:
    """Best-effort target path out of a span that failed to parse.

    The correction shown to the model is far more actionable when it names the
    file ("send a raw block for templates/my_orders.html") than when it says
    "<path>". Deliberately tolerant: this is only ever used for prompt text, so
    a wrong guess costs nothing and is preferable to no guess.
    """
    for key in ("filepath", "file_path", "path"):
        match = re.search(rf'"{key}"\s*:\s*"(?P<value>[^"\\]{{1,200}})"', span)
        if match:
            return match.group("value")
    return ""


def _load_tool_call_json(
    span: str, registered: set[str] | None
) -> tuple[Any, str, bool]:
    """``_load_json`` plus a mutation-aware quote-repair tier.

    Returns ``(parsed, strict_error, repaired)``. *strict_error* is the verbatim
    ``json.loads`` message, which the loop shows the model — a model handed the
    real error can fix it.

    Order is load-bearing: for a span naming a mutation tool the targeted repair
    runs BEFORE generic ``json_repair``, because ``json_repair`` "succeeds" on an
    under-escaped code payload by TRUNCATING the string at the stray quote. That
    yields a plausible-looking dict that would write half a file, with no error
    to report. The targeted repair keeps the whole payload or fails loudly.
    """
    try:
        return json.loads(span), "", False
    except (ValueError, TypeError) as exc:
        error = str(exc)
    # Same Python-apostrophe preservation as _load_json, applied before the
    # quote repair so a payload with both `\'` and a stray `"` needs one pass.
    apostrophe_safe = re.sub(r"(?<!\\)\\'", r"\\\\'", span)
    if apostrophe_safe != span:
        try:
            return json.loads(apostrophe_safe), "", False
        except (ValueError, TypeError):
            pass
    if _names_quote_repair_tool(span, registered):
        recovered = _repair_unescaped_quotes(apostrophe_safe)
        if recovered is not None:
            return recovered, error, True
    return _load_json(span), error, False


def _coerce_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = _load_json(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalize_tool_arguments(
    name: str, arguments: dict[str, Any], *, raw: bool = False
) -> dict[str, Any]:
    """Repair one extra layer of JSON escaping in model-authored file payloads.

    Some local models return a decoded tool-call object but leave file content
    with literal ``\\n`` separators. Writing that value verbatim produces a
    one-line, invalid source file and traps the model in read/edit retries.
    Only payloads with no real line breaks are repaired, and escape sequences
    inside quoted source strings are preserved.

    ``raw=True`` skips the repair entirely, for payloads that never went through
    JSON at all (the raw fenced envelope). Their bodies are already physical
    bytes, and a one-line JS/JSON file whose string contains a literal ``\\n``
    satisfies this function's trigger condition exactly — "no real newline, has
    a ``\\n``" — so running it would corrupt the very files it exists to fix.
    """
    if raw:
        return arguments
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
