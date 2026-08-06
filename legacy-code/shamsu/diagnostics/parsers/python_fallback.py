"""Small fallback parser for plain-text Python tracebacks."""
from __future__ import annotations

import re

from shamsu.diagnostics.types import DiagnosticRecord

FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
SYNTAX_FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+)\s*$')
FINAL_EXCEPTION_RE = re.compile(r"^(?P<etype>[A-Za-z_][\w.]*(?:Error|Exception|Warning)):\s*(?P<message>.*)$")

VENDOR_MARKERS = ("site-packages", "/.venv/", "\\.venv\\", "\\Lib\\", "/lib/python")
SYNTAX_EXCEPTION_TYPES = {"SyntaxError", "IndentationError", "TabError"}


def _is_user_frame(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/")
    return not any(marker.replace("\\", "/") in normalized for marker in VENDOR_MARKERS)


def parse_python_traceback(text: str) -> list[DiagnosticRecord]:
    lines = text.splitlines()
    syntax_records = _parse_syntax_error_block(lines)
    if syntax_records:
        return syntax_records

    frames: list[tuple[str, int, str]] = []
    final_exception: tuple[str, str] | None = None

    for index, line in enumerate(lines):
        frame_match = FRAME_RE.match(line)
        if frame_match:
            frames.append((frame_match.group("file"), int(frame_match.group("line")), frame_match.group("func")))
            continue
        exc_match = FINAL_EXCEPTION_RE.match(line.strip())
        if exc_match and _looks_like_traceback_context(lines, index):
            final_exception = (exc_match.group("etype"), exc_match.group("message").strip())

    if not final_exception:
        return []

    user_frames = [frame for frame in frames if _is_user_frame(frame[0])]
    relevant_frame = user_frames[-1] if user_frames else (frames[-1] if frames else ("", 0, ""))
    etype, message = final_exception
    file_path, line_no, func = relevant_frame

    return [
        DiagnosticRecord(
            tool="python",
            language="python",
            severity="error",
            category="exception",
            code=etype,
            message=message,
            file=file_path,
            line=line_no or None,
            symbol=func,
            stack_frame=f"{file_path}:{line_no} in {func}" if file_path else "",
            related_locations=[f"{f}:{ln} in {fn}" for f, ln, fn in frames if f != file_path],
            raw_excerpt=f"{etype}: {message}",
            parser_name="python_fallback",
            confidence=0.85,
        )
    ]


_TRACEBACK_HEADER = "Traceback (most recent call last)"
_CHAIN_MARKERS = (
    "During handling of the above exception, another exception occurred",
    "The above exception was the direct cause of the following exception",
)


def _looks_like_traceback_context(lines: list[str], index: int) -> bool:
    """Only treat an `Error: message` line as the *final* exception if it
    follows a Traceback block, not an arbitrary log line mentioning "Error".

    Walks back through the traceback body instead of a fixed-size window: a
    Django `manage.py check` settings failure puts 25+ frame lines between the
    header and the final NameError, and a window that stops short leaves the
    whole run with zero diagnostics (observed live 2026-08-01)."""
    for line in reversed(lines[:index]):
        if _TRACEBACK_HEADER in line:
            return True
        stripped = line.strip()
        if not stripped:
            continue
        # Frame headers, indented source/caret lines, chained-exception
        # markers, and intermediate exception lines are all traceback body;
        # anything else means we left the block without finding its header.
        if line[:1].isspace():
            continue
        if FRAME_RE.match(line) or SYNTAX_FRAME_RE.match(line):
            continue
        if FINAL_EXCEPTION_RE.match(stripped) or any(marker in line for marker in _CHAIN_MARKERS):
            continue
        return False
    return False


def _parse_syntax_error_block(lines: list[str]) -> list[DiagnosticRecord]:
    for index, line in enumerate(lines):
        frame_match = SYNTAX_FRAME_RE.match(line)
        if not frame_match:
            continue
        exception_index = _next_exception_line(lines, index + 1)
        if exception_index is None:
            continue
        exc_match = FINAL_EXCEPTION_RE.match(lines[exception_index].strip())
        if not exc_match:
            continue
        etype = exc_match.group("etype")
        if etype not in SYNTAX_EXCEPTION_TYPES:
            continue

        file_path = frame_match.group("file")
        line_no = int(frame_match.group("line"))
        message = exc_match.group("message").strip()
        raw_lines = lines[index : exception_index + 1]
        caret_line = next((item for item in raw_lines if "^" in item), "")
        column = caret_line.index("^") + 1 if "^" in caret_line else None
        return [
            DiagnosticRecord(
                tool="python",
                language="python",
                severity="error",
                category="syntax_error",
                code=etype,
                message=message,
                file=file_path,
                line=line_no,
                column=column,
                raw_excerpt="\n".join(raw_lines),
                parser_name="python_fallback",
                confidence=0.92,
            )
        ]
    return []


def _next_exception_line(lines: list[str], start: int) -> int | None:
    for index in range(start, min(len(lines), start + 8)):
        if FINAL_EXCEPTION_RE.match(lines[index].strip()):
            return index
    return None
