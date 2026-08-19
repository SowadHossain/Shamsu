"""Simple mode: Ollama chat with coding tools attached.

Each test names a behaviour the user asked for, not a mechanism, so a later
refactor that quietly reintroduces the ceremony fails here.
"""
from __future__ import annotations

import asyncio
import time
import contextlib
import io
import json
import os
from pathlib import Path

import pytest

from shamsu.agents.chat_state import ChatState
from shamsu.agents.simple_chat import (
    CTX_BUCKETS,
    SIMPLE_TOOL_SCHEMAS,
    SIMPLE_TOOLS,
    SimpleChatLoop,
    command_needs_approval,
    make_approval_func,
    normalize_arguments,
    workspace_files,
    simple_mode_enabled,
)
from shamsu.agents.simple_prompt import build_instruction, simple_system_prompt
from shamsu.tools.agent_tools import AgentToolRegistry


class FakeClient:
    """Replays scripted model turns and records what it was sent."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.turns:
            return {"message": {"content": "done", "tool_calls": []}}
        return self.turns.pop(0)


def _text(content: str) -> dict:
    return {"message": {"content": content, "tool_calls": []}}


def _tool(name: str, **arguments) -> dict:
    return {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
        }
    }



def _grounding_of(call: dict) -> str:
    """The workspace-grounding system block, wherever the loop chose to put it.

    Deliberately found by CONTENT: its position is a cache decision, not a
    behaviour, and a test that pins the index fails for the wrong reason.
    """
    for message in call["messages"]:
        content = str(message.get("content", ""))
        if message.get("role") == "system" and (
            "Files in the workspace" in content or "workspace is empty" in content
        ):
            return content
    return ""


def _loop(tmp_path: Path, turns, **kwargs) -> SimpleChatLoop:
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    return SimpleChatLoop(
        tmp_path,
        client=FakeClient(turns),
        tools=tools,
        state=state,
        model_name="qwen3:8b",
        **kwargs,
    )


# --- plain chat ---------------------------------------------------------


def test_a_question_needing_no_tool_answers_like_ollama_chat(tmp_path):
    loop = _loop(tmp_path, [_text("Hey! What are we building?")])

    result = asyncio.run(loop.run("hi"))

    assert result.final == "Hey! What are we building?"
    assert result.tool_calls == 0
    assert result.rounds == 1
    assert not result.stopped


def test_the_model_is_sent_the_real_conversation_not_a_state_frame(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])

    asyncio.run(loop.run("remember: the port is 8080"))
    sent = loop.client.calls[0]["messages"]

    assert sent[0]["role"] == "system"
    assert sent[-1] == {"role": "user", "content": "remember: the port is 8080"}
    # None of the synthetic frame sections may appear.
    blob = json.dumps(sent)
    for section in ("[PHASE]", "[CURRENT TASK]", "[OUTPUT CONTRACT]", "[LATEST OBSERVATION]"):
        assert section not in blob, section


def test_earlier_turns_stay_in_the_conversation_so_continue_works(tmp_path):
    loop = _loop(tmp_path, [_text("first"), _text("second")])

    asyncio.run(loop.run("plan the game"))
    asyncio.run(loop.run("continue"))

    second_prompt = loop.client.calls[1]["messages"]
    contents = [m["content"] for m in second_prompt]
    assert "plan the game" in contents
    assert "first" in contents
    assert "continue" in contents


# --- tools --------------------------------------------------------------


def test_the_model_can_write_a_file_and_it_lands_on_disk(tmp_path):
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="hello.py", content="print('hi')\n"), _text("Created hello.py.")],
    )

    result = asyncio.run(loop.run("create hello.py that prints hi"))

    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert result.changed_files == ("hello.py",)
    assert result.final == "Created hello.py."


def test_the_real_tool_result_goes_back_into_the_same_conversation(tmp_path):
    (tmp_path / "notes.txt").write_text("the port is 8080", encoding="utf-8")
    loop = _loop(tmp_path, [_tool("read_file", filepath="notes.txt"), _text("Port 8080.")])

    asyncio.run(loop.run("what port?"))

    second_prompt = loop.client.calls[1]["messages"]
    tool_messages = [m for m in second_prompt if m["role"] == "tool"]
    assert tool_messages, "the tool result must be fed back"
    assert "8080" in tool_messages[0]["content"], "the REAL file content, not a paraphrase"


def test_writing_over_an_existing_file_does_not_need_an_overwrite_flag(tmp_path):
    (tmp_path / "a.py").write_text("old = 1\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="a.py", content="new = 2\n"), _text("Updated.")],
    )

    asyncio.run(loop.run("replace a.py"))

    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "new = 2\n"


def test_a_near_miss_argument_name_does_not_waste_a_round(tmp_path):
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")
    # `path` instead of `filepath` - what a small model reaches for.
    loop = _loop(tmp_path, [_tool("read_file", path="notes.txt"), _text("It says hello.")])

    asyncio.run(loop.run("read notes.txt"))

    tool_messages = [m for m in loop.client.calls[1]["messages"] if m["role"] == "tool"]
    assert '"ok": true' in tool_messages[0]["content"].lower()


def test_an_unknown_tool_is_answered_with_the_names_that_exist(tmp_path):
    loop = _loop(tmp_path, [_tool("delete_everything", filepath="x"), _text("Sorry.")])

    asyncio.run(loop.run("go"))

    tool_messages = [m for m in loop.client.calls[1]["messages"] if m["role"] == "tool"]
    assert "no tool called delete_everything" in tool_messages[0]["content"]
    assert "write_file" in tool_messages[0]["content"]


def test_only_the_seven_tools_are_offered(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])
    asyncio.run(loop.run("hi"))

    offered = {t["function"]["name"] for t in loop.client.calls[0]["tools"]}
    assert offered == {
        "read_file", "list_files", "search_files", "write_file", "patch_file",
        "run_command", "remember",
    }
    assert offered == set(SIMPLE_TOOLS)


# --- verification -------------------------------------------------------


def test_broken_code_comes_back_as_a_tool_result_the_model_can_fix(tmp_path):
    """Not a verdict panel and not a separate repair loop - just information."""
    loop = _loop(
        tmp_path,
        [
            _tool("write_file", filepath="bad.py", content="def broken(\n"),
            _text("Let me fix that."),
        ],
    )

    asyncio.run(loop.run("write bad.py"))

    second_prompt = loop.client.calls[1]["messages"]
    verify = [m for m in second_prompt if m["role"] == "tool" and m.get("name") == "verify"]
    assert verify, "a syntax error must reach the model"
    assert '"ok": false' in verify[0]["content"].lower()
    assert "bad.py" in verify[0]["content"]


def test_good_code_is_confirmed_without_ceremony(tmp_path):
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="ok.py", content="x = 1\n"), _text("Done.")],
    )

    result = asyncio.run(loop.run("write ok.py"))

    verify = [
        m for m in loop.client.calls[1]["messages"]
        if m["role"] == "tool" and m.get("name") == "verify"
    ]
    assert '"ok": true' in verify[0]["content"].lower()
    assert result.final == "Done."
    assert "UNVERIFIED" not in result.final


# --- approvals ----------------------------------------------------------


def test_writing_inside_the_workspace_does_not_ask(tmp_path):
    asked: list = []
    approve = make_approval_func(lambda r: asked.append(r) or True)

    class _Request:
        action_type = "file_write"

    assert approve(_Request()) is True
    assert not asked, "a write inside the sandbox must not raise a prompt"


def test_a_read_only_command_does_not_ask_but_an_install_does():
    """`git branch --show-current` raised two approval prompts before any work."""
    assert not command_needs_approval("git branch --show-current")
    assert not command_needs_approval("git status --short")
    assert not command_needs_approval("ls")
    assert command_needs_approval("npm install")
    assert command_needs_approval("pip install requests")


def test_a_dangerous_command_still_asks():
    assert command_needs_approval("rm -rf /")
    assert command_needs_approval("sudo shutdown now")


# --- context sizing -----------------------------------------------------


def test_every_call_in_a_session_uses_one_window(tmp_path):
    """Superseded 2026-08-18. Sizing num_ctx to the prompt was right at f16,
    where asking 32k for an 8k prompt spilled the cache to RAM and made first
    token take 83s. With a quantized KV cache the whole range costs 385 MiB
    (8192 -> 6506, 16384 -> 6702, 32768 -> 6891), so the saving is gone while
    the costs are not: changing num_ctx RELOADS the model, and the smaller
    bucket halves `output_reserve`, which starves a reasoning model into empty
    replies. Live, that combination produced 290s rounds and a failed turn.

    Prefill is charged on the actual prompt, not the window, so a big window is
    free in time. `_shrink_for_oom` still steps down if the GPU refuses.
    """
    loop = _loop(tmp_path, [_tool("list_files", path="."), _text("hi")])

    asyncio.run(loop.run("hello"))
    asyncio.run(loop.run("and again"))

    windows = {call["options"]["num_ctx"] for call in loop.client.calls}
    assert len(windows) == 1, f"one window per session, saw {sorted(windows)}"
    assert windows == {32768}


def test_the_window_grows_with_the_conversation_and_never_shrinks(tmp_path):
    # Sized to cross the first bucket under EITHER token counter: the vendored
    # tokenizer gives ~1 token per "x ", the chars/4 fallback gives ~0.5, and
    # which one is active depends on global lru_cache state another test may
    # have poisoned. The behaviour under test is growth, not a magic number.
    long_prompt = "x " * 20000
    loop = _loop(tmp_path, [_text("a"), _text("b")])

    asyncio.run(loop.run(long_prompt))
    grown = loop.client.calls[0]["options"]["num_ctx"]
    asyncio.run(loop.run("short"))
    after = loop.client.calls[1]["options"]["num_ctx"]

    assert grown >= CTX_BUCKETS[1]
    assert after == grown, "shrinking would force Ollama to reload the model"


# --- loop control -------------------------------------------------------


def test_an_endless_tool_loop_stops_and_says_how_to_resume(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    loop = _loop(tmp_path, [_tool("read_file", filepath="a.txt")] * 10, max_rounds=3)

    result = asyncio.run(loop.run("go"))

    assert result.stopped
    assert "continue" in result.final


def test_an_empty_model_reply_is_nudged_not_returned_as_the_answer(tmp_path):
    loop = _loop(tmp_path, [_text("   "), _text("Sorry - here is the answer.")])

    result = asyncio.run(loop.run("hi"))

    assert result.final == "Sorry - here is the answer."
    assert not result.stopped


def test_a_model_error_is_reported_verbatim_not_swallowed(tmp_path):
    class Broken:
        async def chat(self, **kwargs):
            raise RuntimeError("ollama is not running")

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    loop = SimpleChatLoop(
        tmp_path,
        client=Broken(),
        tools=tools,
        state=ChatState(simple_system_prompt(tmp_path), hydrate=False),
        model_name="qwen3:8b",
    )

    result = asyncio.run(loop.run("hi"))

    assert "ollama is not running" in result.final
    assert result.stopped


# --- prompt shape -------------------------------------------------------


def test_the_system_prompt_is_small_and_carries_no_prohibitions(tmp_path):
    """The legacy path sent 49 bullet rules, 24% of them prohibitions, with
    "do not claim complete" repeated four times."""
    import re

    from shamsu.context.budget import count_tokens

    prompt = simple_system_prompt(tmp_path)

    assert count_tokens(prompt) < 250
    assert not re.search(r"(?im)^\s*[-*]\s", prompt), "no bullet wall"
    lowered = prompt.lower()
    assert "do not" not in lowered
    assert "never" not in lowered


def test_build_seeds_one_instruction_rather_than_a_second_orchestrator():
    text = build_instruction("SPEC.md")

    assert "SPEC.md" in text
    assert "milestones" in text.lower()
    assert "one at a time" in text.lower()
    assert "continue" in text.lower()


def test_simple_mode_is_the_default_and_legacy_is_opt_in(monkeypatch):
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    assert simple_mode_enabled()

    monkeypatch.setenv("SHAMSU_LEGACY_ROUTING", "1")
    assert not simple_mode_enabled()


# --- REPL wiring --------------------------------------------------------


def test_simple_mode_short_circuits_the_router(monkeypatch):
    """Nothing above the simple loop may run: no orchestrator, no PRD-plan
    sniffing, no 27-branch route table."""
    import inspect

    import shamsu.cli.repl as repl

    source = inspect.getsource(repl._handle_request)
    head = source.split("AgentOrchestrator")[0]

    assert "_legacy_routing_enabled" in head
    assert "_run_simple_chat" in head
    assert head.index("_run_simple_chat") < head.index("agent_result") if "agent_result" in head else True


def test_build_seeds_the_instruction_only_for_a_real_file(tmp_path):
    import shamsu.cli.repl as repl

    (tmp_path / "SPEC.md").write_text("spec", encoding="utf-8")

    seeded = repl._simple_build_seed("build SPEC.md", tmp_path)
    assert "SPEC.md" in seeded and "milestones" in seeded.lower()

    # Ordinary prose must not be hijacked into a PRD run.
    assert repl._simple_build_seed("build the login page", tmp_path) == ""
    assert repl._simple_build_seed("build missing.md", tmp_path) == ""


def test_build_refuses_a_path_outside_the_workspace(tmp_path):
    import shamsu.cli.repl as repl

    assert repl._simple_build_seed("build ../../etc/hosts", tmp_path) == ""


def test_build_is_a_known_command_so_the_router_accepts_it():
    import shamsu.cli.repl as repl
    from shamsu.cli.command_router import CommandRouter

    route = CommandRouter(repl.SYSTEM_COMMANDS).route("/build SPEC.md")

    assert route.valid
    assert route.normalized == "build SPEC.md"


# --- lost the project, and answered instead of editing (live 2026-08-17) ------
#
# A long real session: it built the project fine, then drifted. It stopped
# calling tools and printed the code instead ("gave me the solution but didnt
# actally edit the file"), and it lost the layout - re-printing "ensure your
# files are structured like this" and creating scripts.js and game.js for the
# same job, because the early list_files result had aged out of the window.


def test_the_model_is_always_told_what_is_actually_on_disk(tmp_path):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "game.js").write_text("// game", encoding="utf-8")
    (tmp_path / "README.md").write_text("# x", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    asyncio.run(loop.run("what have we got?"))

    listing = [
        m for m in loop.client.calls[0]["messages"]
        if m["role"] == "system" and "workspace right now" in m["content"]
    ]
    assert listing, "the model must be grounded in the real tree"
    assert "frontend/game.js" in listing[0]["content"]
    assert "README.md" in listing[0]["content"]


def test_the_listing_is_rebuilt_every_call_so_it_cannot_go_stale(tmp_path):
    """This is the whole point: a file created mid-conversation must appear."""
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="new.py", content="x = 1\n"), _text("Made it.")],
    )

    asyncio.run(loop.run("create new.py"))

    first = _grounding_of(loop.client.calls[0])
    assert "new.py" not in first
    # The loop refreshes the listing at the top of each round.
    asyncio.run(loop.run("what files exist?"))
    latest = _grounding_of(loop.client.calls[-1])
    assert "new.py" in latest


def test_noise_directories_stay_out_of_the_listing(tmp_path):
    for junk in (".shamsu", "node_modules", "__pycache__", ".git"):
        (tmp_path / junk).mkdir()
        (tmp_path / junk / "junk.txt").write_text("x", encoding="utf-8")
    (tmp_path / "real.py").write_text("x = 1\n", encoding="utf-8")

    from shamsu.agents.simple_chat import workspace_files

    files = workspace_files(tmp_path)

    assert files == ["real.py"]


def test_showing_the_code_instead_of_writing_it_is_corrected(tmp_path):
    """The exact failure: 147s of generation, a complete game.js in a fence,
    and the file unchanged."""
    (tmp_path / "game.js").write_text("// old\n", encoding="utf-8")
    shown = (
        "Let me implement the fix.\n\n```javascript\n"
        "const canvas = document.getElementById('c');\n"
        "const ctx = canvas.getContext('2d');\n"
        "function loop() { requestAnimationFrame(loop); }\n"
        "loop();\n```\n\nSave this as game.js."
    )
    loop = _loop(
        tmp_path,
        [
            _text(shown),
            _tool("write_file", filepath="game.js", content="// new\n"),
            _text("Applied it."),
        ],
    )

    result = asyncio.run(loop.run("implement the fix"))

    assert (tmp_path / "game.js").read_text(encoding="utf-8") == "// new\n"
    assert result.changed_files == ("game.js",)
    nudge = [
        m for m in loop.client.calls[1]["messages"]
        if m["role"] == "user" and "did not change the file" in m["content"]
    ]
    assert nudge, "the model must be told it described a change without making it"
    assert "game.js" in nudge[0]["content"], "the correction must name the file"


def test_an_ordinary_answer_with_a_snippet_is_not_nagged(tmp_path):
    """A short shell snippet, or prose naming no workspace file, is a real answer."""
    (tmp_path / "game.js").write_text("// x\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("Run `pip install fastapi` and you're set.")])

    result = asyncio.run(loop.run("how do I install it?"))

    assert result.final.startswith("Run ")
    assert result.rounds == 1, "no extra round should be spent"


def test_the_nudge_gives_up_rather_than_looping_forever(tmp_path):
    """A model that only ever describes must still hand back."""
    from shamsu.agents.simple_chat import MAX_PROSE_NUDGES

    (tmp_path / "game.js").write_text("// old\n", encoding="utf-8")
    shown = "```js\na\nb\nc\nd\ne\n```\nThis is the new game.js."
    loop = _loop(tmp_path, [_text(shown)] * 8)

    result = asyncio.run(loop.run("fix it"))

    assert not result.stopped, "it should return the answer, not stall"
    nudges = [
        m for m in loop.state.all_messages
        if m.role == "user" and "did not change the file" in m.content
    ]
    assert len(nudges) == MAX_PROSE_NUDGES


# --- the real memory horizon --------------------------------------------
#
# ROOT CAUSE of "it lost what file structure we had": a fresh SimpleChatLoop is
# built per user message, each rehydrating from disk, and hydration was capped at
# 24 messages. Simple mode emits ~5 messages a turn, so a 32k window remembered
# FIVE TURNS. Everything older was dropped before the model saw anything.


def test_a_long_session_is_not_truncated_to_five_turns(tmp_path):
    from shamsu.agents.chat_state import HYDRATE_MAX_MESSAGES as LEGACY_CAP
    from shamsu.agents.simple_chat import HYDRATE_MAX_MESSAGES as SIMPLE_CAP

    assert LEGACY_CAP == 24, "the legacy default is unchanged"
    # ~5 messages per turn: the cap must cover a real working session, and the
    # token budget - not a message count - must be what finally trims.
    assert SIMPLE_CAP >= 200
    assert SIMPLE_CAP / 5 >= 40, "at least 40 turns of reach"


def test_the_hydration_horizon_is_configurable_not_hardcoded(tmp_path):
    """It was a module constant read directly, so no caller could widen it."""
    import inspect

    from shamsu.agents.chat_state import ChatState

    assert "hydrate_max_messages" in inspect.signature(ChatState.__init__).parameters


def test_simple_mode_asks_for_the_wide_horizon(tmp_path):
    import inspect

    from shamsu.agents import simple_chat

    source = inspect.getsource(simple_chat.SimpleChatLoop.__init__)

    assert "hydrate_max_messages=HYDRATE_MAX_MESSAGES" in source


def test_evicted_turns_survive_as_a_digest_instead_of_vanishing(tmp_path):
    """`select_for_budget` was called but `update_rolling_summary` never was, so
    anything that stopped fitting was hard-dropped."""
    from shamsu.agents.chat_state import ChatMessage
    from shamsu.agents.simple_chat import _digest

    evicted = [
        ChatMessage("user", "set up the backend with fastapi"),
        ChatMessage(
            "assistant",
            "",
            tool_calls=[{"function": {"name": "write_file", "arguments": {"filepath": "backend/main.py"}}}],
        ),
        ChatMessage("user", "now add the frontend"),
    ]

    digest = _digest("", evicted)

    assert "set up the backend with fastapi" in digest
    assert "backend/main.py" in digest
    assert "now add the frontend" in digest


def test_the_digest_accumulates_rather_than_replacing(tmp_path):
    from shamsu.agents.chat_state import ChatMessage
    from shamsu.agents.simple_chat import _digest

    first = _digest("", [ChatMessage("user", "milestone one")])
    second = _digest(first, [ChatMessage("user", "milestone two")])

    assert "milestone one" in second
    assert "milestone two" in second


def test_the_digest_reaches_the_model_when_history_is_evicted(tmp_path):
    loop = _loop(tmp_path, [_text("ok")] * 4)
    # Force eviction: a tiny window makes almost everything fall out.
    loop.state.append_user("the port is 8080 and the entry point is backend/main.py")
    # Each turn must be big enough to actually overflow the 3584-token budget an
    # 8192 ceiling leaves, or nothing is evicted and the test proves nothing.
    for i in range(6):
        loop.state.append_assistant("x " * 2000)
        loop.state.append_user(f"turn {i}")

    import shamsu.agents.simple_chat as sc

    original = sc.max_ctx
    sc.max_ctx = lambda: 8192
    try:
        messages = loop._messages()
    finally:
        sc.max_ctx = original

    summaries = [
        m for m in messages
        if m["role"] == "system" and "Summary of earlier conversation" in m["content"]
    ]
    assert summaries, "evicted turns must leave a trace"
    assert "8080" in summaries[0]["content"]


# --- the file tree, and what is inside those files -----------------------
#
# The listing answers "which files exist"; the code graph answers "what is in
# them". SHAMSU was maintaining a live graph (106 nodes for the asteroid
# workspace, re-indexed on every write) and reading it exactly zero times, while
# the model re-derived frontend/game.js from its own prose for a dozen turns.


def test_the_grounding_block_carries_both_the_tree_and_the_code_facts(tmp_path, monkeypatch):
    import shamsu.agents.simple_chat as sc

    (tmp_path / "game.js").write_text("function update() {}\n", encoding="utf-8")
    monkeypatch.setattr(
        sc, "codebase_brief", lambda *_a, **_k: "Codebase-Memory MCP facts:\n- game.js exports: update"
    )
    loop = _loop(tmp_path, [_text("ok")])

    asyncio.run(loop.run("fix game.js"))

    grounding = _grounding_of(loop.client.calls[0])
    assert "Files in the workspace right now" in grounding
    assert "game.js" in grounding
    assert "exports: update" in grounding


def test_the_code_lookup_happens_once_per_message_not_once_per_round(tmp_path, monkeypatch):
    """It costs ~2s and does not cache internally; paying it per tool round
    would add ~10s to a five-round turn for information that cannot change."""
    import shamsu.agents.simple_chat as sc

    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    calls: list[str] = []
    monkeypatch.setattr(sc, "codebase_brief", lambda _ws, text, **_k: calls.append(text) or "facts")

    loop = _loop(
        tmp_path,
        [
            _tool("read_file", filepath="a.txt"),
            _tool("read_file", filepath="a.txt"),
            _text("done"),
        ],
    )
    asyncio.run(loop.run("look at a.txt"))

    assert len(calls) == 1, f"expected one lookup for the turn, got {len(calls)}"


def test_a_conversational_turn_pays_nothing_for_the_lookup(tmp_path):
    """No existing file named -> return immediately, no MCP round-trip."""
    from shamsu.agents.simple_chat import codebase_brief

    assert codebase_brief(tmp_path, "hi how are you") == ""


def test_the_brief_is_cached_until_the_file_changes(tmp_path, monkeypatch):
    import shamsu.agents.simple_chat as sc

    target = tmp_path / "game.js"
    target.write_text("function a() {}\n", encoding="utf-8")
    lookups: list[int] = []

    def fake_build(_ws, targets):
        lookups.append(1)
        return f"facts for {targets}"

    monkeypatch.setattr("shamsu.abstract.context.build_codebase_memory_brief", fake_build)
    monkeypatch.setattr(
        "shamsu.agents.rewrite_fallback.mentioned_workspace_files",
        lambda *_a, **_k: ["game.js"],
    )
    sc._BRIEF_CACHE.clear()

    sc.codebase_brief(tmp_path, "edit game.js")
    sc.codebase_brief(tmp_path, "edit game.js")
    assert len(lookups) == 1, "an unchanged file must not be re-queried"

    import os
    stamp = target.stat().st_mtime + 10
    os.utime(target, (stamp, stamp))
    sc.codebase_brief(tmp_path, "edit game.js")
    assert len(lookups) == 2, "a changed file must invalidate the cache"


def test_an_unavailable_code_graph_does_not_break_the_turn(tmp_path, monkeypatch):
    import shamsu.agents.simple_chat as sc

    def boom(*_a, **_k):
        raise RuntimeError("mcp is down")

    monkeypatch.setattr("shamsu.abstract.context.build_codebase_memory_brief", boom)
    sc._BRIEF_CACHE.clear()
    (tmp_path / "game.js").write_text("x", encoding="utf-8")

    assert sc.codebase_brief(tmp_path, "edit game.js") == ""

    loop = _loop(tmp_path, [_text("still fine")])
    result = asyncio.run(loop.run("edit game.js"))
    assert result.final == "still fine"


# --- the GPU said no (live 2026-08-17) ----------------------------------
#
# `cudaMalloc failed: out of memory ... failed to allocate buffer for kv cache`.
# A second 5.1GB model was resident on an 8GB card leaving 1.25GB, and raising
# hydration 24 -> 400 made prompts big enough to demand the 32k bucket. The old
# message cap had been accidentally protecting the VRAM ceiling.


def test_the_default_context_is_the_full_32k(tmp_path):
    """32k fits at 100% GPU once the KV cache is q8_0 - measured 2026-08-18:
    f16 made 16k spill to CPU (47.5s); q8_0 put 32k on the GPU at 7.5s."""
    from shamsu.agents.simple_chat import max_ctx

    assert max_ctx() == 32768


def test_the_reply_reserve_scales_with_the_window(tmp_path):
    """A fixed 4096 reserve at 32k left the prompt 28160 and the reply the same
    4096 it got at 8k - the model spent it thinking and returned nothing."""
    from shamsu.agents.simple_chat import output_reserve

    assert output_reserve(8192) == 4096
    assert output_reserve(32768) == 8192
    assert output_reserve(65536) == 16384


def test_an_out_of_memory_reply_is_recognised():
    from shamsu.agents.simple_chat import looks_like_out_of_memory

    assert looks_like_out_of_memory(
        "cudaMalloc failed: out of memory alloc_tensor_range: failed to allocate "
        "CUDA0 buffer of size 2818572288"
    )
    assert looks_like_out_of_memory("llama_init_from_model: failed to initialize the context")
    assert not looks_like_out_of_memory("connection refused")


def test_the_loop_retries_smaller_instead_of_dying_on_oom(tmp_path):
    class OomThenFine:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise RuntimeError("cudaMalloc failed: out of memory")
            return {"message": {"content": "recovered", "tool_calls": []}}

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    loop = SimpleChatLoop(
        tmp_path,
        client=OomThenFine(),
        tools=tools,
        state=ChatState(simple_system_prompt(tmp_path), hydrate=False),
        model_name="qwen3:8b",
    )

    # A long prompt so the first attempt is above the smallest bucket - there
    # has to be somewhere to step DOWN to for this to prove anything.
    result = asyncio.run(loop.run("x " * 20000))

    assert result.final == "recovered"
    assert not result.stopped
    first, second = loop.client.calls[0], loop.client.calls[1]
    assert second["options"]["num_ctx"] < first["options"]["num_ctx"]


def test_the_smaller_window_sticks_for_the_rest_of_the_session(tmp_path):
    class AlwaysOom:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            raise RuntimeError("cudaMalloc failed: out of memory")

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    loop = SimpleChatLoop(
        tmp_path,
        client=AlwaysOom(),
        tools=tools,
        state=ChatState(simple_system_prompt(tmp_path), hydrate=False),
        model_name="qwen3:8b",
    )

    result = asyncio.run(loop.run("x " * 20000))

    sizes = [c["options"]["num_ctx"] for c in loop.client.calls]
    assert sizes == sorted(sizes, reverse=True), f"must step down, got {sizes}"
    assert len(set(sizes)) > 1, "it must actually try something smaller"
    # And when even the smallest fails, say what to do about it.
    assert result.stopped
    assert "ollama ps" in result.final


def test_a_full_gpu_frees_the_model_squatting_on_it(tmp_path, monkeypatch):
    """Stepping down cannot help when another model owns the card: 5.1GB
    resident of 8GB left 1.25GB, so every window failed."""
    import shamsu.runtime.ollama as ollama_rt

    unloaded: list[str] = []
    monkeypatch.setattr(
        ollama_rt, "list_loaded_models", lambda *_a, **_k: ["qwen2.5-coder:7b-instruct-q4_K_M", "qwen3:8b"]
    )
    monkeypatch.setattr(
        ollama_rt, "unload_model", lambda name, *_a, **_k: unloaded.append(name) or True
    )

    class OomUntilFreed:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            self.calls.append(kwargs)
            if not unloaded:
                raise RuntimeError("cudaMalloc failed: out of memory")
            return {"message": {"content": "fits now", "tool_calls": []}}

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    loop = SimpleChatLoop(
        tmp_path,
        client=OomUntilFreed(),
        tools=tools,
        state=ChatState(simple_system_prompt(tmp_path), hydrate=False),
        model_name="qwen3:8b",
    )

    result = asyncio.run(loop.run("hi"))

    assert result.final == "fits now"
    assert unloaded == ["qwen2.5-coder:7b-instruct-q4_K_M"], "only OTHER models get evicted"


# --- the readable transcript --------------------------------------------
#
# Simple mode stored a session_logger and never called it, so there was no
# record of what the model SAW or SAID. These pin the parts that were wrong.


def test_the_log_records_the_prompt_and_the_raw_response(tmp_path):
    from shamsu.agents.simple_log import SimpleTurnLog

    log = SimpleTurnLog(tmp_path, 1, "qwen3.5:9b-q4_K_M")
    log.open_turn("make hello.py")
    log.log_call(
        [{"role": "system", "content": "you are SHAMSU"}, {"role": "user", "content": "make hello.py"}],
        16384, 42,
    )
    log.log_response({"message": {"content": "here you go", "tool_calls": []}}, 1.5)
    log.close_turn("done", 1, stopped=False)

    text = log.path.read_text(encoding="utf-8")
    assert "you are SHAMSU" in text, "the system prompt must be visible"
    assert "make hello.py" in text
    assert "here you go" in text, "the raw response must be visible"
    assert "num_ctx 16,384" in text
    assert (log.dir / "latest.md").exists(), "latest.md is how you read it without hunting"


def test_a_pydantic_response_is_not_logged_as_empty(tmp_path):
    """The client returns ChatResponse, not a dict - reading it with .get()
    logged every single response as '(empty)' while text was plainly produced."""
    from shamsu.agents.simple_log import SimpleTurnLog

    class Message:
        content = "I created the file."
        thinking = "first I should write it"
        tool_calls = []

    class ChatResponse:
        message = Message()

    log = SimpleTurnLog(tmp_path, 1, "m")
    log.log_response(ChatResponse(), 2.0)

    text = log.path.read_text(encoding="utf-8")
    assert "I created the file." in text
    assert "first I should write it" in text, "the thinking channel is where the time goes"
    assert "*(empty)*" not in text


def test_log_headings_stay_ascii(tmp_path):
    """Windows consoles are cp1252; a non-ASCII heading breaks `type`/Get-Content."""
    from shamsu.agents.simple_log import SimpleTurnLog

    log = SimpleTurnLog(tmp_path, 1, "m")
    log.open_turn("hi")
    log.log_call([{"role": "user", "content": "hi"}], 8192, 2)
    log.log_response({"message": {"content": "hello"}}, 0.5)
    log.log_tool_result("write_file", {"filepath": "a.py"}, True, "Created a.py")
    log.close_turn("bye", 1, stopped=False)

    for line in log.path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or line.startswith("### tool"):
            line.encode("cp1252")  # raises if a heading is not console-safe


def test_logging_failure_never_breaks_the_turn(tmp_path):
    from shamsu.agents.simple_log import SimpleTurnLog

    log = SimpleTurnLog(tmp_path, 1, "m")
    log.path.unlink(missing_ok=True)
    log.dir.rmdir()
    log.open_turn("hi")          # must not raise
    log.log_response({"message": {"content": "x"}}, 0.1)
    log.close_turn("done", 1, stopped=False)



def test_an_empty_reply_cannot_spin_forever(tmp_path):
    """Unbounded, this branch ran all 24 rounds - half an hour of "Thinking..."
    with three consecutive `user` messages and no reply."""
    always_empty = [_text("") for _ in range(30)]
    loop = _loop(tmp_path, always_empty, max_rounds=24)

    result = asyncio.run(loop.run("okay proceed"))

    assert result.stopped
    assert len(loop.client.calls) <= 4, f"must give up early, made {len(loop.client.calls)} calls"
    assert "empty reply" in result.final


def test_the_transcript_never_stacks_user_messages(tmp_path):
    """Consecutive user turns with no assistant between them is a malformed
    conversation - the model stops seeing its own output."""
    loop = _loop(tmp_path, [_text(""), _text(""), _text("here you go")])

    asyncio.run(loop.run("okay proceed"))

    roles = [m["role"] for m in loop.client.calls[-1]["messages"]]
    for first, second in zip(roles, roles[1:]):
        assert not (first == "user" and second == "user"), f"stacked user turns: {roles}"


def test_a_reply_that_only_reasoned_is_used_not_discarded(tmp_path):
    """A model that thinks past its budget emits no visible content. That is a
    reply, not silence - re-asking just burns another 30s."""
    reasoned = {"message": {"content": "", "thinking": "The fix is to set speed = 2.", "tool_calls": []}}
    loop = _loop(tmp_path, [reasoned] * 6)

    result = asyncio.run(loop.run("how do I slow the ship?"))

    assert not result.stopped
    assert "speed = 2" in result.final


def test_an_empty_first_turn_is_retried_before_being_salvaged(tmp_path):
    """Salvaging on the FIRST empty turn ended the turn and did no work - the
    live probe lost turns 8-10 that way, main.py was never written."""
    reasoned = {"message": {"content": "", "thinking": "Let me think about this.", "tool_calls": []}}
    loop = _loop(
        tmp_path,
        [reasoned, _tool("write_file", filepath="main.py", content="print(1)"), _text("Wrote main.py.")],
    )

    result = asyncio.run(loop.run("write main.py"))

    assert (tmp_path / "main.py").exists(), "it must get another chance to call the tool"
    assert result.final == "Wrote main.py."


# --- truncated reads (live 2026-08-18) ----------------------------------
#
# `_compact_value` split its 6000-char budget EQUALLY across a dict's keys.
# read_file returns six, so content got 6000//6 = 1000 chars: a 4170-char file
# reached the model as 24% of itself under `"truncated": false`. The model said
# "the file read is being truncated in the response", re-read five times, and
# had no way to ask for the rest.


def test_a_normal_source_file_reaches_the_model_whole(tmp_path):
    from shamsu.tools.agent_tools import AgentToolRegistry

    source = "\n".join(f"const value{i} = {i};" for i in range(200))
    (tmp_path / "app.js").write_text(source, encoding="utf-8")
    reg = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)

    payload = json.loads(reg.read_file("app.js").to_json())

    assert payload["data"]["content"] == source, "the serializer must not silently clip it"
    assert "[truncated" not in payload["data"]["content"]


def test_one_big_field_is_not_starved_by_its_small_siblings(tmp_path):
    """The bug exactly: five tiny keys each reserved an equal share."""
    from shamsu.tools.agent_tools import _compact_value, COMPACT_VALUE_LIMIT

    data = {
        "filepath": "a.js", "resolved_filepath": "a.js", "total_lines": 156,
        "candidates": [], "truncated": False, "content": "x" * 9000,
    }
    out = _compact_value(data, COMPACT_VALUE_LIMIT)

    assert out["content"] == data["content"], "content must not pay for the metadata"


def test_an_oversized_result_stays_parseable(tmp_path):
    """Slicing raw JSON left an unterminated string - unreadable to the model."""
    from shamsu.agents.simple_chat import _budgeted

    payload = json.dumps({"ok": True, "message": "Read file.",
                          "data": {"filepath": "big.js", "content": "y" * 400000}})
    out = _budgeted(payload)

    parsed = json.loads(out)  # must not raise
    assert parsed["data"]["content_truncated"] is True
    assert "start_line" in parsed["data"]["content"], "say how to get the rest"


def test_the_model_can_ask_for_a_line_range(tmp_path):
    """Without this, a truncated read is a dead end - the only move left is to
    re-read and get the identical result, which is what it did five times."""
    read = next(t for t in SIMPLE_TOOL_SCHEMAS if t["function"]["name"] == "read_file")

    assert set(read["function"]["parameters"]["properties"]) >= {"start_line", "end_line"}
    assert read["function"]["parameters"]["required"] == ["filepath"]


def test_a_partly_read_file_cannot_be_overwritten_whole(tmp_path):
    """The data-loss path: rewrite from a fragment and the unseen tail is gone.
    The gutting guard misses it - that needs a 75% shrink AND zero declarations."""
    original = "\n".join(f"function f{i}() {{ return {i}; }}" for i in range(400))
    (tmp_path / "big.js").write_text(original, encoding="utf-8")

    loop = _loop(tmp_path, [_text("ok")])
    loop._partial_reads.add("big.js")

    result = loop._execute("write_file", {"filepath": "big.js", "content": "function f0() {}"})

    assert not result.ok
    assert "only seen part" in result.message
    assert "patch_file" in result.message, "tell it what to do instead"
    assert (tmp_path / "big.js").read_text(encoding="utf-8") == original, "file untouched"


def test_a_full_read_clears_the_block(tmp_path):
    (tmp_path / "small.js").write_text("const a = 1;\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])
    loop._partial_reads.add("small.js")

    loop._execute("read_file", {"filepath": "small.js"})

    assert loop._execute(
        "write_file", {"filepath": "small.js", "content": "const a = 2;\n"}
    ).ok, "once it has seen the whole file, writing must be allowed again"


# --- compaction that survives the process (2026-08-18) ------------------
#
# The rolling summary was held only in memory and rebuilt each turn from
# whatever hydration loaded, so a thread past the 400-message horizon lost its
# earliest turns entirely - evicted, never summarised, gone.


def test_the_summary_survives_a_restart(tmp_path):
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="long thread")
    state = ChatState(simple_system_prompt(tmp_path), session_logger=logger, hydrate=False)
    state.update_rolling_summary("- you asked: build an asteroids game\n- window is 900x700", 12)

    # a brand new process would build a fresh ChatState from the same session
    revived = ChatState(
        simple_system_prompt(tmp_path),
        session_logger=mgr.resume_session(logger.session_id),
        hydrate=True,
    )

    assert "900x700" in revived.rolling_summary, "compaction must outlive the process"


def test_a_session_written_before_this_feature_still_loads(tmp_path):
    """from_dict filters to known fields, so old session.json must not crash."""
    from shamsu.session.manager import SessionMetadata

    meta = SessionMetadata.from_dict(
        {"session_id": "s", "title": "t", "workspace": "w",
         "created_at": "c", "updated_at": "u"}
    )

    assert meta.summary == ""
    assert meta.summarized_upto == 1


def test_the_summary_cannot_grow_until_it_eats_the_window(tmp_path):
    from shamsu.agents.simple_chat import _bounded_summary, summary_budget
    from shamsu.context.budget import count_tokens

    budget = summary_budget(32768)
    lines = [f"- you asked: step {i}" for i in range(2000)]

    out = _bounded_summary(lines, budget)

    assert count_tokens(out) <= budget


def test_compaction_keeps_the_founding_decision_not_just_recent_chatter(tmp_path):
    """`lines[-14:]` kept the newest fourteen and threw away exactly the early
    decisions ('the window is 900x700') a long thread depends on."""
    from shamsu.agents.simple_chat import _bounded_summary, summary_budget

    lines = ["- you asked: the window is 900x700 and max speed is 4.5"]
    lines += [f"- you asked: tweak number {i}" for i in range(500)]

    out = _bounded_summary(lines, summary_budget(32768))

    assert "900x700" in out, "the founding decision must survive"
    assert "tweak number 499" in out, "and so must the most recent work"


def test_resuming_names_the_files_that_changed_while_away(tmp_path):
    """A resumed thread quotes file CONTENT from old reads; this says which of
    those memories to distrust."""
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="stale")
    (tmp_path / "changed.js").write_text("// edited after the session paused\n", encoding="utf-8")

    changed = logger.files_changed_since_last_activity()

    assert "changed.js" in changed


# --- P6: the archive is complete, the prompt is budgeted ----------------
#
# `messages.jsonl` clipped content at 16000 and tool_calls at 4000, so a
# `write_file` of a 10k file was recorded as a fragment and a resumed session
# saw less than the original. Raising the in-memory budget to ~32k chars the
# same day made the gap wider. Rule: truncate at READ, never at WRITE.


def test_a_big_tool_call_is_archived_whole(tmp_path):
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="archive")
    body = "x = 1\n" * 4000                      # ~24k chars, over every old cap
    logger.append_message(
        "assistant", "",
        tool_calls=[{"function": {"name": "write_file",
                                  "arguments": {"filepath": "big.py", "content": body}}}],
    )

    stored = logger.read_messages()[-1]
    written = stored["tool_calls"][0]["function"]["arguments"]["content"]

    assert written == body, f"archive lost {len(body) - len(written)} chars"
    assert "[truncated" not in json.dumps(stored)


def test_a_big_message_content_is_archived_whole(tmp_path):
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="archive")
    body = "line of a very large file\n" * 2000   # ~50k chars

    logger.append_message("tool", body, name="read_file")

    assert logger.read_messages()[-1]["content"] == body


def test_on_disk_fidelity_is_never_less_than_in_memory(tmp_path):
    """The invariant. Whatever the model was allowed to SEE must be at least
    what the transcript keeps, or resuming silently degrades the conversation."""
    from shamsu.agents.simple_chat import MAX_TOOL_RESULT_TOKENS
    from shamsu.session.manager import SessionManager

    in_memory_chars = MAX_TOOL_RESULT_TOKENS * 4
    payload = "y" * in_memory_chars

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="invariant")
    logger.append_message("tool", payload, name="read_file")

    assert len(logger.read_messages()[-1]["content"]) >= in_memory_chars


def test_secrets_are_still_removed_from_the_archive(tmp_path):
    """Lossless must not mean unredacted - redaction is not truncation."""
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="secrets")
    logger.append_message(
        "assistant", "",
        tool_calls=[{"function": {"name": "run_command",
                                  "arguments": {"command": "deploy", "api_key": "sk-abc123"}}}],
    )

    blob = json.dumps(logger.read_messages()[-1])
    assert "sk-abc123" not in blob
    assert "[REDACTED]" in blob


def test_the_markdown_log_keeps_whole_tool_results(tmp_path):
    from shamsu.agents.simple_log import SimpleTurnLog

    log = SimpleTurnLog(tmp_path, 1, "m")
    body = "const value = 1;\n" * 1000            # ~17k chars

    log.log_tool_result("read_file", {"filepath": "a.js"}, True, body)

    text = log.path.read_text(encoding="utf-8")
    assert "more chars]" not in text
    assert text.count("const value = 1;") == 1000


# --- P1: one log file per THREAD, not per turn --------------------------
#
# The counter was `number of files in the directory`, so numbering was
# workspace-global, carried no session id, and two threads interleaved as
# turn-001..turn-011 with no way to tell them apart.


def test_a_thread_is_one_log_file_across_many_turns(tmp_path):
    from shamsu.agents.simple_log import SimpleTurnLog, next_turn_number

    for message in ("first thing", "second thing", "third thing"):
        log = SimpleTurnLog(
            tmp_path, next_turn_number(tmp_path, "sess-A"), "m",
            session_id="sess-A", session_title="Asteroids Game",
        )
        log.open_turn(message)
        log.close_turn("ok", 1, stopped=False)

    files = sorted(p.name for p in (tmp_path / ".shamsu/chat-logs").glob("*.md"))
    assert files == ["latest.md", "sess-A--asteroids-game.md"], files
    text = (tmp_path / ".shamsu/chat-logs/sess-A--asteroids-game.md").read_text(encoding="utf-8")
    assert text.count("# Turn ") == 3
    for message in ("first thing", "second thing", "third thing"):
        assert message in text


def test_two_sessions_do_not_share_a_file_or_a_counter(tmp_path):
    from shamsu.agents.simple_log import SimpleTurnLog, next_turn_number

    for sid, title, message in (("sess-A", "Game", "game work"), ("sess-B", "Api", "api work")):
        log = SimpleTurnLog(tmp_path, next_turn_number(tmp_path, sid), "m",
                            session_id=sid, session_title=title)
        log.open_turn(message)
        log.close_turn("ok", 1, stopped=False)

    root = tmp_path / ".shamsu/chat-logs"
    a = (root / "sess-A--game.md").read_text(encoding="utf-8")
    b = (root / "sess-B--api.md").read_text(encoding="utf-8")

    assert "game work" in a and "api work" not in a
    assert "api work" in b and "game work" not in b
    # each thread starts its own numbering
    assert "# Turn 1 " in a and "# Turn 1 " in b


def test_turn_numbers_survive_a_restart(tmp_path):
    """The number must come from the thread's own file, not process state."""
    from shamsu.agents.simple_log import SimpleTurnLog, next_turn_number

    log = SimpleTurnLog(tmp_path, next_turn_number(tmp_path, "s"), "m", session_id="s")
    log.open_turn("one")
    log.close_turn("ok", 1, stopped=False)

    # a brand new process would call next_turn_number again
    assert next_turn_number(tmp_path, "s") == 2


