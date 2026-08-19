"""What "checked" is allowed to mean.

The old verifier appended a filename to its `checked` list before testing the
extension, so a `.js` or `.md` file it never opened came back as *"no syntax
errors."* Live 2026-08-19 that produced **572** such claims in one session,
including `js/game.js` with 21 unclosed braces - the one signal that should
have caught the truncation instead confirmed the file was fine.

So the rule here: a file is only ever reported as checked by the checker that
actually parsed it. Everything else says `skipped`, out loud, because silence
reads as approval.

Three verdicts, and `skipped` is the important one - it is the escape. A file
type nobody can parse is not a defect, and reporting it as one would leave the
model repairing a `.md` file forever.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CheckResult",
    "bracket_problem",
    "check_file",
    "checker_name",
]

OK = "ok"
PROBLEM = "problem"
SKIPPED = "skipped"


@dataclass(frozen=True)
class CheckResult:
    """One file's verdict, and what to say about it."""

    status: str
    detail: str = ""
    checker: str = ""

    @property
    def ok(self) -> bool:
        return self.status == OK


# Suffix -> how it is parsed. A suffix absent from here has no checker, which
# is a fact to report rather than a hole to hide.
_PYTHON = {".py", ".pyi"}
_JSON = {".json"}
# `node --check` understands plain scripts and modules. It does NOT understand
# JSX or TypeScript, so those get the structural scan.
_NODE = {".js", ".mjs", ".cjs"}
_BRACED = {
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".css", ".scss", ".less",
    ".c", ".h", ".cpp", ".hpp", ".java", ".go", ".rs",
}

NODE_TIMEOUT_SECONDS = 10.0


def checker_name(suffix: str) -> str:
    """Which checker owns this suffix, or ``""`` if none does."""
    lowered = (suffix or "").lower()
    if lowered in _PYTHON:
        return "python"
    if lowered in _JSON:
        return "json"
    if lowered in _BRACED:
        return "brackets"
    return ""


