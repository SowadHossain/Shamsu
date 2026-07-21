"""Small fallback parser for plain-text Python tracebacks."""
from __future__ import annotations

import re

from shamsu.diagnostics.types import DiagnosticRecord

FRAME_RE = re.compile(r'^\s*File "(?P<file>[^"]+)", line (?P<line>\d+), in (?P<func>\S+)')
FINAL_EXCEPTION_RE = re.compile(r"^(?P<etype>[A-Za-z_][\w.]*(?:Error|Exception|Warning)):\s*(?P<message>.*)$")

VENDOR_MARKERS = ("site-packages", "/.venv/", "\\.venv\\", "\\Lib\\", "/lib/python")


def _is_user_frame(file_path: str) -> bool:
    normalized = file_path.replace("\\", "/")
    return not any(marker.replace("\\", "/") in normalized for marker in VENDOR_MARKERS)


def parse_python_traceback(text: str) -> list[DiagnosticRecord]:
    lines = text.splitlines()
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


def _looks_like_traceback_context(lines: list[str], index: int) -> bool:
    """Only treat an `Error: message` line as the *final* exception if it
    follows a Traceback block, not an arbitrary log line mentioning "Error"."""
    window = lines[max(0, index - 15):index]
    return any("Traceback (most recent call last)" in line for line in window)
