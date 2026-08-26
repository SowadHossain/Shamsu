"""Compaction should not spend a model call, or paraphrase, when it needn't.

Most evicted content in a build run is mechanical tool traffic. Summarizing that
with a local 7B costs a full round-trip AND is lossy: every fold paraphrases the
previous paraphrase, so paths and command names drift. Structured facts do not
drift, so they are tried first and the model is only asked when real reasoning
would otherwise be lost.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.agents.simple_state import ChatMessage
from shamsu.context.budget import RESERVE_OUTPUT_TOKENS, SAFETY_MARGIN_TOKENS, count_tokens
from shamsu.tools.agent_tools import AgentToolRegistry


class SilentClient:
    async def chat(self, model, messages, stream, options, **kwargs):
        return {"message": {"content": "ok", "tool_calls": []}}


def _loop(tmp_path: Path) -> AgentChatLoop:
    return AgentChatLoop(
        tmp_path,
        client=SilentClient(),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        model_name="qwen2.5-coder:7b-instruct",
    )


def _call(name: str, **arguments) -> dict:
    return {"function": {"name": name, "arguments": arguments}}


def test_tool_traffic_compacts_without_a_model_call(tmp_path: Path):
    evicted = [
        ChatMessage("user", "Create the orders page."),
        ChatMessage("assistant", "", tool_calls=[_call("read_file", filepath="core/views.py")]),
        ChatMessage("tool", "class Foo: pass", name="read_file"),
        ChatMessage("assistant", "", tool_calls=[_call("write_file", filepath="templates/x.html")]),
        ChatMessage("tool", "ok", name="write_file"),
        ChatMessage("assistant", "", tool_calls=[_call("run_command", command="pytest -q")]),
        ChatMessage("tool", "1 passed", name="run_command"),
    ]

    digest = _loop(tmp_path)._structured_compact("", evicted)

    assert digest
    assert "wrote: templates/x.html" in digest
    assert "read: core/views.py" in digest
    assert "ran: pytest -q" in digest
    assert "asked: Create the orders page." in digest


def test_the_digest_keeps_exact_paths_rather_than_paraphrasing_them(tmp_path: Path):
    """The whole point: a path must survive N compactions byte-exact."""
    evicted = [
        ChatMessage(
            "assistant",
            "",
            tool_calls=[_call("write_file", filepath="open_bazaar/core/migrations/0001_initial.py")],
        ),
        ChatMessage("tool", "ok", name="write_file"),
    ]

    digest = _loop(tmp_path)._structured_compact("", evicted)

    assert "open_bazaar/core/migrations/0001_initial.py" in digest


def test_a_prior_summary_is_carried_forward(tmp_path: Path):
    evicted = [
        ChatMessage("assistant", "", tool_calls=[_call("write_file", filepath="a.py")]),
        ChatMessage("tool", "ok", name="write_file"),
    ]

    digest = _loop(tmp_path)._structured_compact("- earlier: set up the project", evicted)

    assert "earlier: set up the project" in digest
    assert "wrote: a.py" in digest


def test_prose_heavy_eviction_defers_to_the_model_summary(tmp_path: Path):
    """Reasoning must not be flattened into a file list."""
    evicted = [
        ChatMessage(
            "assistant",
            "I considered putting auth in middleware, but the PRD's role matrix means "
            "it has to live on the model, so I am moving USERNAME_FIELD to core.User "
            "and leaving middleware alone. " * 6,
        ),
    ]

    assert _loop(tmp_path)._structured_compact("", evicted) == ""


def test_string_encoded_tool_arguments_are_still_extracted(tmp_path: Path):
    """Native tool_calls often arrive with arguments as a JSON string."""
    evicted = [
        ChatMessage(
            "assistant",
            "",
            tool_calls=[{"function": {"name": "write_file", "arguments": '{"filepath": "b.py"}'}}],
        ),
        ChatMessage("tool", "ok", name="write_file"),
    ]

    digest = _loop(tmp_path)._structured_compact("", evicted)

    assert "wrote: b.py" in digest


def test_repeated_targets_are_deduplicated(tmp_path: Path):
    evicted = [
        ChatMessage("assistant", "", tool_calls=[_call("write_file", filepath="a.py")]),
        ChatMessage("tool", "ok", name="write_file"),
        ChatMessage("assistant", "", tool_calls=[_call("write_file", filepath="a.py")]),
        ChatMessage("tool", "ok", name="write_file"),
    ]

    digest = _loop(tmp_path)._structured_compact("", evicted)

    assert digest.count("a.py") == 1


def test_harness_status_user_messages_are_not_recorded_as_requests(tmp_path: Path):
    """Corrections the harness wrote are not things the user asked for."""
    evicted = [
        ChatMessage("user", "(Answering the earlier question...) use JWT"),
        ChatMessage("assistant", "", tool_calls=[_call("write_file", filepath="a.py")]),
        ChatMessage("tool", "ok", name="write_file"),
    ]

    digest = _loop(tmp_path)._structured_compact("", evicted)

    assert "Answering the earlier question" not in digest


def test_nothing_to_report_yields_nothing(tmp_path: Path):
    assert _loop(tmp_path)._structured_compact("", []) == ""


@pytest.mark.asyncio
async def test_model_compaction_can_be_disabled_for_prd_style_runs(tmp_path: Path):
    loop = AgentChatLoop(
        tmp_path,
        client=SilentClient(),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        model_name="qwen2.5-coder:7b-instruct",
        use_model_compaction=False,
    )
    loop.state.append_user("Build the marketplace from the PRD.")
    loop.state.append_assistant(
        "I considered several architecture options and chose a Dockerized "
        "Postgres, backend, and frontend stack because the PRD needs a browser "
        "marketplace experience. " * 20
    )
    loop.state.append_user("Continue with the approved slice.")

    async def fail_summary(*_args, **_kwargs):
        raise AssertionError("model summary should not be called")

    loop._summarize_evicted = fail_summary  # type: ignore[method-assign]

    messages = await loop._messages_within_budget(num_ctx=1)

    summary = "\n".join(str(message.get("content", "")) for message in messages)
    assert "Continue with the approved slice" in summary
    assert "model summary was disabled" in loop.state.rolling_summary
    assert loop._last_context_evicted is True


@pytest.mark.asyncio
async def test_chat_context_hard_trims_oversized_current_prompt(tmp_path: Path):
    loop = AgentChatLoop(
        tmp_path,
        client=SilentClient(),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        model_name="qwen2.5-coder:7b-instruct",
    )
    loop.state.append_user(
        "Remove the login section from frontend/src/App.jsx.\n\n"
        + ("large stale context block " * 12000)
    )

    num_ctx = 8192
    messages = await loop._messages_within_budget(num_ctx=num_ctx)
    text = "\n".join(str(message.get("content", "")) for message in messages)

    assert "Remove the login section" in text
    assert "context hard-trimmed" in text
    assert count_tokens(text) <= int(
        (num_ctx - RESERVE_OUTPUT_TOKENS - SAFETY_MARGIN_TOKENS) * 0.70
    ) + 256
    assert loop._last_context_evicted is True
