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
    "BracketScan",
    "CheckResult",
    "TRUNCATION_ERROR_MARKERS",
    "bracket_problem",
    "bracket_scan",
    "check_file",
    "check_text",
    "checker_name",
    "unfinished_blocks",
    "truncation_signature",
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
    return _check(text, suffix, kind, path)


def check_text(text: str, suffix: str) -> CheckResult:
    """`check_file`, for content that is not on disk yet.

    So a caller can ask "would this parse?" BEFORE writing it. `replace_symbol`
    is the reason: it builds a complete file in memory, and the honest question
    there is whether the result still parses - which cannot be asked of a file
    that has not been written, and must not be asked by writing it first and
    checking afterwards.
    """
    lowered = (suffix or "").lower()
    kind = checker_name(lowered)
    if not kind:
        return CheckResult(SKIPPED, f"no checker for {lowered or 'this file type'}")
    return _check(text, lowered, kind, None)


def _check(text: str, suffix: str, kind: str, path: Path | None) -> CheckResult:
    """The shared body. `path` is only ever used to name the source and to let
    `node --check` run against a real file."""
    if kind == "python":
        try:
            compile(text, str(path) if path else "<content>", "exec")
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

    if suffix in _NODE and path is not None:
        # `node --check` needs a real file. Unwritten content falls through to
        # the structural scan rather than being written somewhere to satisfy it.
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
    scan = bracket_scan(
        text,
        line_comment=line_comment,
        block_comments=block_comments,
        template_strings=template_strings,
        regex_literals=regex_literals,
    )
    if scan.fault:
        return scan.fault
    if scan.unterminated == "comment":
        return f"unterminated /* comment opened on line {scan.unterminated_at}"
    if scan.open_blocks:
        opener, opened_at = scan.open_blocks[0]
        return (
            f"{len(scan.open_blocks)} unclosed {opener} - the first was opened on line "
            f"{opened_at} and the file ends without closing it, so it was "
            "cut off mid-block"
        )
    return ""


@dataclass(frozen=True)
class BracketScan:
    """What one structural pass found, before anyone decided what it means.

    `bracket_problem` used to be the only reader of this, and it collapsed all
    three fields into one sentence saying *something is wrong*. Two callers now
    need them apart, and for opposite reasons:

    - the post-write verifier has to tell "this file is still being built" from
      "this file is broken", and the difference is `open_blocks` with no
      `fault` and no `unterminated`;
    - the pre-write gate has to tell "an unfinished section" from "a severed
      generation", and the difference is `unterminated`.

    Reporting an unfinished chunk as a defect is not a cosmetic problem: it
    sends the model repairing a file that is simply not finished yet.
    """

    fault: str = ""
    open_blocks: tuple[tuple[str, int], ...] = ()
    unterminated: str = ""
    unterminated_at: int = 0


