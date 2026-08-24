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
        "description": "Read a file, find files by name or glob, or see what you changed",
        "tools": [
            "read_file", "read_symbol", "list_files", "find_files", "find_and_read",
            # Reading what you DID, as opposed to what is there. Withheld
            # outside a repository by the `git` conditional family, so this
            # costs nothing in a workspace that is not one.
            "git_status", "git_diff", "git_log",
        ],
    },
    "write": {
        "description": "Create, edit, extend, rename or rewrite files",
        "tools": [
            "write_file",
            "patch_file",
            "replace_symbol",
            "append_file",
            "read_and_patch",
            "create_and_run",
            # Renaming belongs with writing, not with running. Left out of the
            # roster entirely, a model asked to rename reached for `run_command`
            # and a platform-specific shell verb.
            "move_file",
            "delete_file",
        ],
    },
    "search": {
        "description": "Search the code by meaning or pattern, query the code graph",
        "tools": ["search_files", "search_and_read", "graph_search", "explain_symbol"],
    },
    "web": {
        "description": "Look something up on the web that this workspace cannot answer",
        # Withheld entirely unless a backend is reachable and the user opted in,
        # so this category is empty on a normal local install.
        "tools": ["web_search", "fetch_url", "read_file"],
    },
    "run": {
        "description": "Run a shell command, the project, or its tests",
        "tools": ["run_command", "run_tests", "create_and_run"],
    },
    "plan": {
        "description": "Work out what to do and write the steps down, without changing anything",
        "tools": [
            # Read-only ON PURPOSE, and this is the whole value of the category.
            # smallcode's `planner.md` persona is a strong planner for one
            # reason visible in its frontmatter: `tools: [read_file, find_files,
            # search, hybrid_search, graph_search]` - no write tools, so it
            # CANNOT skip to implementing. That discipline costs us nothing and
            # needs no sub-agent: it is a category with the write tools left out.
            "read_file",
            "read_symbol",
            "list_files",
            "find_files",
            "search_files",
            "graph_search",
            "explain_symbol",
            "contract_create",
            "contract_from_plan",
            "contract_status",
        ],
    },
    "verify": {
        "description": "Write down what done means, and check it off",
        "tools": [
            "contract_create",
            "contract_from_plan",
            "contract_status",
            "contract_assert_pass",
            "contract_assert_fail",
            "contract_assert_skip",
            "run_tests",
        ],
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
# `ask_user` for the same reason, and more strongly: the moment a model needs
# to ask is the moment it has found an ambiguity mid-task, and a category
# switch standing between it and the question is exactly the friction that
# makes a small model guess instead. It is also the escape hatch `delete_file`
# and `write_file` point at when several files could be the target.
#
# `use_skill` for a plainer reason: the skill INDEX is injected into the system
# prompt on every turn, so a model on a small window was being shown a list of
# skills and then handed a tool set that could not open any of them. Direct-mode
# narrowing already kept it (`_narrowed_by_request`); two-stage routing dropped
# it, which is the half of the roster small models actually use.
ALWAYS_TOOLS = ("memory_load", "history_search", "ask_user", "use_skill")

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
