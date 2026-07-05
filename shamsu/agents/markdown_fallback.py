"""Small-model fallback for markdown code blocks that should become files."""
from __future__ import annotations

import re
from dataclasses import dataclass

from shamsu.tools.agent_tools import AgentToolRegistry, ToolResult

CODE_BLOCK_RE = re.compile(r"```(?P<lang>[\w.+-]*)\n(?P<code>.*?)```", re.S)
PATH_RE = re.compile(
    r"(?:create|write|save|make|generate|add|edit|update)"
    r"(?:\s+(?:a|the|file|script|component|module|test|tests))?"
    r"\s+(?:as\s+|to\s+|at\s+)?[\"']?(?P<path>[\w./\\ -]+\.[A-Za-z0-9_]+)[\"']?",
    re.I,
)


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
        path = _infer_path(user_input)
        blocks = [match.group("code") for match in CODE_BLOCK_RE.finditer(assistant_content or "")]
        if not path or not blocks:
            inferred_blocks = _infer_block_paths(blocks)
            if not inferred_blocks:
                return FallbackResult(False)
            results = [
                self.tools.write_file(block_path, block_code.rstrip() + "\n", overwrite=True)
                for block_path, block_code in inferred_blocks
            ]
            ok_count = sum(1 for result in results if result.ok)
            summary = f"Markdown fallback wrote {ok_count}/{len(results)} file(s)."
            return FallbackResult(True, summary, results[-1] if results else None, results)
        if len(blocks) != 1:
            return FallbackResult(
                True,
                "I found multiple code blocks and need a single target before writing a file.",
            )
        result = self.tools.write_file(path, blocks[0].rstrip() + "\n", overwrite=False)
        return FallbackResult(True, result.message, result)


def _infer_path(user_input: str) -> str:
    match = PATH_RE.search(user_input)
    return match.group("path").strip().replace("\\", "/") if match else ""


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
