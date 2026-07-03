"""Small-model fallback for markdown code blocks that should become files."""
from __future__ import annotations

import re
from dataclasses import dataclass

from shamsu.tools.agent_tools import AgentToolRegistry, ToolResult

CODE_BLOCK_RE = re.compile(r"```(?P<lang>[\w.+-]*)\n(?P<code>.*?)```", re.S)
PATH_RE = re.compile(
    r"(?:create|write|save|make)(?:\s+(?:a|the|file))?\s+(?:as\s+|to\s+|at\s+)?[\"']?(?P<path>[\w./\\ -]+\.[A-Za-z0-9_]+)[\"']?",
    re.I,
)


@dataclass(frozen=True)
class FallbackResult:
    handled: bool
    summary: str = ""
    tool_result: ToolResult | None = None


class MarkdownWriteFallback:
    def __init__(self, tools: AgentToolRegistry) -> None:
        self.tools = tools

    def maybe_write(self, user_input: str, assistant_content: str) -> FallbackResult:
        path = _infer_path(user_input)
        blocks = [match.group("code") for match in CODE_BLOCK_RE.finditer(assistant_content or "")]
        if not path or not blocks:
            return FallbackResult(False)
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