def bracket_scan(
    text: str,
    *,
    line_comment: str = "//",
    block_comments: bool = True,
    template_strings: bool = True,
    regex_literals: bool = True,
) -> BracketScan:
    """One pass over *text*, reporting what it saw rather than a verdict.

    `fault` is a real structural contradiction - a closer with nothing open, a
    mismatched pair - which is wrong at any stage of writing. `open_blocks` is
    the opener stack still standing at the end, which is a fault in a finished
    file and ordinary in an unfinished one. `unterminated` says the scan ran off
    the end while still inside a string or a comment, which no legitimate
    section does.
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
                return BracketScan(
                open_blocks=tuple(stack), unterminated="comment", unterminated_at=line
            )
            line += text.count("\n", index, end)
            index = end + 2
            continue
        if char in "\"'" or (template_strings and char == "`"):
            index, line, closed = _skip_string(text, index, line, template_strings)
            if not closed:
                return BracketScan(
                    open_blocks=tuple(stack), unterminated="string", unterminated_at=line
                )
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
                return BracketScan(fault=f"line {line}: unexpected {char} - nothing was open")
            opener, opened_at = stack.pop()
            if _PAIRS[opener] != char:
                return BracketScan(
                    fault=(
                        f"line {line}: {char} does not close the {opener} "
                        f"opened on line {opened_at}"
                    )
                )
        if char.isalnum() or char == "_":
            word += char
        else:
            word = ""
        if not char.isspace():
            previous = char
        index += 1

    return BracketScan(open_blocks=tuple(stack))


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


# ---------------------------------------------------------------------------
# Truncation, which is a different question from validity.

# Python syntax errors that mean "the file stops mid-construct" as opposed to
# an ordinary typo. Lifted from `chat_loop._TRUNCATION_ERROR_MARKERS`, where it
# was Python-only and lived in the legacy loop that simple mode replaced.
TRUNCATION_ERROR_MARKERS = (
    "unterminated string literal",
    "unterminated triple-quoted string literal",
    "was never closed",
    "unexpected eof",
    "incomplete input",
)

# Markers that only mean truncation when the content ALSO stops without a
# newline. `def f():` as the last line of a deliberate 60-line section is
# legitimate; the same line as the last thing a severed generation emitted is
# not, and the trailing newline is what tells them apart.
_TRUNCATION_MARKERS_NEEDING_A_CUT = ("expected an indented block",)

# Characters no line of code can legitimately end on. `:` is absent because
# Python and CSS both end lines with it; `>` because JSX and HTML do; `/`
# because `//` is an empty comment. Everything left is a dangling operator or
# separator with nothing after it, which is what a cut mid-expression leaves.
_DANGLING_ENDINGS = set(",.=+-*%&|^<!~?" + chr(92))

_SCAN_OPTIONS = {
    ".css": {"line_comment": "", "template_strings": False, "regex_literals": False},
    ".scss": {"line_comment": "//", "template_strings": False, "regex_literals": False},
    ".less": {"line_comment": "//", "template_strings": False, "regex_literals": False},
    ".json": {
        "line_comment": "",
        "block_comments": False,
        "template_strings": False,
        "regex_literals": False,
    },
}
_REGEX_SUFFIXES = {".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx"}


def _scan_options(suffix: str) -> dict:
    lowered = (suffix or "").lower()
    if lowered in _SCAN_OPTIONS:
        return dict(_SCAN_OPTIONS[lowered])
    return {"line_comment": "//", "regex_literals": lowered in _REGEX_SUFFIXES}


def unfinished_blocks(text: str, suffix: str = "") -> tuple[tuple[str, int], ...]:
    """The openers still standing, when that is the ONLY thing wrong. Else ``()``.

    The question a chunked write needs answered: *is this file merely not
    finished yet?* An open block on its own is exactly what the first section of
    a file looks like, and the caller can say so instead of reporting a fault.

    Empty when anything else is wrong - a closer with nothing open, a mismatched
    pair, a run that ended inside a string - because those are wrong at every
    stage of writing and must stay problems. Empty too for a language the
    structural scanner does not read: Python has no block delimiters to count,
    so an unfinished Python section is invisible here, which is correct rather
    than a gap - it is also invisible to the model as a problem.
    """
    if checker_name(suffix) != "brackets":
        return ()
    scan = bracket_scan(text, **_scan_options(suffix))
    if scan.fault or scan.unterminated:
        return ()
    return scan.open_blocks


def truncation_signature(text: str, *, suffix: str = "") -> str:
    """Why *text* looks like a generation that was CUT OFF, or ``""``.

    The distinction this draws is the one thing that makes chunked writing
    possible. A first section correctly has unclosed blocks, so a gate that
    tested for *validity* would refuse every legitimate chunk - the fix would
    create the bug. This tests for the shapes a severed generation leaves and
    nothing else:

    ==================================================  ==================
    ends mid-string literal (`render(request, "item`)   always truncation
    ends inside an unterminated /* comment              always truncation
    ends on a dangling operator, no trailing newline    truncation
    ends inside `(` or `[` opened on the last line      truncation
    ends cleanly on a complete line, blocks still open  a section - allow
    balanced and parses                                 allow
    ==================================================  ==================

    Silent for a file type nothing here can read. A truncated `.md` is a short
    document, not a broken one, and inventing a fault in prose would refuse
    writes for the sake of a full stop.
    """
    if not (text or "").strip():
        return ""
    kind = checker_name(suffix)
    if not kind:
        return ""
    ends_cut = not text.endswith(("\n", "\r"))

    if kind == "python":
        found = _python_truncation(text, ends_cut)
        if found:
            return found
    else:
        scan = bracket_scan(text, **_scan_options(suffix))
        if scan.unterminated == "string":
            return (
                f"it ends inside a string opened on line {scan.unterminated_at} "
                "that is never closed"
            )
        if scan.unterminated == "comment":
            return (
                f"it ends inside a /* comment opened on line {scan.unterminated_at}"
            )
        if ends_cut:
            last_line = text.count(chr(10)) + 1
            dangling = next(
                (
                    (opener, at)
                    for opener, at in scan.open_blocks
                    if opener in "([" and at == last_line
                ),
                None,
            )
            if dangling is not None:
                return (
                    f"the last line opens {dangling[0]} and stops before closing it"
                )

    if ends_cut:
        tail = text.rstrip(" " + chr(9))
        if tail and tail[-1] in _DANGLING_ENDINGS:
            return f"the last line ends on {tail[-1]!r} with nothing after it"
    return ""


def _python_truncation(text: str, ends_cut: bool) -> str:
    """Python has a real parser, so ask it rather than counting characters."""
    try:
        compile(text, "<content>", "exec")
    except SyntaxError as exc:
        message = str(getattr(exc, "msg", "") or exc)
        lowered = message.lower()
        if any(marker in lowered for marker in TRUNCATION_ERROR_MARKERS):
            return f"line {exc.lineno}: {message}"
        if ends_cut and any(
            marker in lowered for marker in _TRUNCATION_MARKERS_NEEDING_A_CUT
        ):
            return f"line {exc.lineno}: {message}"
    except Exception:  # noqa: BLE001 - a crashed check is not a truncation
        return ""
    return ""
