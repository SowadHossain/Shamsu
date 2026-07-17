"""Small-model fallback for markdown code blocks that should become files.

Small models routinely answer a file task with prose + a fenced code block
instead of a tool call - the fix is *shown*, never *applied*, and the user's
file stays broken while the answer claims success. This fallback turns that
answer shape into a real write.

Rebuilt 2026-07-17 after the `bugfix_syntax_error` eval flaked at 2/3 and the
captured failure exposed three holes in the original:

* the path regex required a LEADING verb ("edit broken.py"), so "broken.py has
  a syntax error - fix it" found no target at all;
* a usage fence alongside the fix (```bash python broken.py```) tripped the
  "multiple code blocks" refusal - showing how to run the file is how models
  answer;
* the single-block path wrote with ``overwrite=False``, which write_file
  refuses for existing files - and a FIX by definition targets an existing
  file. (The model-facing write_file tool always overwrites for exactly this
  reason; approval still gates it.)

Overwrites of an existing file additionally require the block to look like a
full replacement (>= half the current line count, or a small file), so a
10-line snippet can never silently replace a 300-line module.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from shamsu.tools.agent_tools import AgentToolRegistry, ToolResult

CODE_BLOCK_RE = re.compile(r"```(?P<lang>[\w.+-]*)[ \t]*\n(?P<code>.*?)```", re.S)
PATH_RE = re.compile(
    r"(?:create|write|save|make|generate|add|edit|update|fix)"
    r"(?:\s+(?:a|the|file|script|component|module|test|tests))?"
    r"\s+(?:as\s+|to\s+|at\s+|in\s+)?[\"']?(?P<path>[\w./\\ -]+\.[A-Za-z0-9_]+)[\"']?",
    re.I,
)
# Any file-looking token, wherever it sits in the sentence ("broken.py has a
# syntax error"). Used to find an EXISTING workspace file named by the user.
FILE_TOKEN_RE = re.compile(r"(?P<path>[\w][\w./\\-]*\.[A-Za-z0-9_]{1,8})")

# Fence languages that are usage/output, never file content.
_USAGE_LANGS = frozenset({"bash", "sh", "shell", "console", "cmd", "powershell", "text", "output", ""})
# Fence languages that plausibly ARE content for a given extension.
_EXT_LANGS: dict[str, frozenset[str]] = {
    ".py": frozenset({"python", "py", "python3"}),
    ".js": frozenset({"javascript", "js"}),
    ".jsx": frozenset({"jsx", "javascript"}),
    ".ts": frozenset({"typescript", "ts"}),
    ".tsx": frozenset({"tsx", "typescript"}),
    ".html": frozenset({"html"}),
    ".css": frozenset({"css"}),
    ".json": frozenset({"json"}),
    ".md": frozenset({"markdown", "md"}),
}


@dataclass(frozen=True)
class FallbackResult:
    handled: bool
    summary: str = ""
    tool_result: ToolResult | None = None
    tool_results: list[ToolResult] | None = None


class MarkdownWriteFallback:
    def __init__(self, tools: AgentToolRegistry) -> None:
        self.tools = tools

    def maybe_write(self, user_input: str, assistant_content: str) -> FallbackResult:
        blocks = [
            (match.group("lang").strip().lower(), match.group("code"))
            for match in CODE_BLOCK_RE.finditer(assistant_content or "")
        ]
        path = _infer_path(user_input) or self._existing_file_mentioned(user_input)
        if not path or not blocks:
            inferred_blocks = _infer_block_paths([code for _lang, code in blocks])
            if not inferred_blocks:
                return FallbackResult(False)
            results = [
                self.tools.write_file(block_path, block_code.rstrip() + "\n", overwrite=True)
                for block_path, block_code in inferred_blocks
            ]
            ok_count = sum(1 for result in results if result.ok)
            summary = f"Markdown fallback wrote {ok_count}/{len(results)} file(s)."
            return FallbackResult(True, summary, results[-1] if results else None, results)
        content_blocks = _content_blocks_for(path, blocks)
        if len(content_blocks) != 1:
            return FallbackResult(
                True,
                "I found multiple code blocks and need a single target before writing a file.",
            )
        code = content_blocks[0].rstrip() + "\n"
        target = self._resolve(path)
        if target is not None and target.is_file():
            if not _plausible_replacement(target, code):
                # A fragment must not clobber a large file. handled=True feeds
                # this back to the model as a tool message, steering it to
                # edit_file for a targeted change instead of a full rewrite.
                return FallbackResult(
                    True,
                    f"The code block does not look like a COMPLETE replacement for {path} "
                    "(it is much shorter than the current file). Apply the change with "
                    "edit_file instead of rewriting the whole file.",
                )
            result = self.tools.write_file(path, code, overwrite=True)
            return FallbackResult(True, result.message, result)
        result = self.tools.write_file(path, code, overwrite=False)
        return FallbackResult(True, result.message, result)

    def _existing_file_mentioned(self, user_input: str) -> str:
        """The single existing workspace file the user named, or "".

        "broken.py has a syntax error" names its target before any verb, which
        the verb-led PATH_RE cannot see. A file that actually exists on disk is
        the strongest possible signal it is the target; ambiguity (two existing
        files named) returns "" rather than guessing.
        """
        seen: list[str] = []
        for match in FILE_TOKEN_RE.finditer(user_input or ""):
            candidate = match.group("path").replace("\\", "/").lstrip("./")
            if candidate in seen:
                continue
            resolved = self._resolve(candidate)
            if resolved is not None and resolved.is_file():
                seen.append(candidate)
        return seen[0] if len(seen) == 1 else ""

    def _resolve(self, relative: str) -> Path | None:
        try:
            target = (self.tools.workspace_root / relative).resolve()
            target.relative_to(self.tools.workspace_root)
        except (OSError, ValueError):
            return None
        return target


def _infer_path(user_input: str) -> str:
    match = PATH_RE.search(user_input)
    return match.group("path").strip().replace("\\", "/") if match else ""


# A line that RUNS a file rather than being file content: `python broken.py`,
# `python3 -m py_compile broken.py`, `node app.js`, `npm test`, `pytest`, ...
_RUN_COMMAND_LINE_RE = re.compile(
    r"^\s*[$>]?\s*(?:python3?|node|npm|npx|pip3?|pytest|bash|sh|cmd|powershell)\b", re.I
)


def _is_run_command_block(code: str) -> bool:
    """A short block whose every non-empty line launches something is usage,
    never file content - REGARDLESS of its fence tag. Models tag fences
    sloppily (the fix in a bare ``` and `python3 broken.py` inside a
    ```python fence was observed live), so the tag alone cannot be trusted:
    trusting it once wrote the run command INTO the user's file."""
    lines = [line for line in code.strip().splitlines() if line.strip()]
    if not lines or len(lines) > 3:
        return False
    return all(_RUN_COMMAND_LINE_RE.match(line) for line in lines)


