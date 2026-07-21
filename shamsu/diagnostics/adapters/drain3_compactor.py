"""Drain3-style compaction for noisy runtime logs (dev-server spam, repeated
warnings/stack traces) - never used for exact compiler diagnostics, where
line numbers/symbols/codes must stay exact.

Uses the real `drain3` package (https://github.com/logpai/Drain3) when it's
installed. `drain3` is pure-Python and does not pull in heavy ML
dependencies, so `/diagnostics setup` may install it locally. When it isn't
installed, falls back to a small deterministic template miner: mask
volatile tokens (numbers, hex, uuids, timestamps) and group identical
masked lines, which is the same idea Drain does at a much smaller scale.
"""
from __future__ import annotations

import re
from collections import OrderedDict

_NUMBER_RE = re.compile(r"\b\d+\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]+\b")
_UUID_RE = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
_TIMESTAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\b")


def is_available() -> bool:
    try:
        import drain3  # noqa: F401
    except ImportError:
        return False
    return True


def _mask(line: str) -> str:
    masked = _TIMESTAMP_RE.sub("<TS>", line)
    masked = _UUID_RE.sub("<UUID>", masked)
    masked = _HEX_RE.sub("<HEX>", masked)
    masked = _NUMBER_RE.sub("<N>", masked)
    return masked.strip()


def compact(lines: list[str]) -> tuple[list[tuple[str, int]], int]:
    """Returns (templates_with_counts, repeated_lines_removed)."""
    if is_available():
        templates, removed = _compact_with_drain3(lines)
        if templates:
            return templates, removed
    return _compact_fallback(lines)


def _compact_with_drain3(lines: list[str]) -> tuple[list[tuple[str, int]], int]:
    from drain3 import TemplateMiner  # type: ignore

    miner = TemplateMiner()
    for line in lines:
        if line.strip():
            miner.add_log_message(line)
    templates: list[tuple[str, int]] = []
    total_seen = 0
    for cluster in miner.drain.clusters:
        template = cluster.get_template()
        count = cluster.size
        templates.append((template, count))
        total_seen += count
    removed = max(total_seen - len(templates), 0)
    return templates, removed


def _compact_fallback(lines: list[str]) -> tuple[list[tuple[str, int]], int]:
    grouped: "OrderedDict[str, int]" = OrderedDict()
    total = 0
    for line in lines:
        if not line.strip():
            continue
        total += 1
        key = _mask(line)
        grouped[key] = grouped.get(key, 0) + 1
    templates = list(grouped.items())
    removed = max(total - len(templates), 0)
    return templates, removed


def is_noisy_runtime_log(tool: str, structured_diagnostic_count: int, line_count: int) -> bool:
    """Heuristic: long output with few/no exact diagnostics parsed and a
    tool known to run as a long-lived dev process is "noisy runtime log"
    territory, where Drain3-style compaction is appropriate - not a
    compiler/linter run where every line matters."""
    # Matches the canonical (tool, language) names shamsu.diagnostics.normalize
    # .detect_tool() returns, plus a couple of raw aliases for direct callers.
    noisy_tools = {"npm dev", "vite", "node", "webpack-dev-server", "next dev"}
    if structured_diagnostic_count > 0:
        return False
    return line_count > 40 and any(candidate in tool for candidate in noisy_tools)
