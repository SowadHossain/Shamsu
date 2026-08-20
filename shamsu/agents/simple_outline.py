"""What a file CONTAINS, without sending its contents.

The read path had one move for a large file: hand over the first 24,000 bytes
and stop. That is a dead end with four links (SMALLCODE_GAP_ANALYSIS.md §2) -
the model patches from what it saw, `old_string` is not found because it never
saw that part, the fuzzy retry misses too, and the whole-file rewrite is
refused because the read was partial. There is no fifth move, so the turn spins
and stops.

An outline breaks the chain at the first link. The model is given the SHAPE of
the file - every class, every function, its signature and its exact line range -
and then fetches only the part it actually needs. A 2,000-line module costs a
few hundred tokens to understand instead of blowing the window, and the line
ranges are exact, so the follow-up read cannot miss.

**Deliberately not smallcode's mechanism.** Theirs (`bin/executor.js:132`,
"Feature 2: summarize large files") calls a model to summarise, and feeds it
`content.slice(0, 8000)` - so the outline of a 2,000-line file is derived from
its first ~200 lines and the tail is invisible. That is the head-clipping bug
this module exists to fix, wearing a different coat, and it costs a full
generation (~100s on a local 7B) plus a chance to invent a signature that is
not there.

This is parsed instead: zero model calls, the whole file every time, and a
symbol that appears here is one that is really in the file. Python goes through
`ast` and is exact. Braced languages go through a declaration scan that is
frankly heuristic - it will miss an exotic declaration form - but a symbol it
reports is one it saw, and anything it misses is still reachable by line range.
Silence, never invention, is the failure mode.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Symbol",
    "can_outline",
    "find_symbol",
    "outline",
    "render_outline",
]

# Suffixes with a real parser, and suffixes with a declaration scan. A file type
# in neither has no outline - which is reported, not hidden.
_PYTHON = {".py", ".pyi"}
_BRACED = {
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx",
    ".java", ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".swift",
    ".kt", ".scala",
}


@dataclass(frozen=True)
class Symbol:
    """One declaration, and where its body lives."""

    name: str
    kind: str          # "class" | "function" | "method"
    signature: str
    start: int         # 1-indexed, inclusive
    end: int           # 1-indexed, inclusive
    depth: int = 0     # 0 top-level, 1 inside a class
    purpose: str = ""  # first line of the docstring, when there is one

    @property
    def lines(self) -> int:
        return max(1, self.end - self.start + 1)


def can_outline(suffix: str) -> bool:
    """Is there any way to read the shape of this file type?"""
    lowered = (suffix or "").lower()
    return lowered in _PYTHON or lowered in _BRACED


def outline(text: str, suffix: str) -> list[Symbol]:
    """Every declaration in *text*, in the order it appears."""
    lowered = (suffix or "").lower()
    if lowered in _PYTHON:
        return _python_outline(text)
    if lowered in _BRACED:
        return _braced_outline(text)
    return []


def find_symbol(text: str, suffix: str, name: str) -> Symbol | None:
    """The declaration called *name*, or ``None``.

    Matched leniently on purpose: a model asks for `render`, `Game.render` and
    `def render` for the same thing, and refusing two of the three would spend a
    round teaching a naming convention nobody agreed to.
    """
    wanted = (name or "").strip().strip("()").replace("()", "")
    if not wanted:
        return None
    symbols = outline(text, suffix)
    tail = wanted.rsplit(".", 1)[-1].lower()
    exact = [s for s in symbols if s.name.lower() == wanted.lower()]
    if exact:
        return exact[0]
    # `Game.render` when the outline holds the method as plain `render`, and the
    # reverse - a bare `render` when the outline qualified it.
    qualified = [
        s for s in symbols
        if s.name.lower() == tail or s.name.lower().endswith("." + tail)
    ]
    if qualified:
        return qualified[0]
    return None


def render_outline(relative: str, text: str, suffix: str) -> str:
    """The outline as the model should see it, or ``""`` if there is none.

    Line ranges are printed because they are the follow-up call's arguments. A
    map that says a function exists but not where it is would leave the model
    guessing at ranges, which is the thing this replaces.
    """
    symbols = outline(text, suffix)
    if not symbols:
        return ""
    total = text.count("\n") + 1
    lines = [
        f"{relative} - {total:,} lines, outline only "
        f"({len(symbols)} symbol(s)). The BODIES are not shown."
    ]
    for symbol in symbols:
        indent = "  " * (symbol.depth + 1)
        purpose = f"  - {symbol.purpose}" if symbol.purpose else ""
        lines.append(
            f"{indent}L{symbol.start}-{symbol.end}  {symbol.signature}{purpose}"
        )
    lines.append("")
    lines.append(
        "To see one of these, call read_symbol(filepath, symbol) - it returns "
        "that symbol's exact source. For anything else, read_file with "
        "start_line and end_line."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Python: a real parser, so this is exact.


def _python_outline(text: str) -> list[Symbol]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        # A file mid-edit does not parse, and refusing to describe it would take
        # the outline away exactly when the model is trying to repair it.
        return _braced_outline(text, python=True)
    found: list[Symbol] = []
    for node in tree.body:
        _python_symbol(node, found, depth=0, parent="")
    return found


def _python_symbol(node: ast.AST, into: list[Symbol], depth: int, parent: str) -> None:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return
    is_class = isinstance(node, ast.ClassDef)
    name = f"{parent}.{node.name}" if parent else node.name
    doc = ast.get_docstring(node) or ""
    into.append(
        Symbol(
            name=name,
            kind="class" if is_class else ("method" if depth else "function"),
            signature=_python_signature(node),
            start=int(getattr(node, "lineno", 1)),
            end=int(getattr(node, "end_lineno", 0) or getattr(node, "lineno", 1)),
            depth=depth,
            purpose=doc.strip().splitlines()[0][:100] if doc.strip() else "",
        )
    )
    if is_class:
        for child in node.body:
            _python_symbol(child, into, depth + 1, name)


def _python_signature(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        try:
            bases = [ast.unparse(base) for base in node.bases]
        except Exception:  # noqa: BLE001 - a signature is not worth a crash
            bases = []
        return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    try:
        arguments = ast.unparse(node.args)
    except Exception:  # noqa: BLE001
        arguments = "..."
    return f"{prefix} {node.name}({arguments})"


# ---------------------------------------------------------------------------
# Braced languages: a declaration scan. Heuristic, and quiet when unsure.

_DECLARATIONS = (
    # class / struct / interface / enum / impl / trait
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|internal\s+|abstract\s+|final\s+|sealed\s+|static\s+)*"
                r"(?P<kw>class|struct|interface|enum|impl|trait)\s+(?P<name>[A-Za-z_$][\w$]*)"), "class"),
    # function name(...)  /  async function name(...)
    (re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*\("), "function"),
    # Go: func name(...) / func (r *T) name(...)
    (re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_][\w]*)\s*\("), "function"),
    # Rust: fn name(...)
    (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?(?:const\s+)?fn\s+(?P<name>[A-Za-z_][\w]*)"), "function"),
    # const name = (...) => / const name = function / let name = async (...) =>
    (re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*"
                r"(?:async\s+)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"), "function"),
    # PHP / Java / C# / C++ member and free functions:  modifiers type name(...)
    (re.compile(r"^\s*(?:public|private|protected|internal|static|final|abstract|override|virtual|synchronized|def)\s+"
                r"(?:[\w<>\[\],:?.$]+\s+)*(?P<name>[A-Za-z_$][\w$]*)\s*\([^;]*$"), "function"),
)

# A method inside a class body: `name(args) {` with nothing that makes it a
# call or a control statement. Only consulted at depth >= 1.
_METHOD = re.compile(
    r"^\s*(?:async\s+)?(?:static\s+)?(?:get\s+|set\s+)?(?P<name>[A-Za-z_$][\w$]*)\s*\([^;]*\)\s*\{?\s*$"
)
_NOT_A_DECLARATION = {
    "if", "for", "while", "switch", "catch", "return", "else", "do", "try",
    "with", "case", "typeof", "new", "await", "yield", "throw", "super",
}


def _braced_outline(text: str, python: bool = False) -> list[Symbol]:
    """Declarations by line, with ends found by brace depth.

    Also the fallback for Python source that does not parse - a file caught
    mid-edit still has a shape, and hiding it precisely when the model is
    repairing the file would be the wrong moment to go quiet.
    """
    depths = _depth_by_line(text, python=python)
    lines = text.splitlines()
    found: list[Symbol] = []
    open_classes: list[tuple[str, int]] = []  # (name, depth the body sits at)

    for index, raw in enumerate(lines):
        number = index + 1
        depth_here = depths[index][0] if index < len(depths) else 0
        stripped = raw.strip()
        if not stripped or stripped.startswith(("//", "#", "*", "/*")):
            continue

        # Leaving a class body.
        while open_classes and depth_here <= open_classes[-1][1] - 1:
            open_classes.pop()

        matched = _match_declaration(stripped, depth_here, python)
        if matched is None:
            continue
        name, kind = matched
        if python:
            end = _python_block_end(lines, index)
        else:
            end = _brace_block_end(depths, index)
        parent = open_classes[-1][0] if open_classes else ""
        qualified = f"{parent}.{name}" if parent and kind != "class" else name
        found.append(
            Symbol(
                name=qualified,
                kind="method" if parent and kind != "class" else kind,
                signature=stripped.rstrip("{").strip()[:160],
                start=number,
                end=end,
                depth=1 if parent and kind != "class" else 0,
            )
        )
        if kind == "class":
            open_classes.append((name, depth_here + 1))
    return found


def _match_declaration(stripped: str, depth: int, python: bool) -> tuple[str, str] | None:
    if python:
        match = re.match(r"^(?:async\s+)?(?:def|class)\s+(?P<name>[A-Za-z_]\w*)", stripped)
        if match:
            return match.group("name"), "class" if stripped.startswith("class") else "function"
        return None
    for pattern, kind in _DECLARATIONS:
        match = pattern.match(stripped)
        if match:
            name = match.group("name")
            if name.lower() in _NOT_A_DECLARATION:
                return None
            return name, kind
    if depth >= 1:
        match = _METHOD.match(stripped)
        if match:
            name = match.group("name")
            if name.lower() in _NOT_A_DECLARATION:
                return None
            return name, "function"
    return None


def _brace_block_end(depths: list[tuple[int, int]], index: int) -> int:
    """The line where the block opened at *index* closes again."""
    if index >= len(depths):
        return index + 1
    opened_at = depths[index][0]
    for cursor in range(index, len(depths)):
        _, after = depths[cursor]
        if after <= opened_at and cursor > index:
            return cursor + 1
        if after <= opened_at and cursor == index and depths[index][1] == opened_at:
            # A one-line declaration that never opened a block.
            return cursor + 1
    return len(depths)


def _python_block_end(lines: list[str], index: int) -> int:
    """The last line of an indented Python block starting at *index*."""
    opener = lines[index]
    base = len(opener) - len(opener.lstrip())
    end = index + 1
    for cursor in range(index + 1, len(lines)):
        line = lines[cursor]
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= base:
            break
        end = cursor + 1
    return max(end, index + 1)


def _depth_by_line(text: str, python: bool = False) -> list[tuple[int, int]]:
    """Per line: `(depth at its start, depth at its end)`.

    Strings, template literals and comments are skipped, so a brace inside
    `"}"` does not close a block. Same reasoning as
    `simple_verify.bracket_scan`, and deliberately as forgiving: an apostrophe
    in a comment must not shift every line after it.
    """
    result: list[tuple[int, int]] = []
    depth = 0
    at_line_start = 0
    index = 0
    size = len(text)
    while index <= size:
        if index == size or text[index] == "\n":
            result.append((at_line_start, depth))
            at_line_start = depth
            index += 1
            if index > size:
                break
            continue
        char = text[index]
        if text.startswith("//", index) or (python and char == "#"):
            newline = text.find("\n", index)
            index = size if newline < 0 else newline
            continue
        if not python and text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end < 0:
                index = size
                continue
            while index < end:
                if text[index] == "\n":
                    result.append((at_line_start, depth))
                    at_line_start = depth
                index += 1
            index = end + 2
            continue
        if char in "\"'`":
            index, crossed = _skip_quoted(text, index)
            for _ in range(crossed):
                result.append((at_line_start, depth))
                at_line_start = depth
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        index += 1
    if not result:
        result.append((0, 0))
    return result


def _skip_quoted(text: str, index: int) -> tuple[int, int]:
    """Walk past a quoted run. Returns `(next index, newlines crossed)`."""
    quote = text[index]
    multiline = quote == "`"
    cursor = index + 1
    size = len(text)
    crossed = 0
    while cursor < size:
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if char == quote:
            return cursor + 1, crossed
        if char == "\n":
            if not multiline:
                # An unterminated single-line string is far more often an
                # apostrophe in prose than a fault. Resume at the newline.
                return cursor, crossed
            crossed += 1
        cursor += 1
    return size, crossed


def outline_for_path(path: Path) -> list[Symbol]:
    """Convenience: outline whatever is on disk at *path*."""
    try:
        return outline(path.read_text(encoding="utf-8", errors="replace"), path.suffix)
    except OSError:
        return []