def check_file(path: Path) -> CheckResult:
    """Parse *path* with whatever can parse it, and say which one did.

    Never raises: a checker that blows up is reported as a problem carrying its
    own exception, because "the checker crashed" is information the model needs
    and an exception here would take down the turn.
    """
    suffix = path.suffix.lower()
    kind = checker_name(suffix)
    if not kind:
        return CheckResult(SKIPPED, f"no checker for {suffix or 'this file type'}")
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return CheckResult(PROBLEM, f"could not be read: {exc}", kind)

    if kind == "python":
        try:
            compile(text, str(path), "exec")
        except SyntaxError as exc:
            return CheckResult(PROBLEM, f"line {exc.lineno}: {exc.msg}", kind)
        except Exception as exc:  # noqa: BLE001 - a crashed checker is not a pass
            return CheckResult(PROBLEM, f"{type(exc).__name__}: {exc}", kind)
        return CheckResult(OK, "", kind)

    if kind == "json":
        try:
            json.loads(text)
        except ValueError as exc:
            return CheckResult(PROBLEM, str(exc), kind)
        return CheckResult(OK, "", kind)

    if suffix in _NODE:
        verdict = _node_check(path)
        if verdict is not None:
            return verdict

    problem = bracket_problem(
        text,
        line_comment="" if suffix == ".css" else "//",
        template_strings=suffix not in {".css", ".scss", ".less"},
        regex_literals=suffix in {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"},
    )
    if problem:
        return CheckResult(PROBLEM, problem, "brackets")
    return CheckResult(OK, "", "brackets")


def _node_check(path: Path) -> CheckResult | None:
    """`node --check`, when node is here. ``None`` means fall back.

    A real parser beats a bracket count when one is installed, but its absence
    must not turn into a missing check - which is the whole defect this module
    exists for.
    """
    disabled = os.environ.get("SHAMSU_DISABLE_NODE_CHECK", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"}:
        return None
    node = shutil.which("node")
    if not node:
        return None
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [node, "--check", str(path)],
            capture_output=True,
            text=True,
            timeout=NODE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode == 0:
        return CheckResult(OK, "", "node --check")
    detail = _node_detail(done.stderr or done.stdout or "")
    return CheckResult(PROBLEM, detail or "node --check rejected it", "node --check")


def _node_detail(stderr: str) -> str:
    """The one line of node's stderr worth handing back.

    Node prints the offending source line, a caret, a blank line and a stack.
    The `SyntaxError:` line is the sentence; the rest is noise in a prompt.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    for line in lines:
        if "Error:" in line:
            return line
    return lines[0] if lines else ""


# ---------------------------------------------------------------------------
# The zero-dependency fallback.

_PAIRS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {close: open_ for open_, close in _PAIRS.items()}

# A `/` here starts a regular expression, not a division. Everything else -
# an identifier, a digit, `)`, `]` - means division, so `(a + b) / 2` is not
# read as a literal that swallows the rest of the file.
_REGEX_AFTER_PUNCTUATION = set("(,=:[!&|?{};+-*%^~<>")
_REGEX_AFTER_KEYWORD = {
    "return", "typeof", "case", "in", "of", "do", "else", "yield", "await",
    "delete", "void", "instanceof", "new",
}


def bracket_problem(
    text: str,
    *,
    line_comment: str = "//",
    block_comments: bool = True,
    template_strings: bool = True,
    regex_literals: bool = True,
) -> str:
    """The first structural fault in *text*, or ``""``.

    Counts `()[]{}` outside strings and comments. That is not a parser and does
    not pretend to be one - it answers exactly one question, *does this file
    end mid-block*, which is the shape every truncated generation has:
    `game.js` closed 39 of 60 braces, `player.js` 30 of 47, and both ended on
    the literal line `} else {`.

    Deliberately biased toward silence. An apostrophe in prose, or a `/` it
    cannot classify, resumes scanning rather than inventing a fault - a false
    "your file is broken" would send the model repairing something that is not
    wrong, which is the same failure as a false "no syntax errors", pointed the
    other way.
    """
    stack: list[tuple[str, int]] = []
    line = 1
    index = 0
    size = len(text)
    previous = ""      # last significant character
    word = ""          # last identifier, for `return /re/`

    while index < size:
        char = text[index]
        if char == "\n":
            line += 1
            index += 1
            continue
        if line_comment and text.startswith(line_comment, index):
            newline = text.find("\n", index)
            index = size if newline < 0 else newline
            continue
        if block_comments and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                return f"unterminated /* comment opened on line {line}"
            line += text.count("\n", index, end)
            index = end + 2
            continue
        if char in "\"'" or (template_strings and char == "`"):
            index, line, closed = _skip_string(text, index, line, template_strings)
            if not closed:
                break
            previous, word = char, ""
            continue
        if regex_literals and char == "/" and _starts_a_regex(previous, word):
            index, line, closed = _skip_regex(text, index, line)
            if not closed:
                # Could not classify it. Treat it as division and carry on
                # rather than guess a fault into existence.
                index += 1
            previous, word = "/", ""
            continue
        if char in _PAIRS:
            stack.append((char, line))
        elif char in _CLOSERS:
            if not stack:
                return f"line {line}: unexpected {char} - nothing was open"
            opener, opened_at = stack.pop()
            if _PAIRS[opener] != char:
                return (
                    f"line {line}: {char} does not close the {opener} "
                    f"opened on line {opened_at}"
                )
        if char.isalnum() or char == "_":
            word += char
        else:
            word = ""
        if not char.isspace():
            previous = char
        index += 1

    if stack:
        opener, opened_at = stack[0]
        return (
            f"{len(stack)} unclosed {opener} - the first was opened on line "
            f"{opened_at} and the file ends without closing it, so it was "
            "cut off mid-block"
        )
    return ""


def _skip_string(text: str, index: int, line: int, template: bool) -> tuple[int, int, bool]:
    """Walk past a quoted run. Returns (index, line, closed)."""
    quote = text[index]
    multiline = template and quote == "`"
    cursor = index + 1
    size = len(text)
    while cursor < size:
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == quote:
            return cursor + 1, line, True
        if char == "\n":
            if not multiline:
                # An unterminated single-line string is far more often an
                # apostrophe in prose than a fault. Resume at the newline.
                return cursor, line, True
            line += 1
        cursor += 1
    return size, line, False


def _starts_a_regex(previous: str, word: str) -> bool:
    if not previous:
        return True
    if word in _REGEX_AFTER_KEYWORD:
        return True
    return previous in _REGEX_AFTER_PUNCTUATION


def _skip_regex(text: str, index: int, line: int) -> tuple[int, int, bool]:
    """Walk past a `/.../` literal. Returns (index, line, closed)."""
    cursor = index + 1
    size = len(text)
    in_class = False
    while cursor < size:
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == "\n":
            return index, line, False        # regexes do not span lines
        if char == "[":
            in_class = True
        elif char == "]":
            in_class = False
        elif char == "/" and not in_class:
            cursor += 1
            while cursor < size and text[cursor].isalpha():
                cursor += 1                  # flags
            return cursor, line, True
        cursor += 1
    return index, line, False