def _content_blocks_for(path: str, blocks: list[tuple[str, str]]) -> list[str]:
    """The blocks that could BE the file's content.

    Content is judged before tags: run-command blocks are dropped no matter
    how they are fenced, then the fence tag only breaks ties between real
    candidates. A .py target additionally requires the winner to parse as
    Python - the file may be getting FIXED, so the replacement must at least
    be syntactically valid, and a usage line like `python3 broken.py` is not.
    """
    candidates = [(lang, code) for lang, code in blocks if not _is_run_command_block(code)]
    extension = Path(path).suffix.lower()
    if len(candidates) > 1:
        expected_langs = _EXT_LANGS.get(extension, frozenset())
        tagged = [(lang, code) for lang, code in candidates if lang in expected_langs]
        if len(tagged) == 1:
            candidates = tagged
        else:
            non_usage = [(lang, code) for lang, code in candidates if lang not in _USAGE_LANGS]
            if len(non_usage) == 1:
                candidates = non_usage
    chosen = [code for _lang, code in candidates]
    if extension == ".py":
        chosen = [code for code in chosen if _parses_as_python(code)]
    return chosen


def _parses_as_python(code: str) -> bool:
    import ast

    try:
        ast.parse(code)
        return True
    except SyntaxError:
        return False


def _plausible_replacement(target: Path, code: str) -> bool:
    """True when `code` can stand in for the whole file."""
    try:
        current_lines = len(target.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return False
    new_lines = len(code.splitlines())
    return current_lines <= 40 or new_lines * 2 >= current_lines


def _infer_block_paths(blocks: list[str]) -> list[tuple[str, str]]:
    inferred: list[tuple[str, str]] = []
    for block in blocks:
        lines = block.strip().splitlines()
        if not lines:
            continue
        path = _path_from_comment(lines[0])
        if not path:
            continue
        inferred.append((path, "\n".join(lines[1:])))
    return inferred


def _path_from_comment(line: str) -> str:
    match = re.match(r"\s*(?://|#|/\*)\s*(?P<path>[\w./\\ -]+\.[A-Za-z0-9_]+)", line)
    return match.group("path").strip().replace("\\", "/") if match else ""
