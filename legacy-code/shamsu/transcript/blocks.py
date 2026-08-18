"""Normalize the file-block shapes models actually emit into the canonical one.

``shamsu/llm/output.py`` recognises exactly one raw mutation envelope — the
header on the first line *inside* the fence::

    ```python
    # write_file: app.py
    <body>
    ```

Asked to write files in a chat, a 7B is at least as likely to put the header
*above* the fence, or to title the block with a bolded path. Both are
unambiguous; both were being dropped. The 2026-08-17 smoke run lost an entire
correct milestone that way — a complete ``app.py`` and ``requirements.txt`` the
model had written properly, discarded on layout.

Rather than widen the shared regex (every existing route depends on its exact
behaviour), this module rewrites the variants into the canonical form and hands
the result to the untouched parser. All of that parser's safety work — path
validation, ``..`` rejection, the four-backtick rule for markdown targets —
still runs, and still runs in one place.
"""
from __future__ import annotations

import re

# `# write_file: path` / `// append_file = path` / `write_file: path` sitting on
# its own line, immediately above a fence.
_HEADER_ABOVE_FENCE = re.compile(
    r"^[ \t]*(?:\#+|//+|--|<!--|/\*|\*)?[ \t]*"
    r"(?P<name>write_file|append_file|edit_file)"
    r"[ \t]*[:=][ \t]*"
    r"(?P<path>[^\s\r\n]+?)"
    r"[ \t]*(?:-->|\*/)?[ \t]*\r?\n"
    r"(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)\r?\n",
    re.MULTILINE,
)

# A bolded / backticked / headed bare path immediately above a fence:
#   **app.py**            `src/db.js`            ### templates/index.html
# Deliberately narrow: the path must carry a file extension and no spaces, or
# ordinary prose ("**Note**") starts writing files.
_TITLED_PATH_ABOVE_FENCE = re.compile(
    r"^[ \t]*(?:\#{1,6}[ \t]*)?"
    r"(?:\*\*|`)?"
    r"(?P<path>(?:[\w.\-]+/)*[\w.\-]+\.[A-Za-z0-9]{1,10})"
    r"(?:\*\*|`)?"
    r"[ \t]*:?[ \t]*\r?\n"
    r"(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)\r?\n",
    re.MULTILINE,
)

# Paths that are almost always prose about a file rather than a block titled
# with one. Cheap guard; the parser still validates whatever survives.
_TITLE_DENYLIST = frozenset({"e.g", "i.e", "etc", "vs"})


def normalize_file_headers(text: str) -> str:
    """Move above-the-fence file headers inside the fence.

    Idempotent: canonical blocks contain no header line above their fence, so
    they do not match and pass through untouched.
    """
    if not text:
        return text
    text = _HEADER_ABOVE_FENCE.sub(_move_header_inside, text)
    text = _TITLED_PATH_ABOVE_FENCE.sub(_title_to_header, text)
    return text


def _move_header_inside(match: re.Match[str]) -> str:
    fence = match.group("fence")
    info = match.group("info") or ""
    return f"{fence}{info}\n# {match.group('name')}: {match.group('path')}\n"


def _title_to_header(match: re.Match[str]) -> str:
    path = match.group("path")
    stem = path.rsplit(".", 1)[0].lower()
    fence = match.group("fence")
    info = match.group("info") or ""
    if stem in _TITLE_DENYLIST:
        return match.group(0)
    return f"{fence}{info}\n# write_file: {path}\n"