def test_latest_points_at_the_thread_rather_than_copying_it(tmp_path):
    """The session log is lossless and can reach megabytes - copying it every
    turn would double writes and disk for nothing."""
    from shamsu.agents.simple_log import SimpleTurnLog

    log = SimpleTurnLog(tmp_path, 1, "m", session_id="sess-A", session_title="Game")
    log.open_turn("hello")
    log.log_tool_result("read_file", {"filepath": "a.js"}, True, "x" * 50000)
    log.close_turn("done", 1, stopped=False)

    latest = (tmp_path / ".shamsu/chat-logs/latest.md").read_text(encoding="utf-8")
    assert "sess-A--game.md" in latest
    assert len(latest) < 500, "latest.md must be a pointer, not a copy"


# --- P2/P3b: ownership, unlimited length, re-grounding ------------------


def test_the_same_process_reattaches_to_its_own_session(tmp_path):
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    first, _ = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)

    second, _ = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)

    assert second.session_id == first.session_id


def test_a_second_window_gets_its_own_session(tmp_path, monkeypatch):
    """`latest_active()` had no notion of who was using a session, so two REPLs
    both attached to it, both appended to one transcript, and each one's
    hydration pulled in the other's turns."""
    import json as _json
    import psutil
    from datetime import datetime, timezone
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    first, _ = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)

    # Another LIVE process holds it: a foreign pid with a fresh heartbeat.
    foreign_pid = os.getpid() + 1
    monkeypatch.setattr(psutil, "pid_exists", lambda pid: pid == foreign_pid)
    first.owner_path.write_text(
        _json.dumps({"pid": foreign_pid,
                     "heartbeat": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )

    assert first.claimed_by_other_live_process()

    second, reason = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)

    assert second.session_id != first.session_id, "must not interleave two threads"
    assert "another window" in reason


def test_a_crashed_owner_does_not_lock_the_session_forever(tmp_path):
    """Heartbeat AND pid are both checked: a stale claim must expire, or a crash
    would strand the thread permanently."""
    import json as _json
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger, _ = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    logger.owner_path.write_text(
        _json.dumps({"pid": 999999, "heartbeat": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )

    assert not logger.claimed_by_other_live_process()


def test_our_own_claim_never_blocks_us(tmp_path):
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger, _ = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    logger.claim()

    assert not logger.claimed_by_other_live_process()


def test_coming_back_days_later_still_resumes(tmp_path):
    """8 hours used to fork a new thread silently - overnight always broke."""
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    first, _ = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    first.metadata.updated_at = "2020-01-01T00:00:00+00:00"
    mgr._write_metadata(first.metadata)
    mgr._upsert_index(first.metadata)

    again, reason = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)

    assert again.session_id == first.session_id
    assert "days ago" in reason


# --- P4: a rewrite that cannot fit must not be attempted ----------------


def test_a_file_too_big_to_rewrite_is_refused_with_the_alternative(tmp_path):
    """The reply reserve caps what one turn can emit, so rewriting a larger
    file is cut off partway and the tail is lost. patch_file costs the same at
    any file size."""
    from shamsu.agents.simple_chat import max_ctx, output_reserve

    writable = output_reserve(max_ctx()) * 4
    big = tmp_path / "huge.js"
    big.write_text("x" * (writable + 5000), encoding="utf-8")
    original = big.read_text(encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("write_file", {"filepath": "huge.js", "content": "tiny"})

    assert not result.ok
    assert "patch_file" in result.message
    assert big.read_text(encoding="utf-8") == original, "file must be untouched"


def test_an_ordinary_file_is_still_rewritable(tmp_path):
    (tmp_path / "small.js").write_text("const a = 1;\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    assert loop._execute(
        "write_file", {"filepath": "small.js", "content": "const a = 2;\n"}
    ).ok


def test_creating_a_new_file_is_never_blocked(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])

    assert loop._execute("write_file", {"filepath": "brand_new.js", "content": "x"}).ok


def test_write_file_points_at_patch_file_for_existing_files(tmp_path):
    write = next(t for t in SIMPLE_TOOL_SCHEMAS if t["function"]["name"] == "write_file")

    assert "patch_file" in write["function"]["description"]


def test_resuming_reports_files_changed_while_the_thread_was_away(tmp_path):
    """Measured from BEFORE the resume. Reading `updated_at` afterwards returns
    "now" - resuming logs an event that bumps it - so the answer was always an
    empty list, which reads exactly like "nothing changed". Caught live."""
    import time as _time
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    first, _ = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    first.release()

    _time.sleep(1.1)                       # mtime resolution
    (tmp_path / "edited_while_away.js").write_text("// changed\n", encoding="utf-8")

    again, _ = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)

    assert "edited_while_away.js" in again.files_changed_since_last_activity()


def test_the_explicit_resume_command_reports_them_too(tmp_path):
    import time as _time
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    first = mgr.create_session(title="thread")
    first.release()

    _time.sleep(1.1)
    (tmp_path / "touched.js").write_text("// changed\n", encoding="utf-8")

    again = mgr.resume_session(first.session_id)

    assert "touched.js" in again.files_changed_since_last_activity()


def test_a_long_turn_does_not_let_another_window_steal_the_thread(tmp_path):
    """Claiming only at resume left the heartbeat stale after 5 minutes, so a
    second window would decide the session was free mid-conversation."""
    import json as _json
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger, _ = mgr.resume_or_start(max_age_seconds=8 * 3600, max_messages=200)
    # Age the claim past the staleness window.
    logger.owner_path.write_text(
        _json.dumps({"pid": os.getpid(), "heartbeat": "2020-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )

    loop = _loop(tmp_path, [_text("ok")], session_logger=logger)
    asyncio.run(loop.run("keep working"))

    beat = _json.loads(logger.owner_path.read_text(encoding="utf-8"))["heartbeat"]
    assert beat.startswith("20"), beat
    assert "2020-01-01" not in beat, "each turn must refresh the claim"


def test_a_budget_clipped_read_also_blocks_a_whole_file_rewrite(tmp_path):
    """`_budgeted` trims AFTER `_execute`, so the guard's own inspection saw a
    complete result. Without this the model gets a clipped file and is still
    allowed to rewrite it whole - losing everything it never saw."""
    from shamsu.agents.simple_chat import MAX_TOOL_RESULT_TOKENS

    # Bigger than one tool result may occupy, so _budgeted must clip it.
    huge = "const line = 1;\n" * (MAX_TOOL_RESULT_TOKENS)
    (tmp_path / "huge.js").write_text(huge, encoding="utf-8")

    loop = _loop(tmp_path, [_tool("read_file", filepath="huge.js"), _text("done")])
    asyncio.run(loop.run("read huge.js"))

    assert "huge.js" in loop._partial_reads, "a clipped read must mark the file partial"
    blocked = loop._execute("write_file", {"filepath": "huge.js", "content": "tiny"})
    assert not blocked.ok
    assert (tmp_path / "huge.js").read_text(encoding="utf-8") == huge


def test_a_truncated_read_names_the_exact_next_call(tmp_path):
    """"Read the rest with start_line" was too vague to act on: live
    2026-08-18 the model re-read the same file twice, got the same head both
    times, and gave up without ever trying a range or patch_file."""
    from shamsu.agents.simple_chat import _budgeted

    body = "\n".join(f"line {i}" for i in range(6000))
    payload = json.dumps({"ok": True, "message": "Read file.",
                          "data": {"filepath": "big.js", "total_lines": 6000,
                                   "content": body}})

    data = json.loads(_budgeted(payload))["data"]

    shown = data["shown_lines"]
    assert data["content_truncated"] is True
    assert f"lines 1-{shown} of 6000" in data["content"], "say what you DID show"
    assert f"start_line={shown + 1}" in data["content"], "name the exact next call"
    assert "patch_file" in data["content"], "and how to change it"


# --- P3.2: the digest carries DECISIONS, not just questions -------------


def test_compaction_records_what_was_decided_not_just_what_was_asked(tmp_path):
    """The deterministic digest says "you asked to slow the ship" and never
    "we set maxSpeed to 4.5" - the half a later turn actually needs."""
    class Narrating:
        def __init__(self):
            self.asks = []

        async def chat(self, **kwargs):
            body = kwargs["messages"][-1]["content"]
            self.asks.append(body)
            if "DECISIONS" in body:
                return {"message": {"content": "- maxSpeed is 4.5\n- window is 900x700"}}
            return {"message": {"content": "ok", "tool_calls": []}}

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    loop = SimpleChatLoop(
        tmp_path, client=Narrating(), tools=tools, state=state, model_name="qwen3:8b",
    )
    # Enough history that the budget must evict some of it.
    for i in range(60):
        state.append_user(f"turn {i}: " + ("filler text " * 200))
        state.append_assistant("noted " + ("more filler " * 200))

    asyncio.run(loop.run("carry on"))

    assert "maxSpeed is 4.5" in state.rolling_summary
    assert any("DECISIONS" in ask for ask in loop.client.asks), "must ask for decisions"


def test_a_failed_summary_call_leaves_the_deterministic_digest_standing(tmp_path):
    """The model call only ever ADDS - if it fails, compaction must not."""
    class Failing:
        async def chat(self, **kwargs):
            if "DECISIONS" in kwargs["messages"][-1]["content"]:
                raise RuntimeError("model unavailable")
            return {"message": {"content": "ok", "tool_calls": []}}

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    loop = SimpleChatLoop(
        tmp_path, client=Failing(), tools=tools, state=state, model_name="qwen3:8b",
    )
    for i in range(60):
        state.append_user(f"turn {i}: " + ("filler text " * 200))
        state.append_assistant("noted " + ("more filler " * 200))

    result = asyncio.run(loop.run("carry on"))

    assert not result.stopped, "a failed summary must not break the turn"
    assert state.rolling_summary.strip(), "the deterministic digest still stands"


# --- P3.4: /compact makes an invisible mechanism inspectable ------------


class _Recorder:
    def __init__(self): self.lines = []
    def print(self, text=""): self.lines.append(str(text))
    @property
    def text(self): return "\n".join(self.lines)


def test_compact_shows_what_the_model_is_told_about_earlier_work(tmp_path):
    from shamsu.cli.repl import _handle_compact
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session(title="t")
    logger.save_summary("- maxSpeed is 4.5\n- window is 900x700", 12)
    out = _Recorder()

    _handle_compact("compact", logger, out)

    assert "maxSpeed is 4.5" in out.text
    assert "message 12" in out.text


def test_compact_says_so_when_nothing_has_been_compacted(tmp_path):
    from shamsu.cli.repl import _handle_compact
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session(title="t")
    out = _Recorder()

    _handle_compact("compact", logger, out)

    assert "Nothing compacted yet" in out.text


def test_compact_clear_forgets_the_summary_but_not_the_transcript(tmp_path):
    from shamsu.cli.repl import _handle_compact
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session(title="t")
    logger.append_message("user", "the port is 8080")
    logger.save_summary("- something stale", 5)
    out = _Recorder()

    _handle_compact("compact clear", logger, out)

    assert logger.load_summary()[0] == ""
    assert "the port is 8080" in json.dumps(logger.read_messages()), "transcript survives"


def test_decisions_outrank_routine_asks_in_the_summary(tmp_path):
    """Live 2026-08-18: eight lines of "you asked: step 45: filler..." nearly
    buried the two lines naming the actual decisions. Decisions go first, so
    the head of a bounded summary keeps them."""
    class Narrating:
        async def chat(self, **kwargs):
            if "DECISIONS" in kwargs["messages"][-1]["content"]:
                return {"message": {"content": "- token TTL is 900s\n- Redis on 6380"}}
            return {"message": {"content": "ok", "tool_calls": []}}

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    loop = SimpleChatLoop(
        tmp_path, client=Narrating(), tools=tools, state=state, model_name="qwen3:8b",
    )
    for i in range(60):
        state.append_user(f"step {i}: " + ("filler " * 250))
        state.append_assistant("done " + ("filler " * 250))

    asyncio.run(loop.run("carry on"))

    lines = [l for l in state.rolling_summary.splitlines() if l.strip()]
    assert "TTL is 900s" in lines[0], f"decisions must lead, got {lines[0]!r}"
    # Protected by ORDER, not by trimming the asks - `_bounded_summary` keeps
    # both ends, so leading with decisions guarantees they outlive the middle.
    assert "Redis on 6380" in state.rolling_summary


def test_compaction_does_not_change_the_context_window(tmp_path):
    """A different num_ctx makes Ollama RELOAD the model (~5s measured), and
    compaction would pay it twice - down then back up - every time it fires."""
    seen = []

    class Watching:
        async def chat(self, **kwargs):
            seen.append(kwargs["options"]["num_ctx"])
            if "DECISIONS" in kwargs["messages"][-1]["content"]:
                return {"message": {"content": "- a decision"}}
            return {"message": {"content": "ok", "tool_calls": []}}

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    loop = SimpleChatLoop(
        tmp_path, client=Watching(), tools=tools, state=state, model_name="qwen3:8b",
    )
    for i in range(60):
        state.append_user(f"step {i}: " + ("filler " * 250))
        state.append_assistant("done " + ("filler " * 250))
    asyncio.run(loop.run("carry on"))

    assert len(set(seen)) == 1, (
        f"one window for the whole turn, saw {sorted(set(seen))} - each change "
        "reloads the model"
    )


# --- a transcript that is no longer JSONL (live 2026-08-18) -------------
#
# A 99 KB transcript had been reformatted into indented JSON - an editor
# opening a `.jsonl` does that - and 655 of 657 lines failed to parse.
# `read_messages` skipped them SILENTLY and returned one message. The session
# hydrated almost no history, the agent floundered for 15 minutes, and nothing
# anywhere said why.


def test_a_reformatted_transcript_is_recovered_not_silently_lost(tmp_path):
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="reformatted")
    for i in range(20):
        logger.append_message("user", f"message {i}")

    # Reformat it the way an editor would: indented, one object over many lines.
    records = logger.read_messages()
    logger.messages_path.write_text(
        "\n".join(json.dumps(r, indent=4) for r in records), encoding="utf-8"
    )

    revived = mgr.logger_for(logger.session_id)
    recovered = revived.read_messages()

    assert len(recovered) == 20, f"history must survive, got {len(recovered)}"
    assert recovered[0]["content"] == "message 0"
    assert revived.recovered_message_count == 20, "and it must be REPORTED, not silent"


def test_a_healthy_transcript_does_not_take_the_recovery_path(tmp_path):
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="healthy")
    for i in range(5):
        logger.append_message("user", f"message {i}")

    revived = mgr.logger_for(logger.session_id)
    assert len(revived.read_messages()) == 5
    assert revived.recovered_message_count == 0, "normal files must not look rescued"


def test_recovery_keeps_working_with_a_count_limit(tmp_path):
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="limited")
    for i in range(30):
        logger.append_message("user", f"message {i}")
    records = logger.read_messages()
    logger.messages_path.write_text(
        "\n".join(json.dumps(r, indent=2) for r in records), encoding="utf-8"
    )

    tail = mgr.logger_for(logger.session_id).read_messages(5)

    assert len(tail) == 5
    assert tail[-1]["content"] == "message 29", "must still be the NEWEST five"


def test_a_partly_mangled_transcript_recovers_what_it_can(tmp_path):
    from shamsu.session.manager import SessionManager

    mgr = SessionManager(tmp_path)
    logger = mgr.create_session(title="mangled")
    for i in range(10):
        logger.append_message("user", f"message {i}")
    logger.messages_path.write_text(
        "\n".join(json.dumps(r, indent=2) for r in logger.read_messages())
        + "\n{ this is not json at all\n",
        encoding="utf-8",
    )

    recovered = mgr.logger_for(logger.session_id).read_messages()

    assert len(recovered) == 10, "garbage at the end must not cost the whole file"


# --- no-progress detection (live 2026-08-18) ----------------------------
#
# One turn ran 12 no-op patches ("old_string and new_string are identical")
# and 5 failed ones across 24 rounds and ~25 minutes, changing nothing. Only
# max_rounds stopped it, and the user was told "I stopped after 24 steps"
# rather than what had actually gone wrong.


def test_repeated_edits_that_change_nothing_stop_the_turn(tmp_path):
    (tmp_path / "a.js").write_text("const a = 1;\n", encoding="utf-8")
    # Every patch matches text that is not there.
    turns = [_tool("patch_file", filepath="a.js", old_string="NOT PRESENT",
                   new_string="x") for _ in range(12)]
    loop = _loop(tmp_path, turns, max_rounds=24)

    result = asyncio.run(loop.run("fix it"))

    assert result.stopped
    assert result.rounds < 12, f"must give up early, ran {result.rounds} rounds"
    assert "changed nothing" in result.final
    assert "exact text" in result.final, "and say what would help"


def test_a_no_op_patch_counts_as_no_progress(tmp_path):
    """old_string == new_string 'succeeds' while leaving the file untouched -
    12 of those in one live turn."""
    from shamsu.agents.simple_chat import _changed_nothing
    from shamsu.types import ToolResult

    assert _changed_nothing(
        ToolResult(True, "old_string and new_string are identical; nothing to change.", {})
    )
    assert _changed_nothing(ToolResult(False, "old_string not found in a.js.", {}))
    assert not _changed_nothing(ToolResult(True, "Edited a.js (1 replacement).", {}))


def test_a_successful_edit_resets_the_counter(tmp_path):
    """Progress must clear the count, or a long legitimate session trips it."""
    (tmp_path / "a.js").write_text("const a = 1;\n", encoding="utf-8")
    turns = [
        _tool("patch_file", filepath="a.js", old_string="MISSING", new_string="x"),
        _tool("patch_file", filepath="a.js", old_string="MISSING", new_string="x"),
        _tool("patch_file", filepath="a.js", old_string="const a = 1;", new_string="const a = 2;"),
        _tool("patch_file", filepath="a.js", old_string="MISSING", new_string="x"),
        _text("done"),
    ]
    loop = _loop(tmp_path, turns, max_rounds=24)

    result = asyncio.run(loop.run("fix it"))

    assert not result.stopped, "a real edit in the middle must reset the count"
    assert (tmp_path / "a.js").read_text(encoding="utf-8") == "const a = 2;\n"


def test_reading_a_file_in_pieces_still_allows_writing_it(tmp_path):
    """Told to read a large file with start_line/end_line, the model would read
    ALL of it in ranges and still be refused a write - forever."""
    body = "\n".join(f"line {i}" for i in range(300))
    (tmp_path / "big.js").write_text(body, encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    loop._execute("read_file", {"filepath": "big.js", "start_line": 1, "end_line": 150})
    assert not loop._execute("write_file", {"filepath": "big.js", "content": "x"}).ok

    loop._execute("read_file", {"filepath": "big.js", "start_line": 151, "end_line": 300})

    # The partial-read guard has now released. A DIFFERENT guard takes over -
    # big existing files are steered to patch_file - and that one has its own
    # exit, so the deliberate second attempt lands.
    released = loop._execute("write_file", {"filepath": "big.js", "content": "x"})
    assert not released.data.get("partial_read"), "the partial-read guard never released"
    assert released.data.get("prefer") == "patch_file"
    assert loop._execute("write_file", {"filepath": "big.js", "content": "x"}).ok


def test_a_gap_in_the_ranges_still_counts_as_partial(tmp_path):
    body = "\n".join(f"line {i}" for i in range(300))
    (tmp_path / "big.js").write_text(body, encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    loop._execute("read_file", {"filepath": "big.js", "start_line": 1, "end_line": 100})
    loop._execute("read_file", {"filepath": "big.js", "start_line": 200, "end_line": 300})

    assert not loop._execute("write_file", {"filepath": "big.js", "content": "x"}).ok


# --- repeated blind fixes (live 2026-08-18) -----------------------------
#
# 7 successful patches to one file in a single turn, chasing an error that had
# ALREADY been fixed - the console message was a stale browser cache. Each edit
# changed the file, so the no-change counter never saw them, and the model never
# once said it could not verify any of it.


def _edit(n: int) -> dict:
    return _tool("patch_file", filepath="game.js",
                 old_string=f"const v = {n};", new_string=f"const v = {n + 1};")


def test_repeatedly_editing_one_file_without_confirming_stops(tmp_path):
    (tmp_path / "game.js").write_text(
        "\n".join(f"const v = {i};" for i in range(10)), encoding="utf-8"
    )
    loop = _loop(tmp_path, [_edit(i) for i in range(9)], max_rounds=24, verify_changes=False)

    result = asyncio.run(loop.run("fix the error"))

    assert result.stopped
    assert result.rounds < 9, f"must stop early, ran {result.rounds}"
    assert "without being able to confirm" in result.final
    assert "git diff" in result.final, "warn that blind edits can undo good code"


def test_it_is_warned_before_it_is_stopped(tmp_path):
    (tmp_path / "game.js").write_text(
        "\n".join(f"const v = {i};" for i in range(10)), encoding="utf-8"
    )
    loop = _loop(tmp_path, [_edit(i) for i in range(9)], max_rounds=24, verify_changes=False)

    asyncio.run(loop.run("fix the error"))

    nudges = [
        m.content for m in loop.state.all_messages
        if m.role == "user" and "cannot confirm" in m.content
    ]
    assert nudges, "it must be told before it is cut off"
    assert "ask for the exact error" in nudges[0]


def test_editing_several_different_files_is_not_penalised(tmp_path):
    """The signal is repetition on ONE file, not activity in general."""
    for name in ("a.js", "b.js", "c.js", "d.js", "e.js"):
        (tmp_path / name).write_text("const v = 0;\n", encoding="utf-8")
    turns = [
        _tool("patch_file", filepath=n, old_string="const v = 0;", new_string="const v = 1;")
        for n in ("a.js", "b.js", "c.js", "d.js", "e.js")
    ] + [_text("all done")]
    loop = _loop(tmp_path, turns, max_rounds=24, verify_changes=False)

    result = asyncio.run(loop.run("update them all"))

    assert not result.stopped
    assert result.final == "all done"


# --- the model must SEE what its edit did -------------------------------
#
# A mutation reported "Edited game.js: +3 -1 lines" - a count, not a change. So
# the model could not tell a fix from an edit that removed the wrong thing, and
# live 2026-08-18 it patched one file seven times without seeing any result.
# The harness was already writing a real diff per edit to .shamsu/mutations/
# and never showing it.


def test_an_edit_shows_the_model_the_actual_diff(tmp_path):
    (tmp_path / "a.js").write_text("const a = 1;\nconst b = 2;\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute(
        "patch_file",
        {"filepath": "a.js", "old_string": "const b = 2;", "new_string": "const b = 99;"},
    )

    assert "What changed:" in result.message
    assert "-const b = 2;" in result.message
    assert "+const b = 99;" in result.message


def test_the_diff_reaches_the_conversation_not_just_the_console(tmp_path):
    (tmp_path / "a.js").write_text("x = 1\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("patch_file", filepath="a.js", old_string="x = 1", new_string="x = 2"),
         _text("done")],
    )

    asyncio.run(loop.run("change it"))

    tool_messages = [m for m in loop.client.calls[1]["messages"] if m["role"] == "tool"]
    assert any("+x = 2" in m["content"] for m in tool_messages), "the model must see it"


def test_a_new_file_does_not_produce_a_confusing_diff(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("write_file", {"filepath": "new.js", "content": "const a = 1;\n"})

    assert result.ok
    assert "+const a = 1;" in result.message, "creating a file still shows what landed"


def test_a_huge_rewrite_does_not_replay_the_whole_file(tmp_path):
    from shamsu.agents.simple_chat import MAX_DIFF_LINES

    (tmp_path / "big.js").write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    # First attempt is steered to patch_file; the deliberate retry is honoured
    # and is the one that produces a diff.
    loop._execute(
        "write_file",
        {"filepath": "big.js", "content": "unused"},
    )
    result = loop._execute(
        "write_file",
        {"filepath": "big.js", "content": "\n".join(f"other {i}" for i in range(500))},
    )

    body = result.message.split("What changed:", 1)[1]
    assert len(body.splitlines()) <= MAX_DIFF_LINES + 3
    assert "more diff lines" in result.message


# --- the six tools must actually be callable ----------------------------
# Every one of these is a bug that shipped: a schema whose argument name the
# implementation does not read is indistinguishable, from the model's side,
# from a tool that does not work.


def test_every_tool_schema_argument_survives_normalisation(tmp_path):
    """The name the model is SHOWN must reach the implementation.

    `search_files` shipped with schema `pattern` against an implementation
    reading `query`, so 100% of searches failed with "Missing or placeholder
    query" - one of six tools dead, silently, for as long as it existed.
    """
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    (tmp_path / "hello.py").write_text("value = 1\n", encoding="utf-8")

    for schema in SIMPLE_TOOL_SCHEMAS:
        function = schema.get("function", schema)
        name = function["name"]
        required = (function.get("parameters") or {}).get("required", [])
        if name in {"write_file", "patch_file", "run_command"}:
            continue  # mutating/shell: covered by their own tests
        if name == "remember":
            # Not a registry tool: it writes a scratchpad, not a workspace
            # file. Same contract though - the schema name must reach it.
            loop = _loop(tmp_path, [_text("ok")])
            assert loop._execute("remember", {"note": "a fact"}).ok
            continue
        arguments = {key: "hello.py" if "file" in key else "value" for key in required}
        normalized = normalize_arguments(name, arguments)
        result = tools.execute(SIMPLE_TOOLS[name], normalized)
        assert result.ok, f"{name}({arguments}) -> {result.message}"


def test_search_files_reaches_grep_whatever_the_model_calls_the_argument(tmp_path):
    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    (tmp_path / "game.js").write_text("const bulletSpeed = 7;\n", encoding="utf-8")

    for spelling in ("pattern", "query", "text", "search", "regex"):
        normalized = normalize_arguments("search_files", {spelling: "bulletSpeed"})
        assert normalized == {"query": "bulletSpeed"}
        result = tools.execute(SIMPLE_TOOLS["search_files"], normalized)
        assert result.ok, f"{spelling}: {result.message}"


def test_search_files_keeps_its_directory_argument_as_a_path(tmp_path):
    """`path` means a directory here, not the `filepath` the global alias maps it to."""
    assert normalize_arguments("search_files", {"pattern": "x", "path": "backend"}) == {
        "query": "x",
        "path": "backend",
    }
    assert normalize_arguments("list_files", {"path": "backend"}) == {"path": "backend"}
    # ...while a file-shaped tool still gets filepath.
    assert normalize_arguments("read_file", {"path": "a.py"}) == {"filepath": "a.py"}


# --- legacy tool names in a shared transcript ---------------------------


def test_legacy_logical_tool_names_run_the_simple_equivalent(tmp_path):
    """A legacy-routed SHAMSU sharing the workspace poisons the history.

    Live 2026-08-18 `project.inspect`, `file.read`, `code.search` and `test.run`
    were appended to a simple-mode session by a second process, and the model
    then called what it could see itself having called. Refusing costs a round
    each time; the names map cleanly onto the six tools.
    """
    from shamsu.agents.simple_chat import canonical_tool_name

    assert canonical_tool_name("file.read") == "read_file"
    assert canonical_tool_name("code.search") == "search_files"
    assert canonical_tool_name("project.inspect") == "list_files"
    assert canonical_tool_name("test.run") == "run_command"
    assert canonical_tool_name("file.write") == "write_file"
    assert canonical_tool_name("file.patch") == "patch_file"
    # A prefix some models emit, and the plain names, are untouched.
    assert canonical_tool_name("functions.read_file") == "read_file"
    assert canonical_tool_name("read_file") == "read_file"
    # Something genuinely unknown still reports itself, not a wrong guess.
    assert canonical_tool_name("deploy.rocket") == "deploy.rocket"


def test_a_legacy_named_call_actually_reads_the_file(tmp_path):
    (tmp_path / "main.py").write_text("value = 42\n", encoding="utf-8")
    loop = _loop(tmp_path, [_tool("file.read", filepath="main.py"), _text("It sets 42.")])

    result = asyncio.run(loop.run("what does main.py do?"))

    assert result.final == "It sets 42."
    tool_messages = [m for m in loop.state.messages(500) if m["role"] == "tool"]
    assert tool_messages, "the legacy-named call produced no tool result"
    assert "value = 42" in str(tool_messages[-1]["content"])


def test_an_unknown_tool_still_names_what_is_available(tmp_path):
    loop = _loop(tmp_path, [_tool("deploy.rocket", target="mars"), _text("Cannot.")])

    asyncio.run(loop.run("deploy it"))

    tool_messages = [m for m in loop.state.messages(500) if m["role"] == "tool"]
    assert "no tool called deploy.rocket" in str(tool_messages[-1]["content"])


# --- reads that spin -----------------------------------------------------


def test_repeating_one_read_is_pointed_out_rather_than_left_to_loop(tmp_path):
    """Reads change nothing, so the no-change counter cannot see them spin.

    Live 2026-08-18: `list_files {path: "."}` three times in a row, each
    returning the listing the model already had.
    """
    turns = [_tool("list_files", path=".") for _ in range(3)] + [_text("Done.")]
    loop = _loop(tmp_path, turns)

    result = asyncio.run(loop.run("what is here?"))

    nudges = [
        str(m["content"])
        for m in loop.state.messages(500)
        if m["role"] == "user" and "already called" in str(m["content"])
    ]
    assert nudges, "a verbatim-repeated read was never pointed out"
    assert "list_files" in nudges[0]
    assert result.final == "Done."


def test_different_reads_are_never_treated_as_repetition(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b = 2\n", encoding="utf-8")
    turns = [
        _tool("read_file", filepath="a.py"),
        _tool("read_file", filepath="b.py"),
        _tool("list_files", path="."),
        _text("Two files."),
    ]
    loop = _loop(tmp_path, turns)

    asyncio.run(loop.run("look around"))

    assert not [
        m
        for m in loop.state.messages(500)
        if m["role"] == "user" and "already called" in str(m["content"])
    ]


# --- a long call must not look like a hang -------------------------------


def test_a_slow_model_call_reports_that_it_is_still_running(tmp_path):
    """Ten minutes of silence is how "it is stuck" gets reported."""
    import shamsu.agents.simple_chat as simple_chat

    seen: list[str] = []
    original = simple_chat.HEARTBEAT_SECONDS
    simple_chat.HEARTBEAT_SECONDS = 0.01
    try:

        class SlowClient(FakeClient):
            async def chat(self, **kwargs):
                await asyncio.sleep(0.08)
                return await super().chat(**kwargs)

        tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
        state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
        loop = SimpleChatLoop(
            tmp_path,
            client=SlowClient([_text("Finally.")]),
            tools=tools,
            state=state,
            model_name="qwen3:8b",
            on_status=seen.append,
        )
        result = asyncio.run(loop.run("hello?"))
    finally:
        simple_chat.HEARTBEAT_SECONDS = original

    assert result.final == "Finally."
    assert seen, "a slow call reported nothing while it ran"
    assert all("thinking" in message for message in seen)


def test_the_heartbeat_stops_when_the_call_returns(tmp_path):
    """A ticker left running would keep claiming work after the turn ended."""
    import shamsu.agents.simple_chat as simple_chat

    seen: list[str] = []
    original = simple_chat.HEARTBEAT_SECONDS
    simple_chat.HEARTBEAT_SECONDS = 0.01
    try:
        loop = _loop(tmp_path, [_text("Quick.")], on_status=seen.append)
        asyncio.run(loop.run("hi"))
        settled = len(seen)

        async def _wait():
            await asyncio.sleep(0.1)

        asyncio.run(_wait())
    finally:
        simple_chat.HEARTBEAT_SECONDS = original

    assert len(seen) == settled


# --- history written by another agent ------------------------------------
# A workspace transcript is shared by every SHAMSU that opens it, and the
# legacy router speaks a different tool vocabulary. A model imitates its own
# history, so foreign calls in the transcript are not untidiness - they are
# instructions to call tools this loop cannot execute.


def _legacy_records():
    return [
        {"role": "user", "content": "run the game"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "function": {"name": "project.inspect", "arguments": {}}}
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "project.inspect", "content": "{}"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c2", "function": {"name": "read_file", "arguments": {"filepath": "a.py"}}}
            ],
        },
        {"role": "tool", "tool_call_id": "c2", "name": "read_file", "content": "a = 1"},
        {"role": "tool", "tool_call_id": "", "name": "verify", "content": "a.py: ok"},
        {"role": "assistant", "content": "Done."},
    ]


def _tool_names(state):
    names = set()
    for message in state._messages:
        for call in getattr(message, "tool_calls", []) or []:
            function = call.get("function")
            source = function if isinstance(function, dict) else call
            names.add(str(source.get("name") or ""))
        if getattr(message, "name", ""):
            names.add(message.name)
    return {name for name in names if name}


def test_a_foreign_agents_tool_calls_never_reach_the_model():
    from shamsu.agents.simple_chat import SIMPLE_TRANSCRIPT_TOOLS

    state = ChatState("sys", hydrate=False, known_tools=SIMPLE_TRANSCRIPT_TOOLS)
    state._hydrate_records(_legacy_records(), "content")

    names = _tool_names(state)
    assert "project.inspect" not in names
    assert "read_file" in names, "the legitimate history was thrown away too"


def test_the_result_of_a_dropped_call_is_dropped_with_it():
    """An orphaned tool result is a reply to a question the model never asked."""
    from shamsu.agents.simple_chat import SIMPLE_TRANSCRIPT_TOOLS

    state = ChatState("sys", hydrate=False, known_tools=SIMPLE_TRANSCRIPT_TOOLS)
    state._hydrate_records(_legacy_records(), "content")

    assert not [
        m
        for m in state._messages
        if getattr(m, "role", "") == "tool"
        and getattr(m, "name", "") not in SIMPLE_TRANSCRIPT_TOOLS
    ]


def test_verification_results_survive_the_filter():
    """`verify` is written by the loop itself, not called by the model.

    Filtering on the six CALLABLE tools alone would silently drop "your file
    failed to compile" from the history the next turn reads.
    """
    from shamsu.agents.simple_chat import SIMPLE_TOOLS, SIMPLE_TRANSCRIPT_TOOLS

    assert "verify" not in SIMPLE_TOOLS
    assert "verify" in SIMPLE_TRANSCRIPT_TOOLS

    state = ChatState("sys", hydrate=False, known_tools=SIMPLE_TRANSCRIPT_TOOLS)
    state._hydrate_records(_legacy_records(), "content")

    assert "verify" in _tool_names(state)


def test_an_assistant_turn_that_said_something_keeps_its_words():
    """Only the foreign CALL is dropped - prose the user may refer back to stays."""
    from shamsu.agents.simple_chat import SIMPLE_TRANSCRIPT_TOOLS

    records = [
        {
            "role": "assistant",
            "content": "Let me inspect the project.",
            "tool_calls": [{"id": "c1", "function": {"name": "project.inspect"}}],
        },
    ]
    state = ChatState("sys", hydrate=False, known_tools=SIMPLE_TRANSCRIPT_TOOLS)
    state._hydrate_records(records, "content")

    kept = [m for m in state._messages if getattr(m, "role", "") == "assistant"]
    assert len(kept) == 1
    assert "inspect the project" in kept[0].content
    assert kept[0].tool_calls == []


def test_without_a_declared_vocabulary_nothing_is_filtered():
    """Legacy callers share this class and must be completely unaffected."""
    state = ChatState("sys", hydrate=False)
    state._hydrate_records(_legacy_records(), "content")

    assert "project.inspect" in _tool_names(state)


# --- the harness's own words are not the model's -------------------------


def test_the_harnesss_stop_messages_are_not_replayed_as_the_models_answers():
    """Replaying them teaches the model that stopping is how a turn ends.

    Live 2026-08-18 a session carried "The model did not respond within 600s."
    forward into every later turn as an assistant message.
    """
    from shamsu.agents.chat_state import _should_hydrate_chat_message

    for stop in (
        "The model did not respond within 600s.",
        "The model returned an empty reply 3 times.",
        "I stopped after 24 steps without finishing. Say `continue` to keep going.",
        "I tried 4 edits in a row that changed nothing - either the snippet is missing.",
        "I have now changed frontend/game.js 5 times in this turn without confirming.",
    ):
        assert not _should_hydrate_chat_message("assistant", stop), stop


def test_ordinary_prose_that_merely_starts_the_same_way_is_kept():
    """"I tried..." and "I stopped..." are perfectly normal things to say."""
    from shamsu.agents.chat_state import _should_hydrate_chat_message

    for kept in (
        "I tried a different approach and it worked.",
        "I stopped the server because the port was busy.",
        "I have now changed the approach entirely.",
        "Done! Changed bulletSpeed from 7 to 9 in frontend/game.js.",
    ):
        assert _should_hydrate_chat_message("assistant", kept), kept


def test_a_stopped_turn_does_not_teach_the_next_one_to_stop(tmp_path):
    """End to end: the stop is persisted for the user, but not replayed."""
    from shamsu.agents.chat_state import ChatState

    records = [
        {"role": "user", "content": "review the prd"},
        {"role": "assistant", "content": "The model did not respond within 600s."},
        {"role": "user", "content": "try again"},
    ]
    state = ChatState("sys", hydrate=False)
    state._hydrate_records(records, "content")

    replayed = [m.content for m in state._messages if getattr(m, "role", "") == "assistant"]
    assert not replayed, f"a harness stop was replayed: {replayed}"
    # ...while the user's own turns are untouched.
    asked = [m.content for m in state._messages if getattr(m, "role", "") == "user"]
    assert asked == ["review the prd", "try again"]


# --- pending actions must not reopen the legacy orchestrator -------------
# Slash commands and pending-action replies are dispatched inside main()
# BEFORE _handle_request, so the simple-mode guard there never saw them. A bare
# "yes" was enough to drop the whole conversation into the old loop, which
# speaks a different tool vocabulary and writes it into the shared transcript.


def _route_probe(monkeypatch):
    """Record which loop a dispatch reaches, without running either."""
    import shamsu.cli.repl as repl

    taken: list[tuple[str, str]] = []

    async def fake_simple(user_input, workspace, console, session_logger=None, **kwargs):
        taken.append(("simple", str(user_input)))

    async def fake_legacy(*args, **kwargs):
        taken.append(("legacy", str(args[0]) if args else ""))
        return None

    monkeypatch.setattr(repl, "_run_simple_chat", fake_simple)
    monkeypatch.setattr(repl, "_run_agent_chat", fake_legacy)
    return repl, taken


def test_proceeding_with_a_plan_stays_in_simple_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    repl, taken = _route_probe(monkeypatch)

    pending = {
        "awaiting": "plan_approval",
        "created_from_prompt": "add a scoreboard",
        "steps": ["create score.js", "wire it into index.html"],
    }
    asyncio.run(repl._execute_pending_plan(pending, tmp_path, repl.Console(), None))

    assert [where for where, _ in taken] == ["simple"]
    instruction = taken[0][1]
    assert "add a scoreboard" in instruction
    assert "create score.js" in instruction, "the agreed steps were dropped"


def test_a_pending_prd_plan_stays_in_simple_mode(tmp_path, monkeypatch):
    """This is the exact route that poisoned a live session."""
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    repl, taken = _route_probe(monkeypatch)

    pending = {"created_from_prompt": "build the product from SPEC.md", "prd_path": "SPEC.md"}
    asyncio.run(
        repl._execute_pending_prd_plan(pending, "yes", tmp_path, repl.Console(), None)
    )

    assert [where for where, _ in taken] == ["simple"]
    assert "SPEC.md" in taken[0][1]


def test_resuming_a_paused_plan_stays_in_simple_mode(tmp_path, monkeypatch):
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    repl, taken = _route_probe(monkeypatch)

    paused = {
        "steps": ["step one", "step two", "step three"],
        "resume_index": 1,
        "created_from_prompt": "build the menu",
    }
    asyncio.run(
        repl._resume_paused_plan(paused, "use port 8080", tmp_path, repl.Console(), None)
    )

    assert [where for where, _ in taken] == ["simple"]
    instruction = taken[0][1]
    assert "use port 8080" in instruction, "the answer that unblocked it was lost"
    assert "step two" in instruction
    assert "step one" not in instruction, "already-done steps were replayed"


def test_legacy_routing_still_reaches_the_old_loop(tmp_path, monkeypatch):
    """The escape hatch must keep working, or the pinned suite is testing nothing."""
    monkeypatch.setenv("SHAMSU_LEGACY_ROUTING", "1")
    repl, taken = _route_probe(monkeypatch)

    pending = {"created_from_prompt": "build it", "prd_path": "SPEC.md"}
    try:
        asyncio.run(
            repl._execute_pending_prd_plan(pending, "yes", tmp_path, repl.Console(), None)
        )
    except Exception:
        pass  # the legacy path needs far more scaffolding; reaching it is the point

    assert "simple" not in [where for where, _ in taken]


def test_no_slash_command_can_reach_the_legacy_loop_unguarded():
    """A structural check, so a new handler cannot quietly reopen the hole.

    Walks repl.py's call graph from every handler main() dispatches and fails if
    one reaches `_run_agent_chat` without a `_legacy_routing_enabled` guard on
    the way.
    """
    import ast
    import collections
    from pathlib import Path as _Path

    import shamsu.cli.repl as repl

    source = _Path(repl.__file__).read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    def calls(node):
        found = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                target = child.func
                name = (
                    target.id
                    if isinstance(target, ast.Name)
                    else target.attr if isinstance(target, ast.Attribute) else None
                )
                if name in functions:
                    found.add(name)
        return found

    guarded = {
        name
        for name, node in functions.items()
        if "_legacy_routing_enabled" in calls(node)
    }
    edges = {name: calls(node) for name, node in functions.items()}

    unguarded = []
    for handler in sorted(calls(functions["main"])):
        if handler in guarded:
            continue
        queue = collections.deque([(handler, [handler])])
        seen = {handler}
        while queue:
            current, path = queue.popleft()
            for target in sorted(edges.get(current, ())):
                if target == "_run_agent_chat":
                    unguarded.append(" -> ".join(path + [target]))
                    queue.clear()
                    break
                if target not in seen and target not in guarded:
                    seen.add(target)
                    queue.append((target, path + [target]))

    assert not unguarded, "unguarded routes into the legacy loop:\n" + "\n".join(unguarded)


# --- remote control must speak the same vocabulary -----------------------
# Telegram resumes the LATEST session in the workspace - normally the one the
# desktop REPL is sitting in - and it wrote no chat log of its own, so a legacy
# run there was invisible from the desktop side while poisoning its transcript.


def test_a_telegram_message_runs_the_same_loop_as_the_desktop(tmp_path, monkeypatch):
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    import shamsu.integrations.telegram.sessions as telegram_sessions

    used: list[str] = []

    class _Loop:
        def __init__(self, *args, **kwargs):
            used.append("simple")

        async def run(self, text):
            from shamsu.agents.simple_chat import SimpleChatResult

            return SimpleChatResult(final="done via simple mode")

    def _legacy(*args, **kwargs):
        used.append("legacy")
        raise AssertionError("Telegram reached the legacy loop in simple mode")

    monkeypatch.setattr("shamsu.agents.simple_chat.SimpleChatLoop", _Loop)
    monkeypatch.setattr("shamsu.agents.chat_loop._default_ollama_client", lambda *a, **k: object())
    monkeypatch.setattr(telegram_sessions, "AgentChatLoop", _legacy)

    class _Progress:
        def step(self, *args, **kwargs):
            pass

    service = telegram_sessions.LocalShamsuSessionGateway.__new__(
        telegram_sessions.LocalShamsuSessionGateway
    )
    service.workspace = tmp_path
    final = service._run_simple("hello", None, None, None, _Progress())

    assert final == "done via simple mode"
    assert used == ["simple"]


def test_only_guarded_places_build_the_legacy_loop():
    """A structural check: nothing may construct AgentChatLoop unguarded.

    Two entry points existed - the REPL's own `_run_agent_chat`, and Telegram,
    which had no guard at all. Any third one is the same bug again.
    """
    import ast
    from pathlib import Path as _Path

    import shamsu

    root = _Path(shamsu.__file__).parent
    offenders = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts or "legacy-code" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = getattr(target, "id", None) or getattr(target, "attr", None)
            if name != "AgentChatLoop":
                continue
            # The enclosing function must consult simple mode somewhere.
            enclosing = None
            for candidate in ast.walk(tree):
                if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if candidate.lineno <= node.lineno <= (candidate.end_lineno or 0):
                        if enclosing is None or candidate.lineno > enclosing.lineno:
                            enclosing = candidate
            if enclosing is not None and enclosing.name == "_run_agent_chat":
                # THE legacy runner. It is reached only through guarded callers,
                # which `test_no_slash_command_can_reach_the_legacy_loop_unguarded`
                # verifies by walking the call graph.
                continue
            body = ast.dump(enclosing) if enclosing is not None else ""
            if "simple_mode_enabled" not in body and "_legacy_routing_enabled" not in body:
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "AgentChatLoop is built without checking simple mode at: " + ", ".join(offenders)
    )


# --- approval must not fight the spinner ---------------------------------
# Reported live 2026-08-18: the prompt rendered, then the status kept
# repainting over it - "Working> y" - and the turn sometimes hung. The prompt
# was building its OWN Console, so it had nothing to pause.


class _FakeStatus:
    """Stands in for a Rich status: records start/stop like the real one."""

    def __init__(self):
        self.is_started = True
        self.events: list[str] = []

    def stop(self):
        self.is_started = False
        self.events.append("stop")

    def start(self):
        self.is_started = True
        self.events.append("start")


def test_the_live_spinner_is_stopped_before_approval_reads_input(monkeypatch):
    from rich.console import Console as _Console

    from shamsu.safety import approval as approval_module
    from shamsu.types import ApprovalRequest

    console = _Console(file=io.StringIO(), force_terminal=False)
    status = _FakeStatus()
    setattr(console, "_shamsu_active_statuses", [status])

    running_when_read: list[bool] = []

    def fake_input(_prompt=""):
        running_when_read.append(status.is_started)
        return "y"

    monkeypatch.setattr("builtins.input", fake_input)

    request = ApprovalRequest(
        action_type="run_command",
        risk_level="medium",
        description="python -m http.server 8000",
    )
    assert approval_module.ask_approval(request, console=console) is True

    assert running_when_read == [False], "the spinner was still painting while input was read"
    assert status.events[0] == "stop"


def test_the_spinner_comes_back_after_the_answer(monkeypatch):
    """Otherwise the rest of the turn runs with no sign of life."""
    from rich.console import Console as _Console

    from shamsu.safety import approval as approval_module
    from shamsu.types import ApprovalRequest

    console = _Console(file=io.StringIO(), force_terminal=False)
    status = _FakeStatus()
    setattr(console, "_shamsu_active_statuses", [status])
    monkeypatch.setattr("builtins.input", lambda _p="": "n")

    approval_module.ask_approval(
        ApprovalRequest(action_type="run_command", risk_level="medium", description="rm -rf /"),
        console=console,
    )

    assert status.is_started, "the spinner was never restarted"
    assert status.events == ["stop", "start"]


def test_simple_mode_hands_the_real_console_to_the_prompt(tmp_path, monkeypatch):
    """The whole bug in one line: called bare, the prompt makes its own Console."""
    import shamsu.cli.repl as repl

    seen: dict = {}

    def fake_ask(request, console=None):
        seen["console"] = console
        return True

    monkeypatch.setattr(repl, "ask_approval", fake_ask)

    captured: dict = {}

    def fake_build(workspace, *, console_approval, **kwargs):
        captured["approve"] = console_approval
        raise RuntimeError("stop here - the wiring is what matters")

    monkeypatch.setattr("shamsu.agents.simple_chat.build_simple_tools", fake_build)

    console = repl.Console(file=io.StringIO())
    with contextlib.suppress(RuntimeError):
        asyncio.run(repl._run_simple_chat("hi", tmp_path, console, None))

    assert "approve" in captured, "build_simple_tools was never reached"
    captured["approve"](object())
    assert seen.get("console") is console, "the prompt was given a different console"


def test_a_slow_tool_call_reports_that_it_is_still_running(tmp_path):
    """After an approval, a blocking command was 120s of total silence."""
    import shamsu.agents.simple_chat as simple_chat

    seen: list[str] = []
    original = simple_chat.HEARTBEAT_SECONDS
    simple_chat.HEARTBEAT_SECONDS = 0.01

    class SlowTools(AgentToolRegistry):
        def execute(self, name, arguments):
            time.sleep(0.08)
            return super().execute(name, arguments)

    try:
        (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
        tools = SlowTools(tmp_path, approval_func=lambda _r: True)
        state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
        loop = SimpleChatLoop(
            tmp_path,
            client=FakeClient([_tool("read_file", filepath="a.py"), _text("Read it.")]),
            tools=tools,
            state=state,
            model_name="qwen3:8b",
            on_status=seen.append,
        )
        result = asyncio.run(loop.run("read a.py"))
    finally:
        simple_chat.HEARTBEAT_SECONDS = original

    assert result.final == "Read it."
    assert any("running read_file" in message for message in seen), seen


# --- a slow human must not be punished -----------------------------------
# Reported live: "if i wait for a bit it gets stuck". The tool heartbeat ticks
# every 5s and the approval prompt lives INSIDE tool execution, so answering
# within 5s worked and thinking about it did not.


def test_nothing_paints_while_an_approval_prompt_is_waiting(tmp_path):
    """The heartbeat must hold off, however long the human takes."""
    import shamsu.agents.simple_chat as simple_chat
    from shamsu.safety.approval import prompt_is_active, reading_input

    painted_while_waiting: list[str] = []
    original = simple_chat.HEARTBEAT_SECONDS
    simple_chat.HEARTBEAT_SECONDS = 0.01

    class PromptingTools(AgentToolRegistry):
        def execute(self, name, arguments):
            # Stand in for a human staring at the prompt for a while.
            with reading_input():
                time.sleep(0.15)
            return super().execute(name, arguments)

    def record(message: str) -> None:
        if prompt_is_active():
            painted_while_waiting.append(message)

    try:
        (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
        tools = PromptingTools(tmp_path, approval_func=lambda _r: True)
        state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
        loop = SimpleChatLoop(
            tmp_path,
            client=FakeClient([_tool("read_file", filepath="a.py"), _text("Done.")]),
            tools=tools,
            state=state,
            model_name="qwen3:8b",
            on_status=record,
        )
        result = asyncio.run(loop.run("read it"))
    finally:
        simple_chat.HEARTBEAT_SECONDS = original

    assert result.final == "Done."
    assert not painted_while_waiting, (
        "the spinner painted over a waiting prompt: " + repr(painted_while_waiting)
    )


def test_the_flag_clears_even_when_the_prompt_raises():
    """A stuck flag would silence the spinner for the rest of the session."""
    from shamsu.safety.approval import prompt_is_active, reading_input

    assert not prompt_is_active()
    with contextlib.suppress(RuntimeError):
        with reading_input():
            assert prompt_is_active()
            raise RuntimeError("user hit ctrl-c")
    assert not prompt_is_active(), "the prompt flag was left set"


def test_nested_prompts_do_not_unmask_early():
    from shamsu.safety.approval import prompt_is_active, reading_input

    with reading_input():
        with reading_input():
            assert prompt_is_active()
        assert prompt_is_active(), "an inner prompt cleared the outer one"
    assert not prompt_is_active()


def test_the_repl_status_updater_holds_off_during_a_prompt():
    """The REPL's own painter, not just the loop's."""
    import shamsu.cli.repl as repl
    from shamsu.safety.approval import reading_input

    class _Status:
        def __init__(self):
            self.painted = []

        def update(self, message):
            self.painted.append(message)

    status = _Status()
    update = repl._status_updater(status)

    update("thinking... 5s")
    assert len(status.painted) == 1, "normal updates must still paint"

    with reading_input():
        update("thinking... 10s")
    assert len(status.painted) == 1, "it painted over a waiting prompt"

    update("thinking... 15s")
    assert len(status.painted) == 2, "painting never resumed after the prompt"


# --- the prompt must run where the console lives -------------------------
# Tools execute via `asyncio.to_thread`, so the approval prompt was reading the
# console from a WORKER thread. On Windows the whole input stack -
# prompt_toolkit's console session, msvcrt, Rich's Live - belongs to the main
# thread. That is the run_in_executor+stdin trap, and it is why a turn could
# sit at "Approval Required" forever.


def test_the_approval_prompt_runs_on_the_main_thread(tmp_path):
    import threading

    from shamsu.agents.simple_chat import build_simple_tools

    seen: dict = {}

    def spy(_request):
        seen["is_main"] = threading.current_thread() is threading.main_thread()
        return False

    async def drive():
        tools = build_simple_tools(
            tmp_path,
            console_approval=spy,
            main_loop=asyncio.get_running_loop(),
        )
        # Exactly what SimpleChatLoop._run_tools does.
        await asyncio.to_thread(
            tools.execute, "run_command", {"command": 'python -c "pass"'}
        )

    asyncio.run(drive())

    assert seen.get("is_main") is True, "the prompt read the console off the main thread"


def test_without_a_loop_it_still_asks_rather_than_assuming(tmp_path):
    """No loop to marshal onto must never become an unasked action."""
    import threading

    from shamsu.agents.simple_chat import build_simple_tools

    asked: list[bool] = []

    def spy(_request):
        asked.append(threading.current_thread() is threading.main_thread())
        return False

    async def drive():
        tools = build_simple_tools(tmp_path, console_approval=spy)  # no main_loop
        await asyncio.to_thread(
            tools.execute, "run_command", {"command": 'python -c "pass"'}
        )

    asyncio.run(drive())
    assert asked, "the command ran without anyone being asked"


def test_a_slow_answer_does_not_hang_the_turn(tmp_path):
    """The reported symptom: answer fast and it works, wait and it sticks."""
    from shamsu.agents.simple_chat import build_simple_tools
    from shamsu.safety.approval import reading_input

    def slow_human(_request):
        with reading_input():
            time.sleep(0.4)
        return True

    async def drive():
        tools = build_simple_tools(
            tmp_path,
            console_approval=slow_human,
            main_loop=asyncio.get_running_loop(),
        )
        state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
        loop = SimpleChatLoop(
            tmp_path,
            client=FakeClient(
                [_tool("run_command", command='python -c "print(1)"'), _text("Ran it.")]
            ),
            tools=tools,
            state=state,
            model_name="qwen3:8b",
        )
        return await asyncio.wait_for(loop.run("run it"), timeout=60)

    started = time.perf_counter()
    result = asyncio.run(drive())
    elapsed = time.perf_counter() - started

    assert result.final == "Ran it."
    assert elapsed < 45, f"the turn took {elapsed:.1f}s"


def test_an_unrecognised_key_is_answered_not_swallowed(monkeypatch):
    """Silence on a keypress is indistinguishable from a hang."""
    import io as _io
    import sys as _sys

    from rich.console import Console as _Console

    from shamsu.safety import approval as approval_module

    if _sys.platform != "win32":
        pytest.skip("msvcrt reader is Windows-only")

    keys = iter(["q", "\r", "y"])
    monkeypatch.setattr(approval_module.sys, "platform", "win32")
    fake_msvcrt = type("M", (), {"getwch": staticmethod(lambda: next(keys))})
    monkeypatch.setitem(__import__("sys").modules, "msvcrt", fake_msvcrt)

    buffer = _io.StringIO()
    console = _Console(file=buffer, force_terminal=True)
    answer = approval_module._read_windows_console_answer(console)

    assert answer == "y"
    printed = buffer.getvalue()
    assert "not an option" in printed or "Press y" in printed, printed


def test_the_changing_block_never_sits_in_front_of_the_conversation(tmp_path):
    """llama.cpp caches the longest common PREFIX of the token sequence.

    The workspace listing changes whenever a file does. At position 1 - which is
    where it used to be - one edit invalidated everything after ~150 tokens and
    forced a full re-prefill of the entire conversation. Measured on a live
    session: 46 sends, 6 distinct versions, against a 23,000-token prompt.

    So the rule is positional: the volatile block goes at the END of the
    conversation, never in front of it.
    """
    (tmp_path / "a.py").write_text("a = 1\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_text("one"), _tool("write_file", filepath="b.py", content="b = 2\n"), _text("two")],
    )

    asyncio.run(loop.run("first question"))
    asyncio.run(loop.run("now create b.py"))

    # It really does change between calls - otherwise this proves nothing.
    assert "b.py" not in _grounding_of(loop.client.calls[0])
    assert "b.py" in _grounding_of(loop.client.calls[-1])

    for call in loop.client.calls:
        messages = call["messages"]
        index = next(
            i for i, m in enumerate(messages)
            if str(m.get("content", "")) == _grounding_of(call)
        )
        assert index >= len(messages) - 2, (
            f"the volatile block is at {index} of {len(messages)} - it belongs at "
            "the end, or every earlier turn gets re-prefilled"
        )


def test_the_users_request_is_still_the_last_thing_the_model_reads(tmp_path):
    """Grounding must not be appended AFTER the question it is grounding."""
    loop = _loop(tmp_path, [_text("ok")])

    asyncio.run(loop.run("change the bullet speed"))

    sent = loop.client.calls[0]["messages"]
    assert sent[-1] == {"role": "user", "content": "change the bullet speed"}
    assert _grounding_of(loop.client.calls[0]), "the grounding block went missing"


# ---------------------------------------------------------------------------
# Output room and honest truncation (SMALLCODE plan item B)
#
# A live session produced 19 generations that Ollama cut off at the window, and
# the harness reported none of them. One was displayed as a finished answer
# while ending mid-word, and then became permanent conversation.
# ---------------------------------------------------------------------------


def _cut(content: str = "", thinking: str = "") -> dict:
    """A turn Ollama stopped because the window filled."""
    return {
        "message": {"content": content, "thinking": thinking, "tool_calls": []},
        "done_reason": "length",
        "prompt_eval_count": 31_400,
        "eval_count": 95,
    }


def test_generation_is_capped_at_the_reserve_the_budget_held_back(tmp_path):
    """Without num_predict, generation was bounded only by leftover window."""
    from shamsu.agents.simple_chat import output_reserve

    loop = _loop(tmp_path, [_text("done")])
    asyncio.run(loop.run("hi"))

    options = loop.client.calls[0]["options"]
    assert options["num_predict"] == output_reserve(options["num_ctx"])


def test_a_cut_off_answer_is_never_presented_as_a_finished_one(tmp_path):
    """`done_reason == "length"` means the model was still speaking."""
    loop = _loop(tmp_path, [_cut(content="The fix is to set window.asteroid")])

    result = asyncio.run(loop.run("why is it broken?"))

    assert "cut off" in result.final.lower()
    assert "The fix is to set window.asteroid" in result.final  # partial kept
    assert "/new" in result.final                               # and a way out


def test_a_truncated_thought_never_becomes_the_answer_or_the_history(tmp_path):
    """The exact live failure: a thought ending mid-word, shown as an answer."""
    loop = _loop(tmp_path, [_cut(thinking="means `window.asteroid")] * 6)

    result = asyncio.run(loop.run("why is it broken?"))

    assert "means `window.asteroid" not in result.final
    assert "ran out of room" in result.final.lower()
    hydrated = [m.content for m in loop.state.all_messages if m.role == "assistant"]
    assert not any("means `window.asteroid" in c for c in hydrated)


def test_a_COMPLETE_thought_with_no_content_is_still_used_as_the_answer(tmp_path):
    """Reasoning models end turns this way; claiming it ran out of room lies."""
    finished = {
        "message": {"content": "", "thinking": "Set speed = 2.", "tool_calls": []},
        "done_reason": "stop",
    }
    loop = _loop(tmp_path, [finished] * 6)

    result = asyncio.run(loop.run("how do I slow the ship?"))

    assert result.final == "Set speed = 2."
    assert not result.stopped
    assert "ran out of room" not in result.final.lower()


def test_the_compaction_pass_does_not_spend_its_budget_thinking(tmp_path):
    """A mechanical summary needs no reasoning trace."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(40):
        state.append_user(f"turn {i} " + "padding " * 300)
        state.append_assistant(f"reply {i} " + "padding " * 300)
    loop = _loop(tmp_path, [_text("done")])
    loop.state = state

    asyncio.run(loop.run("carry on"))

    narrations = [c for c in loop.client.calls if c.get("think") is False]
    assert narrations, "the narration pass should disable thinking"


def test_ground_truth_prompt_size_is_read_off_the_response(tmp_path):
    """prompt_eval_count is the only number here that is not a guess."""
    loop = _loop(tmp_path, [{"message": {"content": "hi", "tool_calls": []},
                             "prompt_eval_count": 4242, "eval_count": 7}])

    asyncio.run(loop.run("hello"))

    assert loop.last_prompt_tokens == 4242
    assert loop.last_completion_tokens == 7
    assert loop.last_estimate > 0


def test_an_old_client_that_rejects_a_keyword_still_gets_its_turn(tmp_path):
    """Shedding `think` must not also cost the model its tools."""
    class PickyClient:
        def __init__(self):
            self.calls = []

        async def chat(self, **kwargs):
            if "think" in kwargs:
                raise TypeError("unexpected keyword argument 'think'")
            self.calls.append(kwargs)
            return {"message": {"content": "ok", "tool_calls": []}}

    loop = _loop(tmp_path, [])
    loop.client = PickyClient()

    result = asyncio.run(loop.run("hi"))

    assert result.final == "ok"
    assert "tools" in loop.client.calls[0], "tools were shed unnecessarily"


# ---------------------------------------------------------------------------
# Patch-first editing (SMALLCODE plan item C)
#
# Small models are unreliable at reproducing whole files - they truncate,
# hallucinate imports and drift in indentation - and each attempt costs ~100s.
# Live 2026-08-18, whole-file rewrites drove one turn to 18 minutes/18 rounds.
# ---------------------------------------------------------------------------


def _big_file(tmp_path: Path, name: str = "game.js", lines: int = 200) -> Path:
    path = tmp_path / name
    path.write_text("\n".join(f"const value{i} = {i};" for i in range(lines)), encoding="utf-8")
    return path


def test_rewriting_a_substantial_existing_file_is_steered_to_patch(tmp_path):
    _big_file(tmp_path)
    loop = _loop(tmp_path, [_tool("write_file", filepath="game.js", content="// oops")])

    asyncio.run(loop.run("clean up game.js"))

    assert _big_file  # keep the helper referenced for readers
    assert (tmp_path / "game.js").read_text(encoding="utf-8") != "// oops"
    told = [m.content for m in loop.state.all_messages if m.role == "tool"]
    assert any("patch_file" in c for c in told), "the error must name the next call"


def test_a_deliberate_second_attempt_at_a_full_rewrite_is_honoured(tmp_path):
    """Every guard needs an exit. A full rewrite is sometimes exactly right."""
    _big_file(tmp_path)
    rewrite = _tool("write_file", filepath="game.js", content="// intended full rewrite")
    loop = _loop(tmp_path, [rewrite, rewrite, _text("rewritten")])

    asyncio.run(loop.run("replace game.js entirely"))

    assert (tmp_path / "game.js").read_text(encoding="utf-8") == "// intended full rewrite"


def test_a_small_existing_file_is_still_rewritten_whole(tmp_path):
    """The limit must not turn every tiny config edit into a patch round."""
    (tmp_path / "config.json").write_text('{"port": 8080}', encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="config.json", content='{"port": 9090}'), _text("done")],
    )

    asyncio.run(loop.run("change the port"))

    assert (tmp_path / "config.json").read_text(encoding="utf-8") == '{"port": 9090}'


def test_a_brand_new_file_is_never_steered_to_patch(tmp_path):
    """There is nothing to patch against."""
    body = "\n".join(f"line {i}" for i in range(300))
    loop = _loop(tmp_path, [_tool("write_file", filepath="new.py", content=body), _text("done")])

    asyncio.run(loop.run("create new.py"))

    assert (tmp_path / "new.py").read_text(encoding="utf-8") == body


# ---------------------------------------------------------------------------
# Eliding tool payloads (SMALLCODE plan item D)
#
# Code enters the conversation twice - as the write_file content inside
# tool_calls, and as the body of a read_file result - and both have served
# their purpose the moment the call returns. Measured on a 130-message
# session: 44,833 tokens verbatim vs 10,476 elided, ~13 turns vs ~57.
# ---------------------------------------------------------------------------


def _write_call_body(path: str, body: str) -> list[dict]:
    return [{"function": {"name": "write_file", "arguments": {"filepath": path, "content": body}}}]


def _session(tmp_path):
    from shamsu.session.manager import SessionManager

    return SessionManager(tmp_path).create_session("elide")


def test_an_old_write_payload_is_dropped_but_the_call_is_still_legible(tmp_path):
    """Keys are kept, long values are not - so the call still reads as
    `write_file(filepath=game.js)` rather than as a hole in the history."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    state.append_user("build it")
    state.append_assistant("", tool_calls=_write_call_body("game.js", "x=1;" + chr(10)))
    # Enough weight after it to push the session over the elision target.
    for i in range(60):
        state.append_user(f"later {i} " + "padding " * 400)
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    loop._elide_payloads()

    payload = state.all_messages[2]
    arguments = payload.tool_calls[0]["function"]["arguments"]
    assert arguments["filepath"] == "game.js", "the model must still see WHICH file"
    assert payload.elided


def test_nothing_is_elided_while_there_is_room(tmp_path):
    """Eliding what does not need to go throws away detail for nothing.

    smallcode evicts down TO a target and stops; so does this. A short session
    keeps every byte of every payload.
    """
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    body = "x=1;" + chr(10) * 2
    for i in range(30):
        state.append_user(f"step {i}")
        state.append_assistant("", tool_calls=_write_call_body(f"f{i}.js", body))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    changed = loop._elide_payloads()

    assert changed == 0, "elided under budget, losing detail for no reason"


def test_elision_survives_the_turn_boundary(tmp_path):
    """A fresh ChatState is built per user message and rehydrates from disk.

    Eliding only in memory for the current turn saves nothing across turns -
    which is exactly the case the 44,833 -> 10,476 measurement was taken on.
    """
    from shamsu.context.budget import messages_tokens

    logger = _session(tmp_path)
    state = ChatState(simple_system_prompt(tmp_path), session_logger=logger, hydrate=False)
    body = "const x = 1;\n" * 400
    for i in range(40):   # ~120 messages, the scale the reclaim was measured at
        state.append_user(f"step {i}")
        state.append_assistant("", tool_calls=_write_call_body(f"f{i}.js", body))
        state.append_tool("", "write_file", json.dumps({"ok": True, "message": "wrote it",
                                                        "data": {"content": body}}))

    # A NEW loop for the next user message, hydrating everything back off disk.
    fresh = SimpleChatLoop(
        tmp_path,
        client=FakeClient([_text("ok")]),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        session_logger=logger,
        model_name="qwen3:8b",
    )
    before = messages_tokens(m.to_ollama() for m in fresh.state.all_messages)
    fresh._elide_payloads()
    after = messages_tokens(m.to_ollama() for m in fresh.state.all_messages)

    assert after < before * 0.5, f"expected a large reclaim, got {before} -> {after}"


def test_the_most_recent_payloads_are_kept_verbatim(tmp_path):
    """Inside a turn the model MUST see what it just did, or it repeats it."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    body = "y=2;\n" * 400
    state.append_user("go")
    state.append_assistant("", tool_calls=_write_call_body("recent.js", body))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    loop._elide_payloads()

    arguments = state.all_messages[2].tool_calls[0]["function"]["arguments"]
    assert arguments["content"] == body, "the current edit was elided out from under it"


def test_shell_output_is_compacted_not_discarded(tmp_path):
    """A stack trace cannot be fetched back by calling anything."""
    from shamsu.agents.simple_chat import elide_tool_result

    trace = json.dumps({
        "ok": False,
        "message": "FAILED\n" + "\n".join(f"  frame {i}" for i in range(200)) + "\nAssertionError: boom",
    })
    kept = elide_tool_result("run_command", trace)

    assert "FAILED" in kept
    assert "AssertionError: boom" in kept, "the end of a trace is where the answer is"
    assert "elided" in kept
    assert len(kept) < len(trace) / 2


def test_a_file_result_says_how_to_get_it_back(tmp_path):
    from shamsu.agents.simple_chat import elide_tool_result

    payload = json.dumps({"ok": True, "message": "Read game.js",
                          "data": {"content": "z=3;\n" * 500, "total_lines": 500}})
    kept = elide_tool_result("read_file", payload)

    assert "read_file" in kept, "the model must be told how to recover it"
    assert "z=3" not in kept
    assert json.loads(kept)["data"]["total_lines"] == 500


def test_elision_never_orphans_a_tool_result_from_its_call(tmp_path):
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(30):
        state.append_assistant("", tool_calls=_write_call_body(f"f{i}.js", "q=1;\n" * 300))
        state.append_tool(f"call-{i}", "write_file", json.dumps({"ok": True, "message": "wrote"}))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    loop._elide_payloads()

    messages = state.all_messages
    owners = sum(1 for m in messages if m.role == "assistant" and m.tool_calls)
    results = sum(1 for m in messages if m.role == "tool")
    assert owners == results == 30, "elision must rewrite content, never remove messages"


def test_a_long_edit_turn_elides_before_it_reaches_the_user(tmp_path):
    """Waiting for the next user message is too late - the turn fills the window."""
    body = chr(10).join(f"const line{i} = {i};" for i in range(400))
    (tmp_path / "big.js").write_text(body, encoding="utf-8")

    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(30):   # a turn already well into the window
        state.append_user(f"step {i}")
        state.append_assistant("", tool_calls=_write_call_body(f"f{i}.js", body))
    loop = _loop(tmp_path, [_tool("read_file", filepath="big.js")] * 4 + [_text("done")])
    loop.state = state

    asyncio.run(loop.run("study big.js"))

    assert loop.evictions > 0, "no mid-turn sweep ever elided anything"
    assert any(m.elided for m in loop.state.all_messages)


def test_a_short_turn_does_not_elide_what_it_is_working_on(tmp_path):
    """Under no pressure, the sweep must leave the current edit alone."""
    (tmp_path / "small.js").write_text("const a = 1;", encoding="utf-8")
    loop = _loop(tmp_path, [_tool("read_file", filepath="small.js")] * 4 + [_text("done")])

    asyncio.run(loop.run("read small.js"))

    assert loop.evictions == 0

def test_the_reply_reserve_is_actually_available_after_budgeting(tmp_path):
    """Item A's own acceptance test, measured on a REAL assembled prompt.

    The reserve promises ~8,192 tokens at a 32k window. Charging `tool_calls`
    alone still leaves the tool schemas, the grounding block and the rolling
    summary uncounted - about 3,900 tokens - so headroom would come back as
    ~5,300 while every internal number claimed it was fixed. This builds the
    prompt the way `_call_model` does and measures what is actually left.
    """
    from shamsu.agents.simple_chat import output_reserve
    from shamsu.context.budget import messages_tokens, tool_schema_tokens

    for i in range(40):
        (tmp_path / f"file{i}.py").write_text("x = 1" + chr(10) * 2, encoding="utf-8")
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    body = "const value = 1;" + chr(10)
    for i in range(60):
        state.append_user(f"step {i} " + "padding " * 80)
        state.append_assistant("", tool_calls=_write_call_body(f"f{i}.js", body * 200))
    state.update_rolling_summary("- an earlier decision worth keeping" * 40, start_abs=1)

    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state
    loop._files = workspace_files(tmp_path)

    prompt = loop._messages()
    ceiling = loop._num_ctx(prompt)
    sent = messages_tokens(prompt) + tool_schema_tokens(SIMPLE_TOOL_SCHEMAS)
    headroom = ceiling - sent

    assert headroom >= output_reserve(ceiling), (
        f"prompt is {sent} of a {ceiling} window, leaving {headroom} to answer "
        f"in; the reserve promises {output_reserve(ceiling)}"
    )


def test_the_tool_schemas_sent_on_every_call_are_charged(tmp_path):
    """~630 tokens, on every single request, counted nowhere."""
    from shamsu.agents.simple_chat import SIMPLE_TOOL_SCHEMAS
    from shamsu.context.budget import tool_schema_tokens

    loop = _loop(tmp_path, [_text("ok")])

    assert tool_schema_tokens(SIMPLE_TOOL_SCHEMAS) > 300
    assert loop._fixed_overhead() >= tool_schema_tokens(SIMPLE_TOOL_SCHEMAS)


# ---------------------------------------------------------------------------
# Context meter and counters (SMALLCODE plan item E)
#
# A whole session re-compacted the same 23 messages every turn and it was
# found by reading scrollback. A counter makes that class of bug obvious.
# ---------------------------------------------------------------------------


def _reset_counters():
    from shamsu.agents.simple_chat import SESSION_COUNTERS

    for field_name in ("compactions", "evictions", "truncations", "calls",
                       "last_prompt_tokens", "last_window", "last_estimate",
                       "total_prompt", "total_completion"):
        setattr(SESSION_COUNTERS, field_name, 0)
    return SESSION_COUNTERS


def test_the_meter_reports_the_real_prompt_size_not_the_estimate(tmp_path):
    counters = _reset_counters()
    loop = _loop(tmp_path, [{"message": {"content": "hi", "tool_calls": []},
                             "prompt_eval_count": 22_300}])

    asyncio.run(loop.run("hello"))

    assert counters.last_prompt_tokens == 22_300
    assert "22.3k" in counters.meter()
    assert f"{counters.pct}%" in counters.meter()


def test_a_truncated_reply_is_counted(tmp_path):
    counters = _reset_counters()
    loop = _loop(tmp_path, [_cut(content="half an ans")])

    asyncio.run(loop.run("explain"))

    assert counters.truncations == 1


def test_the_user_is_warned_on_the_way_up_and_only_once(tmp_path):
    """At the wall the only thing left to say is that it was already cut."""
    counters = _reset_counters()
    said: list[str] = []
    full = {"message": {"content": "", "tool_calls": [
        {"function": {"name": "list_files", "arguments": {"path": "."}}}]},
        "prompt_eval_count": 30_000}
    loop = _loop(tmp_path, [full, full, _text("done")], on_activity=said.append)

    asyncio.run(loop.run("go"))

    warnings = [m for m in said if "filling the window" in m]
    assert len(warnings) == 1, f"expected exactly one warning, got {warnings}"
    assert counters.pct >= 80


def test_a_quiet_session_warns_about_nothing(tmp_path):
    _reset_counters()
    said: list[str] = []
    loop = _loop(tmp_path, [{"message": {"content": "hi", "tool_calls": []},
                             "prompt_eval_count": 900}], on_activity=said.append)

    asyncio.run(loop.run("hello"))

    assert not [m for m in said if "filling the window" in m]


# ---------------------------------------------------------------------------
# @file expansion (SMALLCODE plan item G)
# ---------------------------------------------------------------------------


def test_an_at_mention_arrives_as_content_not_as_a_literal_string(tmp_path):
    """The user typed @SPEC.md and the model had to spend a round on read_file."""
    (tmp_path / "SPEC.md").write_text("The ship must slow to 4.5 max speed.", encoding="utf-8")
    loop = _loop(tmp_path, [_text("understood")])

    asyncio.run(loop.run("build what @SPEC.md describes"))

    sent = json.dumps(loop.client.calls[0]["messages"])
    assert "must slow to 4.5" in sent, "the file was never expanded"
    assert loop.client.calls[0]["messages"][-1]["role"] == "user"


def test_the_expansion_is_sent_but_never_persisted(tmp_path):
    """A resumed session must replay what was typed, not a stale file copy."""
    (tmp_path / "SPEC.md").write_text("original contents", encoding="utf-8")
    logger = _session(tmp_path)
    loop = SimpleChatLoop(
        tmp_path,
        client=FakeClient([_text("ok")]),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        session_logger=logger,
        model_name="qwen3:8b",
    )

    asyncio.run(loop.run("summarise @SPEC.md"))

    stored = [m for m in logger.read_messages(10) if m["role"] == "user"]
    assert stored[-1]["content"] == "summarise @SPEC.md"
    assert "original contents" not in stored[-1]["content"]


def test_a_decorator_is_not_mistaken_for_a_mention(tmp_path):
    """`@app.route(...)` in pasted code must not turn a request into a file dump."""
    loop = _loop(tmp_path, [_text("ok")])

    asyncio.run(loop.run("why does @app.route('/') fail?"))

    sent = loop.client.calls[0]["messages"][-1]["content"]
    assert "Mentioned file context" not in sent


def test_a_huge_mention_cannot_swallow_the_window(tmp_path):
    from shamsu.agents.simple_chat import MAX_MENTION_TOKENS
    from shamsu.context.budget import count_tokens

    (tmp_path / "HUGE.md").write_text("padding word " * 40_000, encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    asyncio.run(loop.run("read @HUGE.md"))

    sent = loop.client.calls[0]["messages"][-1]["content"]
    assert count_tokens(sent) < MAX_MENTION_TOKENS * 1.5
    assert "read_file" in sent, "it must say how to get the rest"


# ---------------------------------------------------------------------------
# Per-category budget buckets (SMALLCODE plan item F)
#
# One total tells you the window is full and never what filled it, so the only
# available response is to drop the OLDEST messages - which on a real session
# evicts a user's early decisions while leaving a 2,618-token write_file
# payload untouched.
# ---------------------------------------------------------------------------


def test_tool_results_are_the_majority_bucket_on_an_edit_session(tmp_path):
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    body = "const value = 1;" + chr(10)
    for i in range(20):
        state.append_user(f"change thing {i}")
        state.append_assistant("", tool_calls=_write_call_body(f"f{i}.js", body * 200))
        state.append_tool("", "write_file", json.dumps(
            {"ok": True, "message": "wrote", "data": {"content": body * 200}}))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    allocation = loop.token_allocation()

    assert allocation.fattest() == "tool results"
    assert allocation.tool_results > allocation.conversation


def test_a_write_payload_is_charged_to_tools_not_to_conversation(tmp_path):
    """Lumping the two hid the biggest items behind a label that looked like chat."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    state.append_assistant("I will write it", tool_calls=_write_call_body("a.js", "x" * 20_000))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    allocation = loop.token_allocation()

    assert allocation.tool_results > 1000
    assert allocation.conversation < 100, "the payload was counted as conversation"


def test_a_plain_conversation_is_not_attacked_by_payload_elision(tmp_path):
    """Eliding harder reclaims nothing when the fat bucket is the chat itself."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(60):
        state.append_user(f"question {i} " + "words " * 200)
        state.append_assistant(f"answer {i} " + "words " * 200)
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state
    said: list[str] = []
    loop.on_activity = said.append

    allocation = loop.token_allocation()
    loop._elide_under_pressure()

    assert allocation.fattest() == "conversation"
    assert any("will not help much" in m for m in said)


def test_the_buckets_add_up_to_what_the_budget_counts(tmp_path):
    """The breakdown and the trimmer must never disagree about the same prompt."""
    from shamsu.context.budget import messages_tokens, tool_schema_tokens

    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(10):
        state.append_user(f"u{i}")
        state.append_assistant("", tool_calls=_write_call_body(f"f{i}.js", "y" * 5000))
        state.append_tool("", "write_file", json.dumps({"ok": True, "message": "wrote"}))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state
    loop._files = workspace_files(tmp_path)

    allocation = loop.token_allocation()
    counted = (
        messages_tokens(m.to_ollama() for m in state.all_messages)
        + tool_schema_tokens(SIMPLE_TOOL_SCHEMAS)
        + allocation.grounding
    )

    assert abs(allocation.total - counted) <= 2, f"{allocation.total} vs {counted}"


# ---------------------------------------------------------------------------
# Working memory (SMALLCODE plan item H)
#
# Distinct from the rolling summary: that is OUR lossy digest, written when the
# window fills. This is the model's own note, written when it decides
# something, and it survives compaction because it was never part of the
# conversation being compacted.
# ---------------------------------------------------------------------------


def test_a_remembered_fact_reaches_the_next_turns_prompt(tmp_path):
    loop = _loop(tmp_path, [_tool("remember", note="the dev server runs on port 8080"),
                            _text("noted")])

    asyncio.run(loop.run("we settled on port 8080"))

    later = _loop(tmp_path, [_text("still 8080")])
    asyncio.run(later.run("what port again?"))

    sent = json.dumps(later.client.calls[0]["messages"])
    assert "port 8080" in sent


def test_working_memory_survives_compaction(tmp_path):
    """The rolling summary is lossy by definition; a deliberate note is not."""
    from shamsu.agents.simple_memory import remember, render_memory

    remember(tmp_path, "the window is 900x700")
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(60):
        state.append_user(f"turn {i} " + "padding " * 200)
        state.append_assistant(f"reply {i} " + "padding " * 200)
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state
    loop._files = workspace_files(tmp_path)

    prompt = json.dumps(loop._messages())

    assert "900x700" in prompt


def test_the_memory_block_is_charged_to_the_budget(tmp_path):
    """A permanent block nobody counts is the exact bug item A fixed."""
    from shamsu.agents.simple_memory import remember

    loop = _loop(tmp_path, [_text("ok")])
    before = loop._fixed_overhead()
    for i in range(20):
        remember(tmp_path, f"decision number {i} about the architecture of the thing")

    assert loop._fixed_overhead() > before


def test_working_memory_cannot_grow_without_limit(tmp_path):
    from shamsu.agents.simple_memory import MAX_MEMORY_TOKENS, remember, render_memory
    from shamsu.context.budget import count_tokens

    for i in range(200):
        remember(tmp_path, f"fact {i} " + "detail " * 20)

    assert count_tokens(render_memory(tmp_path)) <= MAX_MEMORY_TOKENS * 1.2


def test_the_oldest_notes_go_first(tmp_path):
    """A note from before the project took its current shape is the stale one."""
    from shamsu.agents.simple_memory import remember, render_memory

    remember(tmp_path, "OLDEST decision " + "x " * 100)
    for i in range(40):
        remember(tmp_path, f"newer decision {i} " + "y " * 20)

    kept = render_memory(tmp_path)
    assert "OLDEST decision" not in kept
    assert "newer decision 39" in kept


def test_the_same_fact_is_not_remembered_twice(tmp_path):
    from shamsu.agents.simple_memory import read_memory, remember

    remember(tmp_path, "the port is 8080")
    ok, message = remember(tmp_path, "the  port   is 8080")

    assert ok
    assert "Already remembered" in message
    assert read_memory(tmp_path).count("port is 8080") == 1


def test_an_empty_note_says_what_to_do_instead(tmp_path):
    from shamsu.agents.simple_memory import remember

    ok, message = remember(tmp_path, "   ")

    assert not ok
    assert "note" in message


# ---------------------------------------------------------------------------
# Methods taken from smallcode's own source (not from the plan's summary of it)
# ---------------------------------------------------------------------------


def test_search_finds_code_by_meaning_when_no_word_matches(tmp_path):
    """`grep_files` matched with `query in line` - a literal substring.

    So "the function that validates tokens" found nothing, and the model was
    told "Found 0 match(es)" as though it had asked a fair question.
    """
    (tmp_path / "auth.py").write_text(
        "def validateAuthToken(raw):\n"
        "    return raw and len(raw) > 10\n",
        encoding="utf-8",
    )
    (tmp_path / "unrelated.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("search_files", {"query": "the function that validates tokens"})

    assert result.ok
    assert "auth.py" in result.message
    assert result.data["matches"][0]["symbol"] == "validateAuthToken"


def test_search_still_honours_a_real_regex(tmp_path):
    """Substring matching meant a regex was searched for literally."""
    (tmp_path / "views.py").write_text(
        "def handle_user_login():\n    pass\n\ndef handle_admin_login():\n    pass\n",
        encoding="utf-8",
    )
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("search_files", {"query": r"def handle_\w+_login", "mode": "regex"})

    assert result.ok
    assert result.data["count"] >= 1
    assert all(m["exact"] for m in result.data["matches"])


def test_an_invalid_regex_is_searched_for_literally_not_refused(tmp_path):
    """Refusing costs a round and teaches nothing."""
    (tmp_path / "a.py").write_text("x = handle_(1)\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("search_files", {"query": "handle_("})

    assert result.ok


def test_search_falls_back_to_grep_rather_than_erroring(tmp_path):
    """A search that errors is worse than a plain one."""
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("search_files", {"query": "anything"})

    assert result.ok  # empty workspace -> grep path, still a clean answer


def test_thinking_is_switched_off_once_the_turn_is_repairing(tmp_path):
    """smallcode: on a retry the model 'already overthought the original'."""
    loop = _loop(tmp_path, [_text("ok")])
    assert loop._should_disable_thinking() is False

    loop._repair_attempts = 2

    assert loop._should_disable_thinking() is True


def test_thinking_can_be_switched_off_entirely_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_THINKING_DISABLE", "1")
    loop = _loop(tmp_path, [_text("ok")])

    assert loop._should_disable_thinking() is True


def test_efficiency_reports_output_earned_per_token_of_context(tmp_path):
    """The number that says whether the context work is paying off."""
    counters = _reset_counters()
    loop = _loop(tmp_path, [{"message": {"content": "hi", "tool_calls": []},
                             "prompt_eval_count": 1000, "eval_count": 250}])

    asyncio.run(loop.run("hello"))

    assert counters.total_prompt == 1000
    assert counters.total_completion == 250
    assert abs(counters.efficiency - 25.0) < 0.01
    assert counters.average_prompt == 1000
