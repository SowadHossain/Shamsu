"""The raw-write protocol must actually reach the model that needs it.

This is the trap slice 1.4 exists to close. _TOOL_PROTOCOL_PROMPT - the only
place SHAMSU ever explained how to emit a tool call - is gated on
supports_native_tools=False. Every tier anchor carries supports_native_tools=True,
so qwen2.5-coder:7b-instruct and qwen3:8b have NEVER seen it. Its three examples
are read_file/run_command/ask_user, so nothing in the prompt ever taught a model
how to encode a multiline file. The model was then blamed for guessing wrong.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.agents.chat_loop import AgentChatLoop, _system_prompt
from shamsu.runtime.models import model_supports_native_tools
from shamsu.tools.agent_tools import AgentToolRegistry

NATIVE_TOOL_MODELS = ("qwen2.5-coder:7b-instruct", "qwen3:8b")


def test_the_tier_models_really_are_flagged_native():
    """Pins the premise. If this ever flips, the gating below changes meaning."""
    for name in NATIVE_TOOL_MODELS:
        assert model_supports_native_tools(name) is True, name


def test_the_raw_write_protocol_is_injected_even_for_native_tool_models():
    prompt = _system_prompt(
        Path("/ws"),
        include_tool_protocol=False,
        available_tools=("read_file", "write_file"),
    )

    assert "# write_file: templates/my_orders.html" in prompt
    assert "never as escaped JSON" in prompt


def test_the_raw_write_protocol_reaches_a_native_model_system_message(tmp_path: Path):
    """Through the real construction path, not just the helper."""
    loop = AgentChatLoop(
        tmp_path,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        model_name="qwen2.5-coder:7b-instruct",
    )

    assert loop._supports_native_tools is True
    system = loop.state.system_prompt
    assert "# write_file: " in system
    # The old JSON-only protocol is still correctly withheld from this model.
    assert "This model does not use native tool-calls" not in system


def test_a_read_only_loop_is_not_taught_to_write(tmp_path: Path):
    loop = AgentChatLoop(
        tmp_path,
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _request: True),
        model_name="qwen2.5-coder:7b-instruct",
        read_only=True,
    )

    assert "# write_file: " not in loop.state.system_prompt


def test_the_json_protocol_still_warns_non_native_models_off_escaping():
    prompt = _system_prompt(
        Path("/ws"),
        include_tool_protocol=True,
        available_tools=("read_file", "write_file"),
    )

    assert "This model does not use native tool-calls" in prompt
    # The JSON dialect must carry the same exception, since it is the one that
    # actively invites a model to put content in a JSON string.
    assert "file content is never JSON" in prompt


def test_the_write_guidance_tells_the_model_not_to_escape():
    prompt = _system_prompt(Path("/ws"), available_tools=("write_file",))

    assert "Send file content as a RAW block" in prompt


def test_raw_write_guidance_is_omitted_when_raw_write_tools_are_unavailable():
    prompt = _system_prompt(
        Path("/ws"),
        available_tools=("file.read", "file.patch", "run_command"),
    )

    assert "Send file content as a RAW block" not in prompt
    assert "# write_file: templates/my_orders.html" not in prompt


def test_raw_write_example_uses_an_available_raw_tool():
    prompt = _system_prompt(Path("/ws"), available_tools=("append_file",))

    assert "# append_file: templates/my_orders.html" in prompt
    assert "# write_file: templates/my_orders.html" not in prompt
