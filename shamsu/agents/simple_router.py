"""Two-stage tool routing: pick a category, then get that category's tools.

Ported from smallcode `src/tools/two_stage_router.js` (MIT, (c) 2026
Doorman11991 - see reference/smallcode/LICENSE), including the ~16k threshold
and the fall-back-to-everything rule.

The problem it solves appeared here the moment the roster grew: 19 tools is
~2,100 tokens of schema on every single call, 6.4% of a 32k window, and - worse
than the tokens - 19 things a 7B model has to choose between correctly.

Stage 1 shows the model one tool, `select_category`, and about 200 tokens of
category descriptions. Stage 2 shows it only the tools in the category it
picked. It costs one extra round and buys back most of the schema.

**The part worth copying exactly is when NOT to use it.** smallcode routes by
context window: at or below 16k, two-stage; above it, send everything. That is
the opposite of what I had reached for - a manual switch defaulting to
narrow - and it is better reasoning. A big window can afford the schemas, and
paying an extra round to save 2k tokens out of 32k is a bad trade. A small one
cannot, and there the round is cheap by comparison.

SHAMSU reaches small windows on its own: `_shrink_for_oom` steps 32k down to
16k and then 8k when the GPU refuses, and smaller models cap lower to begin
with. Routing on the *effective* ceiling means the tool list narrows exactly
when the window gets tight, without anyone deciding anything.
"""
from __future__ import annotations

import os
from typing import Any

# At or below this, narrow the tools. smallcode's number, and the reasoning
# holds: above it the schemas are affordable and an extra round is not worth
# saving them.
TWO_STAGE_CTX_THRESHOLD = 16_384

# Categories over SHAMSU's roster. Grouped by what the model is trying to DO,
# not by which module implements it - "I need to look at a file" is a thought a
# small model can have; "this is in agent_tools" is not.
TOOL_CATEGORIES: dict[str, dict[str, Any]] = {
    "read": {
        "description": "Read a file, or find files by name or glob",
        "tools": [
            "read_file", "read_symbol", "list_files", "find_files", "find_and_read",
        ],
    },
    "write": {
        "description": "Create, edit, extend or rewrite files",
        "tools": [
            "write_file",
            "patch_file",
            "append_file",
            "read_and_patch",
            "create_and_run",
        ],
    },
    "search": {
        "description": "Search the code by meaning or pattern, query the code graph",
        "tools": ["search_files", "search_and_read", "graph_search", "explain_symbol"],
    },
    "run": {
        "description": "Run a shell command, the project, or its tests",
        "tools": ["run_command", "run_tests", "create_and_run"],
    },
    "recall": {
        "description": "Remember or look up project knowledge, and search this conversation",
        "tools": [
            "memory_load",
            "memory_remember",
            "memory_list",
            "memory_forget",
            "history_search",
        ],
    },
}

# Reachable from every category. Recall is cross-cutting because the moment a
# model needs it is the moment it is doing something else and has realised it
# is missing a fact - forcing a category switch to look one up is the sort of
# friction that makes a small model give up and guess instead.
ALWAYS_TOOLS = ("memory_load", "history_search")

SELECTOR_TOOL_NAME = "select_category"


def routing_mode(context_window: int) -> str:
    """``"direct"`` or ``"two_stage"``, from the window unless overridden."""
    override = os.environ.get("SHAMSU_TOOL_ROUTING", "").strip().lower()
    if override in {"direct", "two_stage"}:
        return override
    return "two_stage" if context_window <= TWO_STAGE_CTX_THRESHOLD else "direct"


def category_selector_tool() -> dict[str, Any]:
    """Stage 1: the single tool the model sees before it has chosen."""
    listed = "; ".join(
        f"{name} ({body['description']})" for name, body in TOOL_CATEGORIES.items()
    )
    return {
        "type": "function",
        "function": {
            "name": SELECTOR_TOOL_NAME,
            "description": (
                "Say which kind of tool you need next and you will be given "
                f"those tools. Categories: {listed}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": list(TOOL_CATEGORIES),
                        "description": "The kind of action you need to take next.",
                    }
                },
                "required": ["category"],
            },
        },
    }


def tools_for_category(category: str, schemas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stage 2: that category's tools, plus the cross-cutting ones.

    An unrecognised category returns EVERYTHING rather than nothing. A model
    that invents a category name has still told us it wants to act, and
    answering with an empty tool list would strand it - smallcode makes the
    same call and it is the right one.
    """
    body = TOOL_CATEGORIES.get((category or "").strip().lower())
    if body is None:
        return schemas
    allowed = set(body["tools"]) | set(ALWAYS_TOOLS)
    chosen = [
        schema
        for schema in schemas
        if (schema.get("function") or {}).get("name") in allowed
    ]
    return chosen or schemas


def schema_savings(schemas: list[dict[str, Any]]) -> dict[str, int]:
    """What routing costs and saves, in tokens, for reporting."""
    from shamsu.context.budget import tool_schema_tokens

    direct = tool_schema_tokens(schemas)
    selector = tool_schema_tokens([category_selector_tool()])
    per_category = [
        tool_schema_tokens(tools_for_category(name, schemas)) for name in TOOL_CATEGORIES
    ]
    average = round(sum(per_category) / len(per_category)) if per_category else 0
    return {
        "direct": direct,
        "selector": selector,
        "average_category": average,
        "saved": max(0, direct - (selector + average)),
    }
