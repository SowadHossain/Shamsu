"""Simple mode: Ollama chat with coding tools attached.

Each test names a behaviour the user asked for, not a mechanism, so a later
refactor that quietly reintroduces the ceremony fails here.
"""
from __future__ import annotations

import asyncio
import json
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


def test_only_the_six_tools_are_offered(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])
    asyncio.run(loop.run("hi"))

    offered = {t["function"]["name"] for t in loop.client.calls[0]["tools"]}
    assert offered == {
        "read_file", "list_files", "search_files", "write_file", "patch_file", "run_command",
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


def test_a_short_conversation_does_not_allocate_the_whole_window(tmp_path):
    """Ollama reserves KV for the whole num_ctx; asking 32k for an 8k prompt is
    what spilled the cache to system RAM and made first token take 83s."""
    loop = _loop(tmp_path, [_text("hi")])

    asyncio.run(loop.run("hello"))

    assert loop.client.calls[0]["options"]["num_ctx"] == CTX_BUCKETS[0]


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

    first = loop.client.calls[0]["messages"][1]["content"]
    assert "new.py" not in first
    # The loop refreshes the listing at the top of each round.
    asyncio.run(loop.run("what files exist?"))
    latest = loop.client.calls[-1]["messages"][1]["content"]
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

    grounding = loop.client.calls[0]["messages"][1]["content"]
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


def test_the_default_context_fits_an_8gb_card(tmp_path):
    """~5GB of weights + 144 KiB/token of KV: 16k costs ~2.25GB, 32k ~4.5GB."""
    from shamsu.agents.simple_chat import max_ctx

    assert max_ctx() == 16384


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
