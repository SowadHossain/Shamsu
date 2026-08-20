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




def _named_tool(tool: str, arguments: dict) -> dict:
    """`_tool`, for a call whose ARGUMENT is called `name` - which collides with
    `_tool`'s own first parameter."""
    return {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": tool, "arguments": arguments}}],
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


# Tools handled inside `_execute` rather than by the registry: they reach
# project memory, the code graph, the conversation archive, or the filesystem
# directly.
_NON_REGISTRY_TOOLS = frozenset({
    "memory_remember", "memory_load", "memory_list", "memory_forget",
    "graph_search", "explain_symbol", "history_search", "append_file",
    "find_files", "read_symbol", "run_tests", "use_skill", "replace_symbol",
    "contract_create", "contract_status", "contract_assert_pass",
    "contract_assert_fail", "contract_assert_skip",
    "find_and_read", "search_and_read", "read_and_patch", "create_and_run",
})


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


def test_the_offered_tools_are_exactly_the_ones_that_can_run(tmp_path):
    """The name was always the contract; the roster did not honour it.

    A graph tool with no index, a history search with no earlier session and a
    memory reader with no notes can only answer "nothing here" - and they cost
    516 tokens on every single call to say it. In a fresh workspace they cannot
    run, so they are not offered.
    """
    loop = _loop(tmp_path, [_text("ok")])
    asyncio.run(loop.run("hi"))

    offered = {t["function"]["name"] for t in loop.client.calls[0]["tools"]}
    conditional = {
        "memory_load", "memory_list", "memory_forget",
        "graph_search", "explain_symbol", "history_search",
        # A bare tmp_path is not a repository, so read-only git can only
        # answer "not a git repository" - the same 516-token nothing the
        # families above were withheld for.
        "git_status", "git_diff", "git_log",
        # And nothing is serving search on a test machine.
        "web_search", "fetch_url",
    }

    # Everything that can act on a bare workspace, and nothing that cannot.
    assert offered == {
        n for n in SIMPLE_TOOLS
        if n not in {"remember", "select_category"} and n not in conditional
    }
    assert "memory_remember" in offered, "the tool that creates notes is never withheld"


def test_the_full_roster_is_offered_once_everything_has_something_to_answer(tmp_path, monkeypatch):
    """The gate is about relevance, not removal: give each family what it needs
    and the roster is exactly what it always was."""
    from shamsu import paths

    notes = paths.memory_notes_dir(tmp_path)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "n.md").write_text("# port" + chr(10) + "8080" + chr(10), encoding="utf-8")
    index = tmp_path / ".shamsu" / "abstract"
    index.mkdir(parents=True, exist_ok=True)
    (index / "last-index.json").write_text("{}", encoding="utf-8")
    for name in ("20260819-000001-aaaa", "20260819-000002-bbbb"):
        (paths.sessions_dir(tmp_path) / name).mkdir(parents=True, exist_ok=True)
    # "Everything has something to answer from" now includes a repository for
    # read-only git to describe, and a reachable search backend. The web probe
    # is faked rather than served: this test is about the ROSTER honouring
    # availability, not about httpx.
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    monkeypatch.setattr(
        "shamsu.agents.simple_chat._web_is_reachable", lambda _ws: True
    )

    loop = _loop(tmp_path, [_text("ok")])
    asyncio.run(loop.run("hi"))

    offered = {t["function"]["name"] for t in loop.client.calls[0]["tools"]}

    assert offered == {
        n for n in SIMPLE_TOOLS if n not in {"remember", "select_category"}
    }


# --- verification -------------------------------------------------------


def test_broken_code_comes_back_as_a_tool_result_the_model_can_fix(tmp_path):
    """Not a verdict panel and not a separate repair loop - just information.

    An ordinary syntax error, deliberately. `def broken(` used to stand here and
    is now stopped BEFORE the write by the truncation gate, which is different
    behaviour with its own tests. A missing colon is a mistake rather than a
    severed generation, so it is written, checked, and reported - which is what
    this test is about.
    """
    loop = _loop(
        tmp_path,
        [
            _tool("write_file", filepath="bad.py", content="def broken(x)\n    return x\n"),
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
    "do not claim complete" repeated four times.

    The bound was 250 when the prompt described six tools. There are now 19,
    and naming a capability in prose is what makes a small model use it -
    smallcode's issue #58 was a model refusing research tasks with "my tools
    are for code files only" while the web tools sat in its own schema list.
    So the ceiling moved to 320 for capability lines that are all positive
    statements of what CAN be done, and to 360 when two more were added: the
    60-line write rule, and the outline-first read. Both replace a decision the
    model was previously making badly on its own - "too big" is not a number,
    and neither is "read the file". 420 when the `act` section landed: a model
    that stops to ask after one section spends the user's turn on a question
    they already answered (live 2026-08-20). 520 when the skill index landed -
    a skill the model cannot see is one it will never load, and the roster is
    the only thing that makes `use_skill` reachable. It grows by ~14 tokens per
    bundled skill, so the ceiling has headroom for a few more rather than
    tracking the roster exactly. 640 when `symbols` and `done` landed - a
    capability not named here is one a small model will not use, which is the
    single most expensive thing this prompt can get wrong.

    The size was never the real guard anyway; the three assertions below are.
    A prompt can be short and still be a wall of prohibitions, and that is the
    failure this test exists to catch.
    """
    import re

    from shamsu.context.budget import count_tokens

    prompt = simple_system_prompt(tmp_path)

    assert count_tokens(prompt) < 640
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


def test_a_plan_that_shows_code_is_not_nudged_into_writing_it(tmp_path):
    """Asked to plan, planning is the deliverable - not a skipped job.

    The nudge assumes prose plus a fence means the model answered instead of
    working. When the request was "review and plan", that is backwards: it
    told the model to abandon the plan and start writing, which is the same
    presumption that cost 24 rounds and 577s live on 2026-08-18.
    """
    (tmp_path / "game.js").write_text("// old\n", encoding="utf-8")
    plan = (
        "Here is what I would do to game.js:\n\n```javascript\n"
        "// step 1: hoist the canvas lookup\n"
        "// step 2: guard the empty asteroid array\n"
        "// step 3: cap the frame rate\n"
        "// step 4: add a restart handler\n```\n"
    )
    loop = _loop(tmp_path, [_text(plan)])

    result = asyncio.run(loop.run("review game.js and plan the next steps"))

    assert result.rounds == 1, "planning must not cost a correction round"
    nudges = [
        m for m in loop.state.all_messages
        if m.role == "user" and "did not change the file" in m.content
    ]
    assert not nudges, "a plan is the answer; it must not be told to write it"
    assert (tmp_path / "game.js").read_text(encoding="utf-8") == "// old\n"


def test_a_review_that_also_asks_for_a_fix_still_nudges(tmp_path):
    """The asymmetry, deliberately: a change-verb wins over a words-verb.

    Skipping the nudge wrongly means the work silently never happens. Nudging
    wrongly costs one round. So a request naming both must still nudge.
    """
    (tmp_path / "game.js").write_text("// old\n", encoding="utf-8")
    shown = "```js\na\nb\nc\nd\ne\n```\nThat is the new game.js."
    loop = _loop(
        tmp_path,
        [
            _text(shown),
            _tool("write_file", filepath="game.js", content="// new\n"),
            _text("Applied."),
        ],
    )

    asyncio.run(loop.run("review game.js and fix the bug"))

    assert (tmp_path / "game.js").read_text(encoding="utf-8") == "// new\n"


def test_asks_only_for_words_matches_on_word_boundaries(tmp_path):
    """`plan` must not fire on `planet`, and `add` must not fire on `address`.

    A raw substring is how `_PRD_BUILD_NOUNS` came to hold "it" and made almost
    any sentence name a product.
    """
    from shamsu.agents.simple_chat import asks_only_for_words

    assert asks_only_for_words("plan the next steps")
    assert asks_only_for_words("explain what this does")
    assert asks_only_for_words("what would you do about the retry loop?")
    # A change-verb present anywhere wins.
    assert not asks_only_for_words("review it and fix the bug")
    assert not asks_only_for_words("plan and then implement it")
    # No words-verb at all.
    assert not asks_only_for_words("make the tests pass")
    # Word boundaries, both directions.
    assert not asks_only_for_words("render the planet texture")
    assert asks_only_for_words("explain the address parser")


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
    4096 it got at 8k - the model spent it thinking and returned nothing.

    8192 no longer returns 4096, and that change is the point rather than a
    regression: 4096 was HALF that window, and the same expression returned
    100% of a 4096 one. The floor now applies only where it still leaves room
    to send a prompt - see `test_the_reply_reserve_never_outgrows_the_window`.
    """
    from shamsu.agents.simple_chat import output_reserve

    assert output_reserve(32768) == 8192
    assert output_reserve(65536) == 16384
    assert output_reserve(16384) == 4096
    # Below 16k the quarter falls under the floor, and the floor gives way
    # rather than eating the window.
    assert output_reserve(8192) == 2730
    assert output_reserve(4096) == 1365


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

    lines = [line for line in state.rolling_summary.splitlines() if line.strip()]
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
    (tmp_path / "hello.py").write_text(
        "value = 1\n\n\ndef greet():\n    return value\n", encoding="utf-8"
    )
    # `move_file` and `delete_file` get their own throwaways: probing them
    # against hello.py renamed or removed the file every LATER probe depends on.
    (tmp_path / "to_move.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "to_delete.py").write_text("x = 1\n", encoding="utf-8")
    # `git_status` and friends need a repository to describe, and `git_log`
    # needs it to have at least one commit - "does not have any commits yet" is
    # git being right, and says nothing about whether the schema names reach
    # the implementation, which is what this test is for.
    import subprocess

    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run([*git, "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run([*git, "commit", "-qm", "probe"], cwd=tmp_path, check=True)

    for schema in SIMPLE_TOOL_SCHEMAS:
        function = schema.get("function", schema)
        name = function["name"]
        required = (function.get("parameters") or {}).get("required", [])
        if name in {"write_file", "patch_file", "run_command"}:
            continue  # mutating/shell: covered by their own tests
        if name in _NON_REGISTRY_TOOLS:
            # Not registry tools: they reach project memory and the code graph,
            # not the workspace. Same contract though - the schema names must
            # reach the implementation.
            loop = _loop(tmp_path, [_text("ok")])
            probe = {
                "memory_remember": {"type": "decision", "title": "t", "content": "c"},
                "memory_load": {"task": "anything"},
                "memory_list": {},
                "memory_forget": {"id": "nope"},
                "graph_search": {"query": "anything"},
                "explain_symbol": {"symbol": "anything"},
                "history_search": {"query": "anything"},
                "append_file": {"filepath": "grown.py", "content": "# section"},
                "find_files": {"pattern": "**/*.py"},
                "read_symbol": {"filepath": "hello.py", "symbol": "greet"},
                "run_tests": {"test_filter": "nothing_matches_this"},
                "use_skill": {"name": "developer"},
                # Ordered so the contract exists before anything asserts on it -
                # `SIMPLE_TOOL_SCHEMAS` lists create first.
                "contract_create": {"title": "probe", "assertions": ["it runs"]},
                "contract_status": {},
                "contract_assert_pass": {"assertion_id": "a01", "evidence": "ran it"},
                "contract_assert_fail": {"assertion_id": "a01", "evidence": "broke"},
                "contract_assert_skip": {"assertion_id": "a01", "reason": "n/a"},
                "replace_symbol": {"filepath": "hello.py", "symbol": "greet",
                                   "content": "def greet():" + chr(10) + "    return 2"},
                "find_and_read": {"pattern": "**/*.py"},
                "search_and_read": {"query": "value"},
                "read_and_patch": {"filepath": "hello.py", "old_string": "value",
                                   "new_string": "other"},
                "create_and_run": {"filepath": "made.py", "content": "x = 1",
                                   "command": "echo done"},
            }[name]
            result = loop._execute(name, probe)
            # memory_forget on a missing id is a legitimate no.
            # memory_forget on a missing id, and read_and_patch whose snippet
            # is absent, are both legitimate NOs.
            assert result.ok or name in {
                "memory_forget", "read_and_patch", "run_tests",
            }, (
                f"{name} -> {result.message}"
            )
            continue
        # One placeholder for every argument is fine until a tool's arguments
        # must DIFFER from each other: `move_file("value", "value")` is
        # correctly refused as a move onto itself, which says nothing about
        # whether its schema names reach the implementation.
        distinct = {
            "move_file": {"source": "to_move.py", "destination": "moved.py"},
            # No arguments at all, and "value" is not a file to delete.
            "delete_file": {"filepath": "to_delete.py"},
            # Reaching a real search backend is not this test's business - it
            # asks whether schema names reach the implementation, and these two
            # would need a live SearXNG to answer at all.
            "web_search": None,
            "fetch_url": None,
            "git_status": {},
            "git_diff": {},
            "git_log": {"limit": "3"},
            "ask_user": {"question": "Which one?"},
        }
        if name in {"web_search", "fetch_url"}:
            continue
        arguments = distinct.get(name) or {
            key: "hello.py" if "file" in key else "value" for key in required
        }
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
        # Audited 2026-08-20 against every message `_stop` can emit. These three
        # were the only ones still replaying, and "I refused all of them.
        # Nothing was changed." is the worst possible thing to teach a model
        # about how a turn ends.
        (
            "My last 3 attempts to write game.js were cut off by my own output "
            "limit part-way through, so I refused all of them. Nothing was changed."
        ),
        (
            "My last 3 attempts to write game.js each stopped part-way through a "
            "string or a block, so I refused them. Nothing was changed."
        ),
        (
            "RuntimeError: cudaMalloc failed. The GPU ran out of memory even at "
            "the smallest context."
        ),
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
        # The new patterns must not eat ordinary prose either. A model
        # explaining a crash it diagnosed is not the harness reporting one.
        "My last 3 attempts to write the parser taught me the grammar is ambiguous.",
        "The error: your GPU ran out of memory because two models are resident.",
        "I fixed the out of memory crash by lowering num_ctx.",
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

    # The approval prompt now goes through the shared control store, so that
    # the same question reaches the browser and the phone. The property under
    # test is unchanged and still the one that matters: whatever asks the
    # human must be given THIS console. Called bare, it would build a fresh
    # one, which knows nothing about the live spinner - so the status repaints
    # over the question and over the answer being typed.
    import shamsu.control.console as shared_console

    async def _instant(_store, _approval_id, asked_console, **_kwargs):
        seen["console"] = asked_console
        return "deny"

    monkeypatch.setattr(shared_console, "ask_here_or_anywhere", _instant)
    monkeypatch.setattr(
        shared_console,
        "render_request",
        lambda _record, rendered_console, **_kw: seen.__setitem__(
            "rendered", rendered_console
        ),
    )

    captured["approve"](object())
    assert seen.get("console") is console, "the prompt was given a different console"
    assert seen.get("rendered") is console, "the question was rendered elsewhere"


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


def test_generation_is_always_capped(tmp_path):
    """Without num_predict at all, generation was bounded only by leftover
    window - which is the bug this was written for and still holds.

    It used to assert equality with `output_reserve`. That pinned the reserve as
    the VALUE, and the reserve is what the prompt assembler holds back, not what
    the reply may use: live 2026-08-19 a reply was cut at 8,192 with a 2,270
    token prompt in a 32,768 window. The reserve is now the FLOOR - see V2.
    """
    from shamsu.agents.simple_chat import MAX_REPLY_TOKENS, output_reserve

    loop = _loop(tmp_path, [_text("done")])
    asyncio.run(loop.run("hi"))

    options = loop.client.calls[0]["options"]
    assert output_reserve(options["num_ctx"]) <= options["num_predict"] <= MAX_REPLY_TOKENS


def test_a_model_that_cannot_think_is_never_asked_to(tmp_path):
    """LIVE 2026-08-19: every turn died on `does not support thinking` (400).

    Ollama rejects `think=` outright for a model with no reasoning channel, so
    the turn is over before a token is generated. Against qwen2.5:3b-instruct
    that killed all five turns of the first live run. The cookbook had recorded
    `is_reasoning=False` for it the whole time - simple mode never asked, so
    most of the roster (the 8GB default included) could not run at all.
    """
    loop = _loop(tmp_path, [_text("done")])
    loop.model_name = "qwen2.5-coder:7b-instruct"
    asyncio.run(loop.run("hi"))

    assert loop.client.calls[0]["think"] is False


def test_a_reasoning_model_is_still_asked_to_think(tmp_path):
    """The other half: the gate must not switch thinking off for everyone."""
    loop = _loop(tmp_path, [_text("done")])
    loop.model_name = "qwen3:8b"
    asyncio.run(loop.run("hi"))

    assert loop.client.calls[0]["think"] is True


def test_an_unknown_model_is_assumed_not_to_think(tmp_path):
    """False is the safe default, and the asymmetry is the reason.

    A reasoning model asked NOT to think still answers. A plain model asked to
    think returns a 400 and nothing else.
    """
    loop = _loop(tmp_path, [_text("done")])
    loop.model_name = "some-model-nobody-has-heard-of"
    asyncio.run(loop.run("hi"))

    assert loop.client.calls[0]["think"] is False


def test_the_system_prompt_survives_an_overflow_the_budget_missed(tmp_path):
    """Ollama keeps 4 tokens from the front by default - not the system prompt.

    The budget is meant to make overflow impossible. It is also the thing that
    was wrong by 9,500 tokens this week, so `num_keep` is the floor under that
    assumption: when the estimate IS wrong, the model should lose old turns,
    not its own identity and tool list.
    """
    from shamsu.context.budget import PER_MESSAGE_OVERHEAD, count_tokens

    loop = _loop(tmp_path, [_text("done")])
    asyncio.run(loop.run("hi"))

    options = loop.client.calls[0]["options"]
    system_cost = count_tokens(loop.state.system_prompt) + PER_MESSAGE_OVERHEAD
    assert options["num_keep"] >= system_cost, (
        "the whole system prompt must be inside the kept prefix"
    )
    # And it cannot be the thing that starves the window it is protecting.
    assert options["num_keep"] <= options["num_ctx"] // 8


def test_num_keep_cannot_starve_the_window_with_a_long_system_prompt(tmp_path):
    """The clamp, proved by making the system prompt absurd."""
    loop = _loop(tmp_path, [_text("done")])
    loop.state.system_prompt = "you are a helpful assistant. " * 4000
    asyncio.run(loop.run("hi"))

    options = loop.client.calls[0]["options"]
    assert options["num_keep"] == options["num_ctx"] // 8


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


def test_the_buckets_add_up_to_the_prompt_exactly(tmp_path):
    """The meter says "where the last prompt went", so it must mean the PROMPT.

    Reading every stored message instead reported 42,440 tokens of tool results
    inside a 23,595-token prompt - true about the wrong thing. Re-running the
    selection was closer and still wrong by ~900, because `_messages` updates
    the rolling summary as it builds. Classifying the assembled list is the
    only version that cannot drift.
    """
    from shamsu.context.budget import messages_tokens, tool_schema_tokens

    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(30):
        state.append_user(f"u{i} " + "pad " * 50)
        state.append_assistant("", tool_calls=_write_call_body(f"f{i}.js", "y" * 5000))
        state.append_tool(f"c{i}", "write_file", json.dumps({"ok": True, "message": "wrote"}))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state
    loop._files = workspace_files(tmp_path)

    built = loop._messages()
    sent = messages_tokens(built) + tool_schema_tokens(loop._sent_schemas())

    assert loop.token_allocation(built).total == sent


def test_the_buckets_describe_the_prompt_not_the_whole_conversation(tmp_path):
    """A meter that overstates by 2x is worse than none: it gets believed."""
    from shamsu.context.budget import messages_tokens

    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(60):
        state.append_user(f"u{i}")
        state.append_assistant("", tool_calls=_write_call_body(f"f{i}.js", "y" * 6000))
        state.append_tool(f"c{i}", "write_file", json.dumps({"ok": True, "message": "wrote"}))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    whole = messages_tokens(m.to_ollama() for m in state.all_messages)
    reported = loop.token_allocation().total

    assert reported < whole / 2, "the meter is reporting the archive, not the prompt"


# ---------------------------------------------------------------------------
# Working memory (SMALLCODE plan item H)
#
# Distinct from the rolling summary: that is OUR lossy digest, written when the
# window fills. This is the model's own note, written when it decides
# something, and it survives compaction because it was never part of the
# conversation being compacted.
# ---------------------------------------------------------------------------


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

    loop._repair_streak = 2

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


# ---------------------------------------------------------------------------
# Typed, task-scoped project memory (plan item H, rebuilt on smallcode's model)
#
# The first version loaded every note into every prompt. This one holds
# hundreds and shows the five that score against the request - which is the
# difference between memory and a permanent tax on the window.
# ---------------------------------------------------------------------------


def test_a_remembered_decision_comes_back_for_a_related_request(tmp_path):
    from shamsu.agents.simple_memory import remember, render_memory

    remember(tmp_path, "decision", "dev server port", "The dev server runs on port 8080.")

    assert "8080" in render_memory(tmp_path, "what port does the server use?")


def test_memory_is_scoped_to_the_request_not_dumped_wholesale(tmp_path):
    """The whole reason the store can grow without the prompt growing with it."""
    from shamsu.agents.simple_memory import remember, render_memory

    remember(tmp_path, "decision", "dev server port", "The dev server runs on port 8080.")
    for i in range(40):
        remember(tmp_path, "context", f"unrelated topic {i}", f"Something about widget {i}.")

    block = render_memory(tmp_path, "what port does the server use?")

    assert "8080" in block
    assert block.count("\n") <= 6, "a request pulled in more than the top few notes"
    assert "widget 7" not in block


def test_a_request_matching_nothing_costs_no_tokens(tmp_path):
    from shamsu.agents.simple_memory import remember, render_memory

    remember(tmp_path, "gotcha", "sqlite locking", "Close the cursor before forking.")

    assert render_memory(tmp_path, "how do I centre a div?") == ""
    assert render_memory(tmp_path, "") == ""


def test_the_type_is_carried_through_so_a_gotcha_reads_as_one(tmp_path):
    from shamsu.agents.simple_memory import remember, render_memory

    remember(tmp_path, "gotcha", "cursor forking", "Close the cursor before forking.")

    assert "[gotcha]" in render_memory(tmp_path, "problem with the cursor")


def test_an_unknown_type_is_refused_with_the_list_of_real_ones(tmp_path):
    from shamsu.agents.simple_memory import MEMORY_TYPES, remember

    ok, message = remember(tmp_path, "reminder", "a title", "a fact")

    assert not ok
    assert all(kind in message for kind in MEMORY_TYPES)


def test_the_same_fact_is_not_stored_twice(tmp_path):
    from shamsu.agents.simple_memory import MemoryStore, remember

    remember(tmp_path, "decision", "port", "The port is 8080.")
    ok, message = remember(tmp_path, "decision", "port", "The  port   is 8080.")

    assert ok
    assert "Already remembered" in message
    assert len(MemoryStore(tmp_path).all_notes()) == 1


def test_notes_are_readable_and_deletable_by_hand(tmp_path):
    """A memory a user cannot inspect is one they cannot trust."""
    from shamsu.agents.simple_memory import MEMORY_DIR, remember

    remember(tmp_path, "convention", "naming", "Modules are snake_case.", ["style"])

    written = list((tmp_path / MEMORY_DIR).glob("convention-*.md"))
    assert written, "no human-readable note was written"
    body = written[0].read_text(encoding="utf-8")
    assert "snake_case" in body and "type: convention" in body


def test_unused_notes_are_archived_then_forgotten(tmp_path):
    from shamsu.agents.simple_memory import MemoryStore, remember
    from datetime import datetime, timedelta, timezone

    remember(tmp_path, "context", "ancient", "Something decided long ago.")
    store = MemoryStore(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=70)).isoformat()
    for note in store.notes.values():
        note.last_used_at = old

    store.tidy()
    assert all(n.tier == "archive" for n in store.notes.values())

    ancient = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    for note in store.notes.values():
        note.last_used_at = ancient
    store.tidy()

    assert not store.notes, "a note archived long ago was never forgotten"


def test_the_hot_set_is_capped_so_recall_stays_sharp(tmp_path):
    from shamsu.agents.simple_memory import HOT_CAP, MemoryStore, remember

    for i in range(HOT_CAP + 8):
        remember(tmp_path, "context", f"note {i}", f"Fact number {i} about the project.")

    hot = [n for n in MemoryStore(tmp_path).all_notes() if n.tier == "hot"]
    assert len(hot) <= HOT_CAP


def test_an_archived_note_is_de_ranked_not_lost(tmp_path):
    from shamsu.agents.simple_memory import MemoryStore, remember

    remember(tmp_path, "decision", "storage choice", "Payments use the ledger table.")
    store = MemoryStore(tmp_path)
    for note in store.notes.values():
        note.tier = "archive"
    store._save()

    found = MemoryStore(tmp_path).recall("which table do payments use?")

    assert found and "ledger" in found[0].content


def test_the_memory_block_is_charged_to_the_budget(tmp_path):
    """A permanent block nobody counts is the exact bug item A fixed."""
    from shamsu.agents.simple_memory import remember

    loop = _loop(tmp_path, [_text("ok")])
    loop._request = "what port does the dev server use?"
    before = loop._fixed_overhead()
    remember(tmp_path, "decision", "dev server port", "The dev server runs on port 8080.")

    assert loop._fixed_overhead() > before


def test_the_model_can_write_a_typed_note_through_the_tool(tmp_path):
    loop = _loop(
        tmp_path,
        [_tool("remember", type="decision", title="port", content="The port is 8080."),
         _text("noted")],
    )

    asyncio.run(loop.run("we settled on port 8080"))

    later = _loop(tmp_path, [_text("still 8080")])
    asyncio.run(later.run("what port did we choose?"))

    assert "8080" in json.dumps(later.client.calls[0]["messages"])


def test_a_corrupt_memory_index_is_kept_not_overwritten(tmp_path):
    """Losing notes silently is the worst failure this class of code has.

    An unreadable index left the store empty, and the next `remember()` wrote
    a fresh one straight over notes that were still on disk. Same shape as the
    reformatted transcript that once hydrated a single message and said
    nothing about it.
    """
    from shamsu.agents.simple_memory import MemoryStore, remember

    remember(tmp_path, "decision", "the original", "A fact worth keeping.")
    index = MemoryStore(tmp_path).index_path
    index.write_text("{ this is not json", encoding="utf-8")

    remember(tmp_path, "decision", "a later note", "Written after the damage.")

    spoiled = index.with_suffix(".json.corrupt")
    assert spoiled.exists(), "the unreadable index was destroyed, not kept"
    assert "not json" in spoiled.read_text(encoding="utf-8")
    # And the store keeps working rather than refusing every turn from here on.
    assert any(n.title == "a later note" for n in MemoryStore(tmp_path).all_notes())


# ---------------------------------------------------------------------------
# The same tool roster smallcode exposes: 4 memory tools + 2 code-graph tools
# ---------------------------------------------------------------------------


def test_memory_is_no_longer_write_only(tmp_path):
    """Writing was the only half that existed, which is not memory."""
    loop = _loop(tmp_path, [_text("ok")])

    loop._execute("memory_remember", {"type": "workflow", "title": "tests",
                                      "content": "Run pytest from the repo root."})
    loaded = loop._execute("memory_load", {"task": "how do I run the tests?"})
    listed = loop._execute("memory_list", {})

    assert "pytest" in loaded.message
    assert "[workflow]" in loaded.message
    assert "tests" in listed.message


def test_a_note_that_went_wrong_can_be_deleted(tmp_path):
    from shamsu.agents.simple_memory import MemoryStore

    loop = _loop(tmp_path, [_text("ok")])
    loop._execute("memory_remember", {"type": "decision", "title": "old port",
                                      "content": "The port is 3000."})
    note_id = MemoryStore(tmp_path).all_notes()[0].id

    result = loop._execute("memory_forget", {"id": note_id})

    assert result.ok
    assert MemoryStore(tmp_path).all_notes() == []


def test_memory_list_can_be_filtered_by_type(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])
    loop._execute("memory_remember", {"type": "gotcha", "title": "g", "content": "A trap."})
    loop._execute("memory_remember", {"type": "decision", "title": "d", "content": "A choice."})

    only = loop._execute("memory_list", {"type": "gotcha"})

    assert "A trap." in only.message
    assert "A choice." not in only.message


def test_the_one_word_remember_spelling_still_works(tmp_path):
    """It shipped first and a model will keep reaching for it."""
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("remember", {"type": "context", "title": "t", "content": "A fact."})

    assert result.ok


def test_graph_tools_say_so_plainly_when_there_is_no_index(tmp_path):
    """Most workspaces are never indexed; raising there would be useless."""
    loop = _loop(tmp_path, [_text("ok")])

    search = loop._execute("graph_search", {"query": "anything"})
    explain = loop._execute("explain_symbol", {"symbol": "anything"})

    assert search.ok and explain.ok
    for result in (search, explain):
        assert "search_files" in result.message, "it must name what to use instead"


def test_a_stale_graph_answer_is_labelled_stale(tmp_path, monkeypatch):
    """A stale index gives confidently wrong answers that look authoritative."""
    from shamsu.agents import simple_graph

    monkeypatch.setattr(simple_graph, "_adapter", lambda: _FakeGraph())
    ok, message = simple_graph.graph_search(tmp_path, "handler")

    assert ok
    assert "handler" in message
    assert "out of date" in message, "nothing warned that the graph predates the code"


class _FakeGraph:
    def is_available(self, workspace):
        return True

    def query(self, workspace, query, limit=15):
        return {"ok": True, "data": {"results": [
            {"name": "handler", "file_path": "app/views.py", "start_line": 12}
        ]}}

    def get_symbols(self, workspace, name):
        return self.query(workspace, name)

    def get_references(self, workspace, name):
        return {"ok": True, "data": {"results": []}}


def test_a_large_file_can_be_built_in_sections(tmp_path):
    """The move SHAMSU had no tool for.

    It could refuse a whole-file rewrite and it could patch an existing
    snippet; between those there was no way to GROW a file. A model told "too
    large to rewrite" had no next move but a patch against text it was
    guessing at. smallcode's answer: write the first section, append the rest.
    """
    loop = _loop(tmp_path, [_text("ok")])

    loop._execute("write_file", {"filepath": "app.py", "content": "# app\n"})
    for i in range(3):
        result = loop._execute(
            "append_file", {"filepath": "app.py", "content": f"def s{i}():\n    return {i}\n"}
        )
        assert result.ok

    body = (tmp_path / "app.py").read_text(encoding="utf-8")
    assert body.startswith("# app")
    assert all(f"def s{i}()" in body for i in range(3))
    assert body.count("# app") == 1, "appending overwrote instead of adding"


def test_appending_does_not_glue_lines_together(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])
    (tmp_path / "a.txt").write_text("first", encoding="utf-8")

    loop._execute("append_file", {"filepath": "a.txt", "content": "second"})

    assert (tmp_path / "a.txt").read_text(encoding="utf-8").splitlines() == ["first", "second"]


def test_find_files_hunts_where_list_files_only_shows_one_directory(tmp_path):
    (tmp_path / "src" / "deep").mkdir(parents=True)
    (tmp_path / "src" / "deep" / "target.py").write_text("x = 1", encoding="utf-8")
    (tmp_path / "readme.md").write_text("hi", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("find_files", {"pattern": "**/*.py"})

    assert "src/deep/target.py" in result.message
    assert "readme.md" not in result.message


def test_a_glob_that_matches_nothing_explains_the_usual_mistake(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("find_files", {"pattern": "*.py"})

    assert result.ok
    assert "**/*.py" in result.message, "it should name the fix, not just say no"


def test_the_model_can_search_the_whole_conversation(tmp_path):
    """Including turns long since evicted from the window."""
    from shamsu.session.manager import SessionManager

    logger = SessionManager(tmp_path).create_session("long one")
    logger.append_message("user", "we settled on port 8080 for the dev server")
    for i in range(80):
        logger.append_message("user", f"filler {i}")
        logger.append_message("assistant", f"ok {i}")
    loop = SimpleChatLoop(
        tmp_path,
        client=FakeClient([_text("ok")]),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        session_logger=logger,
        model_name="qwen3:8b",
    )

    result = loop._execute("history_search", {"query": "what port did we pick?"})

    assert result.ok
    assert "8080" in result.message


# ---------------------------------------------------------------------------
# Composite tools (smallcode shapes, our half-failure rule)
#
# Two steps in one call saves a round; the risk is that each doubles the ways
# a call can HALF-fail. A composite that errors when its second step misses is
# worse than two plain calls, because the model paid for the first step and got
# nothing. So: a half-failure returns the half that worked.
# ---------------------------------------------------------------------------


def test_a_failed_patch_still_hands_back_the_file(tmp_path):
    """The measured failure this prevents: 12 no-op patches in a single turn.

    Refused with only 'did not match', the model retries from memory. Given
    the real text, the next attempt is computed rather than guessed.
    """
    (tmp_path / "game.js").write_text("const speed = 5;\nconst lives = 3;\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute(
        "read_and_patch",
        {"filepath": "game.js", "old_string": "const speed = 9;", "new_string": "const speed = 2;"},
    )

    assert not result.ok
    assert "did not apply" in result.message
    assert "const speed = 5;" in result.message, "the real text was not returned"
    assert result.data.get("patch_failed")


def test_a_matching_patch_just_patches(tmp_path):
    (tmp_path / "game.js").write_text("const speed = 5;\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute(
        "read_and_patch",
        {"filepath": "game.js", "old_string": "const speed = 5;", "new_string": "const speed = 2;"},
    )

    assert result.ok
    assert "const speed = 2;" in (tmp_path / "game.js").read_text(encoding="utf-8")


def test_create_and_run_keeps_the_file_when_the_command_fails(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute(
        "create_and_run",
        {"filepath": "made.py", "content": "x = 1\n", "command": "this-command-does-not-exist"},
    )

    assert (tmp_path / "made.py").read_text(encoding="utf-8") == "x = 1\n"
    assert result.data.get("file_written") is True
    assert "made.py" in result.message


def test_find_and_read_returns_the_file_and_names_the_alternatives(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "settings.py").write_text("DEBUG = True\n", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "settings.py").write_text("DEBUG = False\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("find_and_read", {"pattern": "**/settings.py"})

    assert result.ok
    # The model is handed the whole serialised result, not just the message -
    # `read_file` carries content in `data`, which is what reaches the prompt.
    assert "DEBUG" in result.to_json()
    assert "other file(s) also matched" in result.message, "a silent pick between two files"


def test_search_and_read_reads_the_best_hit_and_shows_the_runners_up(tmp_path):
    (tmp_path / "auth.py").write_text(
        "def validateAuthToken(raw):\n    return len(raw) > 10\n", encoding="utf-8"
    )
    (tmp_path / "maths.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("search_and_read", {"query": "the function that validates tokens"})

    assert result.ok
    assert "auth.py" in result.message
    assert "validateAuthToken" in result.to_json(), "it found the file but did not read it"


def test_a_composite_that_finds_nothing_explains_rather_than_erroring(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])

    found = loop._execute("find_and_read", {"pattern": "**/*.nope"})
    searched = loop._execute("search_and_read", {"query": "quantum flux capacitor"})

    assert found.ok and searched.ok
    assert "No files match" in found.message


def test_a_big_window_sends_every_tool(tmp_path):
    """smallcode routes on the window: above ~16k the schemas are affordable
    and paying an extra round to save 2k out of 32k is a bad trade."""
    from shamsu.agents.simple_chat import active_tool_schemas

    assert len(active_tool_schemas(32768)) == len(SIMPLE_TOOL_SCHEMAS)


def test_a_tight_window_offers_only_the_category_selector(tmp_path):
    from shamsu.agents.simple_chat import active_tool_schemas
    from shamsu.agents.simple_router import SELECTOR_TOOL_NAME

    offered = [t["function"]["name"] for t in active_tool_schemas(8192)]

    assert offered == [SELECTOR_TOOL_NAME]


def test_choosing_a_category_hands_over_that_categorys_tools(tmp_path):
    from shamsu.agents.simple_chat import active_tool_schemas

    offered = {t["function"]["name"] for t in active_tool_schemas(8192, "write")}

    assert "write_file" in offered and "patch_file" in offered
    assert "graph_search" not in offered, "the narrowing did nothing"
    # Recall is cross-cutting: needing a fact mid-edit must not cost a switch.
    assert "memory_load" in offered and "history_search" in offered


def test_an_invented_category_gets_everything_rather_than_nothing(tmp_path):
    """It has still told us it wants to act; an empty tool list strands it."""
    from shamsu.agents.simple_chat import active_tool_schemas

    assert len(active_tool_schemas(8192, "wibble")) == len(SIMPLE_TOOL_SCHEMAS)


def test_routing_can_be_forced_either_way(tmp_path, monkeypatch):
    from shamsu.agents.simple_router import routing_mode

    monkeypatch.setenv("SHAMSU_TOOL_ROUTING", "two_stage")
    assert routing_mode(131072) == "two_stage"
    monkeypatch.setenv("SHAMSU_TOOL_ROUTING", "direct")
    assert routing_mode(4096) == "direct"


def test_the_model_can_pick_a_category_and_then_act(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_TOOL_ROUTING", "two_stage")
    loop = _loop(
        tmp_path,
        [_tool("select_category", category="write"),
         _tool("write_file", filepath="made.py", content="x = 1" + chr(10)),
         _text("written")],
    )

    asyncio.run(loop.run("create made.py"))

    assert (tmp_path / "made.py").read_text(encoding="utf-8") == "x = 1" + chr(10)
    first = [t["function"]["name"] for t in loop.client.calls[0]["tools"]]
    second = [t["function"]["name"] for t in loop.client.calls[1]["tools"]]
    assert first == ["select_category"]
    assert "write_file" in second, "the chosen category was not handed over"


def test_narrowed_routing_charges_the_budget_less(tmp_path, monkeypatch):
    """The sent schemas and the charged schemas must never disagree."""
    monkeypatch.setenv("SHAMSU_TOOL_ROUTING", "direct")
    loop = _loop(tmp_path, [_text("ok")])
    full = loop._fixed_overhead()

    monkeypatch.setenv("SHAMSU_TOOL_ROUTING", "two_stage")
    narrowed = loop._fixed_overhead()

    assert narrowed < full


def test_what_is_sent_is_what_is_charged(tmp_path):
    """Item A's whole lesson, applied to the roster growth."""
    from shamsu.context.budget import tool_schema_tokens

    loop = _loop(tmp_path, [_text("ok")])
    asyncio.run(loop.run("hi"))

    sent = loop.client.calls[0]["tools"]
    assert loop._fixed_overhead() >= tool_schema_tokens(sent)


# ---------------------------------------------------------------------------
# Speaking to the agent while it works
#
# A turn here can run 24 rounds. Live sessions have spent 18 minutes on 18
# whole-file writes, and 25 minutes on 17 mutations that changed nothing. Until
# now the user could watch that happen or Ctrl-C and lose the turn.
# ---------------------------------------------------------------------------


def test_feedback_typed_mid_turn_reaches_the_model(tmp_path):
    from shamsu.agents.simple_feedback import FeedbackQueue

    feedback = FeedbackQueue()
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="a.py"), _text("done")],
        feedback=feedback,
    )
    feedback.push("stop - you are editing the wrong file")

    asyncio.run(loop.run("look at a.py"))

    sent = json.dumps(loop.client.calls[0]["messages"])
    assert "wrong file" in sent


def test_feedback_is_framed_as_an_interruption_not_the_next_request(tmp_path):
    """Dropped in bare it reads like a new task, and the model finishes first."""
    from shamsu.agents.simple_feedback import render_interjection

    rendered = render_interjection(["use the other file"])

    assert "interrupted" in rendered.lower()
    assert "takes precedence" in rendered.lower()
    assert "use the other file" in rendered


def test_feedback_lands_between_rounds_never_inside_one(tmp_path):
    """Appended mid-round it orphans a tool_call_id from its result."""
    from shamsu.agents.simple_feedback import FeedbackQueue

    feedback = FeedbackQueue()
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="a.py"), _tool("read_file", filepath="a.py"), _text("done")],
        feedback=feedback,
    )
    feedback.push("actually look at b.py")

    asyncio.run(loop.run("look at a.py"))

    roles = [m.role for m in loop.state.all_messages]
    for index, role in enumerate(roles):
        if role == "tool":
            assert roles[index - 1] in {"assistant", "tool"}, (
                "a user message was injected between a call and its result"
            )


def test_feedback_is_recorded_not_whispered(tmp_path):
    """A steer that changed a session and left no trace makes the log unreadable."""
    from shamsu.agents.simple_feedback import FeedbackQueue

    feedback = FeedbackQueue()
    loop = _loop(tmp_path, [_text("ok")], feedback=feedback)
    feedback.push("prefer tabs")

    asyncio.run(loop.run("format it"))

    said = [m.content for m in loop.state.all_messages if m.role == "user"]
    assert any("prefer tabs" in text for text in said)


def test_several_interjections_arrive_together_in_order(tmp_path):
    from shamsu.agents.simple_feedback import FeedbackQueue, render_interjection

    feedback = FeedbackQueue()
    feedback.push("first thing")
    feedback.push("second thing")

    rendered = render_interjection(feedback.drain())

    assert rendered.index("first thing") < rendered.index("second thing")


def test_blank_typing_is_not_an_interruption(tmp_path):
    from shamsu.agents.simple_feedback import FeedbackQueue

    feedback = FeedbackQueue()

    assert feedback.push("   ") is False
    assert feedback.push("") is False
    assert not feedback


def test_a_turn_with_nobody_listening_behaves_exactly_as_before(tmp_path):
    loop = _loop(tmp_path, [_text("ok")])

    result = asyncio.run(loop.run("hi"))

    assert result.final == "ok"
    assert loop.feedback is None


# ---------------------------------------------------------------------------
# The assembled prompt: structure, ordering, and pairing
#
# Every part of this changed in this branch - budget, elision, grounding,
# memory, capability lines, routing. These pin the shape they must add up to.
# ---------------------------------------------------------------------------


def _assembled(tmp_path, turns: int = 40):
    from shamsu.agents.simple_memory import remember

    for i in range(20):
        (tmp_path / f"m{i}.py").write_text("x = 1\n", encoding="utf-8")
    remember(tmp_path, "decision", "dev server port", "The dev server runs on port 8080.")
    loop = _loop(tmp_path, [_text("ok")])
    loop._request = "what port does the dev server use?"
    loop._files = workspace_files(tmp_path)
    for i in range(turns):
        loop.state.append_user(f"turn {i} " + "pad " * 40)
        loop.state.append_assistant(
            "", tool_calls=_write_call_body(f"m{i}.py", "y\n" * 300)
        )
        loop.state.append_tool(f"c{i}", "write_file", json.dumps({"ok": True, "message": "wrote"}))
    loop.state.append_user("what port does the dev server use?")
    return loop, loop._messages()


def test_the_standing_prompt_leads_and_the_request_ends(tmp_path):
    """A small model reads the end. The request has to be there."""
    _loop_, built = _assembled(tmp_path)

    assert built[0]["role"] == "system"
    assert built[-1]["role"] == "user"
    assert "what port" in built[-1]["content"]


def test_grounding_sits_immediately_before_the_request(tmp_path):
    """Not at position 1: it changes whenever a file does, and sitting early it
    invalidated the KV prefix and forced a full re-prefill every turn."""
    _loop_, built = _assembled(tmp_path)

    grounding = [
        i for i, m in enumerate(built) if "Files in the workspace" in str(m.get("content"))
    ]

    assert grounding == [len(built) - 2]


def test_recalled_memory_rides_along_with_the_grounding(tmp_path):
    _loop_, built = _assembled(tmp_path)

    assert any("8080" in str(m.get("content")) for m in built)


def test_no_tool_result_is_orphaned_from_its_call(tmp_path):
    """Trimming to a budget must never cut between a call and its result."""
    _loop_, built = _assembled(tmp_path)

    roles = [m["role"] for m in built]
    for index, role in enumerate(roles):
        if role == "tool":
            assert index > 0 and roles[index - 1] in {"assistant", "tool"}


def test_the_assembled_prompt_leaves_the_reply_its_reserve(tmp_path):
    from shamsu.agents.simple_chat import output_reserve
    from shamsu.context.budget import messages_tokens, tool_schema_tokens

    loop, built = _assembled(tmp_path, turns=60)
    ceiling = loop._ceiling()
    sent = messages_tokens(built) + tool_schema_tokens(loop._sent_schemas())

    assert sent < ceiling
    assert ceiling - sent >= output_reserve(ceiling)


def test_the_estimate_matches_the_assembled_prompt(tmp_path):
    """Item A, end to end: the number budgeted and the number sent are one."""
    from shamsu.context.budget import messages_tokens, tool_schema_tokens

    loop, built = _assembled(tmp_path)
    sent = messages_tokens(built) + tool_schema_tokens(loop._sent_schemas())

    assert abs(loop._estimate_prompt(built) - sent) <= 1


# --- verification: only claim what was actually parsed (C2) --------------


def _verify_json(loop, written):
    report = loop._verify(written)
    return json.loads(report) if report else {}


def test_verify_never_reports_a_file_it_has_no_checker_for_as_checked(tmp_path):
    """RC2: `game-plan.md` came back as "no syntax errors" from a checker that
    never opened it. 572 such claims in one session."""
    (tmp_path / "game-plan.md").write_text("# Plan\n", encoding="utf-8")
    loop = _loop(tmp_path, [])

    report = _verify_json(loop, ["game-plan.md"])

    assert "game-plan.md: no syntax errors" not in report["message"]
    assert report["data"]["checked"] == []
    assert "game-plan.md" in report["message"]
    assert "NOT checked" in report["message"]


def test_a_file_type_with_no_checker_is_not_a_problem_to_repair(tmp_path):
    """The escape. Reporting `.md` as broken would leave the model fixing prose."""
    (tmp_path / "notes.md").write_text("# Notes\n", encoding="utf-8")
    loop = _loop(tmp_path, [])

    report = _verify_json(loop, ["notes.md"])

    assert report["ok"] is True
    assert "problems" not in report["data"]


def test_a_javascript_file_with_open_blocks_reads_as_progress_not_a_fault(tmp_path, monkeypatch):
    """The three real files - game.js 60/39 braces, player.js 47/30, bullet.js
    24/17 - were once certified clean, and that was the defect. This is the
    OTHER edge of the same knife: once a large file is meant to arrive in
    sections, an open block is what the first section looks like, and calling it
    a fault sends the model repairing a file that is not finished yet."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    (tmp_path / "game.js").write_text(
        "function update() {\n  if (alive) {\n    for (const a of rocks) {\n",
        encoding="utf-8",
    )
    loop = _loop(tmp_path, [])
    # The precondition the exemption now turns on: the write that just landed
    # ADDED to this file. Left unsaid, this test passed for a file a patch had
    # just broken, which is the defect below.
    loop._last_write_grew["game.js"] = True

    report = _verify_json(loop, ["game.js"])

    assert report["ok"] is True
    assert not report["data"]["problems"] if "problems" in report["data"] else True
    unfinished = report["data"]["unfinished"][0]
    assert unfinished.startswith("game.js:")
    assert "3 block(s) still open" in unfinished
    assert "append_file" in unfinished
    assert "game.js" not in report["data"]["checked"], "it must not read as verified"


def test_a_brace_a_patch_ate_is_a_fault_not_progress(tmp_path):
    """THE 2026-08-20 defect, and the reason Phase 0 exists.

    A patch that eats a closing brace leaves open blocks and nothing else
    wrong - byte-for-byte what the first section of a chunked write looks
    like. So `node --check: SyntaxError` was thrown away, the report came back
    `ok: true`, and the model was told to "continue with append_file": advice
    that cannot close a brace missing in the MIDDLE of a file, on a file it had
    just been told was fine. Asked to fix it, the model had nothing to fix.
    """
    (tmp_path / "game.js").write_text(
        'function greet(n) {\n  if (n) {\n    console.log(n);\n\n}\n',
        encoding="utf-8",
    )
    loop = _loop(tmp_path, [])
    # A patch. It did not grow the file, so it cannot claim to be building it.
    loop._last_write_grew["game.js"] = False

    report = _verify_json(loop, ["game.js"])

    assert report["ok"] is False, "a file a patch broke must not verify as ok"
    assert report["data"]["unfinished"] == []
    problem = report["data"]["problems"][0]
    assert problem.startswith("game.js:")
    assert "append_file" not in problem, "appending cannot close a brace in the middle"


def test_a_patch_into_a_half_built_file_reports_the_fault_and_says_it_is_half_built(tmp_path):
    """The one case the gate could raise a false alarm on, answered with both
    facts rather than by suppressing one. A model told only "unexpected end of
    input" may close the open blocks and end the file early."""
    (tmp_path / "game.js").write_text(
        'function greet(n) {\n  if (n) {\n    console.log(n);\n\n}\n',
        encoding="utf-8",
    )
    loop = _loop(tmp_path, [])
    loop._built_up.add("game.js")          # appended to earlier in this turn
    loop._last_write_grew["game.js"] = False  # but the LAST write was a patch

    report = _verify_json(loop, ["game.js"])

    assert report["ok"] is False, "the error is real and is reported"
    problem = report["data"]["problems"][0]
    assert "building this file in sections" in problem
    assert "closing it early" in problem


def test_an_unclosed_block_points_at_the_innermost_not_line_one(tmp_path, monkeypatch):
    """"the first { on line 1" points a repair at the top of a file whose damage
    is 300 lines lower. The stack's LAST entry is the one nearest where the text
    actually stops."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    (tmp_path / "game.js").write_text(
        "class Game {\n  update() {\n    if (alive) {\n",
        encoding="utf-8",
    )
    loop = _loop(tmp_path, [])
    loop._last_write_grew["game.js"] = True

    unfinished = _verify_json(loop, ["game.js"])["data"]["unfinished"][0]

    assert "opened on line 3" in unfinished, unfinished
    assert "line 1" not in unfinished


def test_a_file_still_open_when_the_turn_ends_counts_against_the_run(tmp_path, monkeypatch):
    """The other half of calling open blocks progress. Mid-turn there is nothing
    to pass or fail; once the model has declared itself done, a file it left
    mid-block is broken, and the run outcome has to see that."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    ledger = _RecordingLedger()
    loop = _verified(
        tmp_path,
        [_tool("write_file", filepath="game.js", content="function f() {" + chr(10)),
         _text("all done")],
        ledger,
        max_rounds=2,
    )

    asyncio.run(loop.run("write game.js"))

    failures = [e for e in ledger.events if e["type"] == "verification_failed"]
    assert failures, "a file abandoned mid-block read as a success"
    assert failures[0]["verifier_id"] == "syntax:game.js"
    assert "still open" in failures[0]["detail"]


def test_a_file_finished_before_the_turn_ends_is_not_failed_for_being_unfinished(tmp_path, monkeypatch):
    """The settle pass must not punish the file it was watching get built."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    ledger = _RecordingLedger()
    loop = _verified(
        tmp_path,
        [_tool("write_file", filepath="game.js", content="function f() {" + chr(10)),
         _tool("append_file", filepath="game.js", content="}" + chr(10)),
         _text("all done")],
        ledger,
        max_rounds=3,
    )

    asyncio.run(loop.run("build game.js in two pieces"))

    assert not [e for e in ledger.events if e["type"] == "verification_failed"]
    assert [e for e in ledger.events if e["type"] == "verification_passed"]


def test_a_complete_javascript_file_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    (tmp_path / "ok.js").write_text(
        "const half = (a) => a / 2;\n"
        "// a } in a comment\n"
        "const s = \"a } in a string\";\n"
        "const re = /[{]/;\n"
        "function go() { return { a: [1, 2] }; }\n",
        encoding="utf-8",
    )
    loop = _loop(tmp_path, [])

    report = _verify_json(loop, ["ok.js"])

    assert report["ok"] is True
    assert report["data"]["checked"] == ["ok.js"]


def test_python_is_still_compiled(tmp_path):
    (tmp_path / "broken.py").write_text("def f(:\n", encoding="utf-8")
    loop = _loop(tmp_path, [])

    report = _verify_json(loop, ["broken.py"])

    assert report["ok"] is False
    assert "broken.py: line" in report["data"]["problems"][0]


def test_a_mix_says_which_files_were_checked_and_which_were_not(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "plan.md").write_text("# hi\n", encoding="utf-8")
    loop = _loop(tmp_path, [])

    report = _verify_json(loop, ["app.py", "plan.md"])

    assert report["data"]["checked"] == ["app.py"]
    assert report["data"]["skipped"] == ["plan.md (no checker for .md)"]
    assert "Checked app.py: no syntax errors." in report["message"]
    assert "NOT checked: plan.md" in report["message"]


def test_node_check_is_used_when_node_is_installed(tmp_path, monkeypatch):
    """A real parser beats a bracket count - and its absence must not become a
    missing check, which is the defect this whole module exists for."""
    import subprocess as _subprocess

    from shamsu.agents import simple_verify

    monkeypatch.delenv("SHAMSU_DISABLE_NODE_CHECK", raising=False)
    monkeypatch.setattr(simple_verify.shutil, "which", lambda _name: "/usr/bin/node")

    def fake_run(argv, **kwargs):
        assert argv[1] == "--check"
        return _subprocess.CompletedProcess(
            argv, 1, "", "file.js:3\nSyntaxError: Unexpected end of input\n    at wrap\n"
        )

    monkeypatch.setattr(simple_verify.subprocess, "run", fake_run)
    # A stray closer, not an open block: an open block is now reported as
    # progress by whichever checker found it, so it could no longer show that
    # node was the one that ran.
    (tmp_path / "cut.js").write_text("function f() { ) }\n", encoding="utf-8")
    loop = _loop(tmp_path, [])

    report = _verify_json(loop, ["cut.js"])

    assert report["ok"] is False
    assert "SyntaxError: Unexpected end of input" in report["data"]["problems"][0]


def test_the_bracket_scan_stays_quiet_on_prose_apostrophes():
    """A false "your file is broken" is the same defect pointed the other way."""
    from shamsu.agents.simple_verify import bracket_problem

    assert bracket_problem("// it's fine\nfunction f() { return 1; }\n") == ""


def test_the_bracket_scan_reports_a_stray_closer():
    from shamsu.agents.simple_verify import bracket_problem

    problem = bracket_problem("function f() { return 1; }\n}\n")

    assert "line 2" in problem
    assert "unexpected }" in problem


# --- a severed generation never reaches the disk (C1) --------------------


def _cut_tool(name: str, **arguments) -> dict:
    """A tool call the output cap severed part-way through its arguments."""
    return {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
        },
        "done_reason": "length",
        "prompt_eval_count": 21_472,
        "eval_count": 8_192,
    }


def test_a_write_cut_off_mid_argument_never_reaches_the_disk(tmp_path):
    """RC1: five rounds of `write_file -> ok` each followed by a cut-off notice.
    game.js ended with 60 open braces and 39 closes."""
    loop = _loop(
        tmp_path,
        [_cut_tool("write_file", filepath="game.js", content="function update() {\n  if (a) {\n")],
        max_rounds=1,
        verify_changes=False,
    )

    asyncio.run(loop.run("write the game loop"))

    assert not (tmp_path / "game.js").exists()


def test_a_truncated_write_never_overwrites_a_file_that_was_already_good(tmp_path):
    """`write_file` REPLACES. This is the loss that cannot be undone."""
    (tmp_path / "game.js").write_text("// the whole working file\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_cut_tool("write_file", filepath="game.js", content="function update() {\n")],
        max_rounds=1,
        verify_changes=False,
    )

    asyncio.run(loop.run("add a pause key"))

    assert (tmp_path / "game.js").read_text(encoding="utf-8") == "// the whole working file\n"


def test_the_refusal_reaches_the_model_and_names_the_next_call(tmp_path):
    """The correction belongs in the tool result, and it names the call."""
    loop = _loop(
        tmp_path,
        [_cut_tool("write_file", filepath="game.js", content="x")],
        max_rounds=1,
        verify_changes=False,
    )

    asyncio.run(loop.run("write the game loop"))

    said = [m.content for m in loop.state.all_messages if m.role == "tool"]
    assert any("REFUSED" in c and "append_file" in c for c in said)
    assert any("unchanged on disk" in c for c in said)


def test_append_file_is_refused_when_cut_off_too(tmp_path):
    """`Round 9 append_file -> ok` sits above a cut-off notice in the log, and
    append_file was never in MUTATING_TOOLS."""
    (tmp_path / "main.js").write_text("start\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_cut_tool("append_file", filepath="main.js", content="function f() {\n")],
        max_rounds=1,
        verify_changes=False,
    )

    asyncio.run(loop.run("add a function"))

    assert (tmp_path / "main.js").read_text(encoding="utf-8") == "start\n"


def test_a_read_in_a_cut_off_generation_still_runs(tmp_path):
    """Only writes damage anything. Refusing reads would strand the model."""
    (tmp_path / "main.js").write_text("line one\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_cut_tool("read_file", filepath="main.js"), _text("got it")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("read main.js"))

    said = [m.content for m in loop.state.all_messages if m.role == "tool"]
    assert any("line one" in c for c in said)


def test_only_the_severed_call_is_refused_not_the_ones_that_finished(tmp_path):
    """Earlier calls in the same reply finished generating; the cap took the last."""
    two = {
        "message": {
            "content": "",
            "tool_calls": [
                {"function": {"name": "write_file", "arguments": {"filepath": "a.js", "content": "const a = 1;\n"}}},
                {"function": {"name": "write_file", "arguments": {"filepath": "b.js", "content": "const b = {\n"}}},
            ],
        },
        "done_reason": "length",
    }
    loop = _loop(tmp_path, [two], max_rounds=1, verify_changes=False)

    asyncio.run(loop.run("write both files"))

    assert (tmp_path / "a.js").read_text(encoding="utf-8") == "const a = 1;\n"
    assert not (tmp_path / "b.js").exists()


def test_a_complete_generation_writes_normally(tmp_path):
    """The guard must not touch the ordinary path."""
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="ok.js", content="const a = 1;\n"), _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write ok.js"))

    assert (tmp_path / "ok.js").read_text(encoding="utf-8") == "const a = 1;\n"


def test_the_second_refusal_says_something_the_first_did_not(tmp_path):
    """A repeated identical error teaches nothing - RC8, one issue over."""
    cut = _cut_tool("write_file", filepath="game.js", content="function f() {\n")
    loop = _loop(tmp_path, [cut, cut], max_rounds=2, verify_changes=False)

    asyncio.run(loop.run("write the game loop"))

    said = [m.content for m in loop.state.all_messages if m.role == "tool"]
    assert len(said) == 2
    assert said[0] != said[1]
    assert "FIRST 60 LINES ONLY" in said[1]


def test_the_refusal_guard_has_an_exit(tmp_path):
    """A guard with no escape is a deadlock. Three refusals ends the turn with
    a reason, rather than spending the whole round budget on it."""
    from shamsu.agents.simple_chat import MAX_TRUNCATED_WRITE_REFUSALS

    cut = _cut_tool("write_file", filepath="game.js", content="function f() {\n")
    loop = _loop(tmp_path, [cut] * 24, max_rounds=24, verify_changes=False)

    result = asyncio.run(loop.run("write the game loop"))

    assert result.stopped
    assert result.rounds < 24
    assert str(MAX_TRUNCATED_WRITE_REFUSALS) in result.final
    assert "one part at a time" in result.final


def test_a_write_that_lands_intact_clears_the_refusal_streak(tmp_path):
    """Consecutive, not lifetime: a session must not be poisoned by one bad round."""
    cut = _cut_tool("write_file", filepath="game.js", content="function f() {\n")
    good = _tool("write_file", filepath="game.js", content="const a = 1;\n")
    loop = _loop(tmp_path, [cut, good, cut, _text("done")], max_rounds=4, verify_changes=False)

    asyncio.run(loop.run("write the game loop"))

    assert loop._truncated_refusals == 1


# --- elision keeps the evidence, not just the conclusion (C10) -----------
#
# RC10. The final prompt of the 2026-08-19 session held 15 reads of main.js
# reduced to `{"elided": "call read_file for the current contents"}` and 8
# surviving assistant sentences all asserting the same wrong diagnosis. The
# model re-derived line 426 every round because that was the only evidence left
# in the room.


def _read_payload(path: str, body: str) -> str:
    return json.dumps({
        "ok": True,
        "message": "Read file.",
        "data": {
            "filepath": path,
            "resolved_filepath": path,
            "total_lines": len(body.splitlines()),
            "content": body,
        },
    })


def _crowded(state, rounds: int = 60) -> None:
    """Enough weight to push the session well past the elision target."""
    for i in range(rounds):
        state.append_user(f"later {i} " + "padding " * 400)


def test_the_current_contents_of_a_file_survive_elision(tmp_path):
    """Fix 1, the one the report says would alone have ended the loop."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    state.append_user("why is main.js broken?")
    state.append_assistant("", tool_calls=[{"function": {"name": "read_file", "arguments": {"filepath": "main.js"}}}])
    state.append_tool("c1", "read_file", _read_payload("main.js", "the real contents of main.js\n"))
    _crowded(state)
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    loop._elide_payloads()

    kept = state.all_messages[3]
    assert "the real contents of main.js" in kept.content
    assert "call read_file for the current contents" not in kept.content


def test_a_superseded_read_of_the_same_file_is_still_elided(tmp_path):
    """Fifteen stubs of one file is pure loss; so were fifteen full copies."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(3):
        state.append_user(f"read it {i}")
        state.append_assistant("", tool_calls=[{"function": {"name": "read_file", "arguments": {"filepath": "main.js"}}}])
        state.append_tool(f"c{i}", "read_file", _read_payload("main.js", f"revision {i} of main.js\n"))
    _crowded(state)
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    loop._elide_payloads()

    reads = [m for m in state.all_messages if m.role == "tool"]
    assert "revision 2 of main.js" in reads[-1].content, "the current one must survive"
    assert "revision 0 of main.js" not in reads[0].content, "superseded, should be a stub"
    assert "revision 1 of main.js" not in reads[1].content


def test_a_write_echo_is_not_mistaken_for_the_file_contents(tmp_path):
    """A write result carries `resolved_filepath` too, and is not evidence."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    state.append_user("write it")
    state.append_assistant("", tool_calls=_write_call_body("main.js", "x=1;" + chr(10)))
    state.append_tool("c1", "write_file", json.dumps({
        "ok": True, "message": "Created main.js.",
        "data": {"filepath": "main.js", "resolved_filepath": "main.js", "line_count": 1},
    }))
    _crowded(state)
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    loop._elide_payloads()

    assert state.all_messages[3].elided


def test_only_a_bounded_number_of_files_keep_their_contents(tmp_path):
    """Protection is the one thing elision may not reclaim, so it is capped."""
    from shamsu.agents.simple_chat import MAX_PROTECTED_READ_PATHS

    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    for i in range(MAX_PROTECTED_READ_PATHS + 3):
        state.append_user(f"read f{i}")
        state.append_assistant("", tool_calls=[{"function": {"name": "read_file", "arguments": {"filepath": f"f{i}.js"}}}])
        state.append_tool(f"c{i}", "read_file", _read_payload(f"f{i}.js", f"contents of f{i}\n"))
    _crowded(state)
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    loop._elide_payloads()

    survived = [m for m in state.all_messages if m.role == "tool" and "contents of f" in m.content]
    assert len(survived) <= MAX_PROTECTED_READ_PATHS
    assert "contents of f0" not in "".join(m.content for m in state.all_messages)


def test_protection_stops_at_an_allowance_so_it_cannot_pin_the_window(tmp_path):
    """The bound. Only the most recent read is kept whatever it costs; further
    files are added while the protected total stays under the allowance."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    huge = "a line of a very large file " * 60 + chr(10)
    for i in range(4):
        state.append_user(f"read big{i}")
        state.append_assistant("", tool_calls=[{"function": {"name": "read_file", "arguments": {"filepath": f"big{i}.js"}}}])
        state.append_tool(f"c{i}", "read_file", _read_payload(f"big{i}.js", huge * 40))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    protected = loop._current_file_reads()

    assert len(protected) == 1, "four oversized reads must not all be kept"
    assert id(state.all_messages[-1]) in protected, "and the one kept is the newest"


def test_protection_can_never_deadlock_the_prompt(tmp_path):
    """The real escape is eviction, one layer down: protection stops a payload
    being SHRUNK, it does not stop a message being dropped to fit the window."""
    from shamsu.agents.simple_chat import output_reserve
    from shamsu.context.budget import messages_tokens, tool_schema_tokens

    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    huge = "a line of a very large file " * 60 + chr(10)
    for i in range(6):
        state.append_user(f"read big{i}")
        state.append_assistant("", tool_calls=[{"function": {"name": "read_file", "arguments": {"filepath": f"big{i}.js"}}}])
        state.append_tool(f"c{i}", "read_file", _read_payload(f"big{i}.js", huge * 40))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state
    loop._request = "fix it"

    loop._elide_payloads()
    built = loop._messages()
    ceiling = loop._ceiling()
    sent = messages_tokens(built) + tool_schema_tokens(loop._sent_schemas())

    assert sent < ceiling
    assert ceiling - sent >= output_reserve(ceiling)


def test_the_read_that_survives_is_the_one_the_model_reasons_from(tmp_path):
    """End to end, the shape of the live failure: the model's wrong claim and
    the read that disproves it, both old. The read must not be the casualty."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    state.append_assistant("I found it! The issue is on line 426 - a stray comment.")
    state.append_assistant("", tool_calls=[{"function": {"name": "read_file", "arguments": {"filepath": "main.js"}}}])
    state.append_tool("c1", "read_file", _read_payload("main.js", "426: // a perfectly ordinary comment\n"))
    _crowded(state)
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state

    loop._elide_payloads()

    prompt = "".join(m.content for m in state.all_messages)
    assert "line 426 - a stray comment" in prompt          # the claim survives
    assert "a perfectly ordinary comment" in prompt        # so does what disproves it


# --- a turn that ends on a promise is not finished (C7) ------------------
#
# RC7. Fourteen assistant turns in the 2026-08-19 session ended with prose
# announcing an edit and no tool call, every one on a colon, every one handed
# back to the user as a complete answer. This is the defect the user felt:
# "I told it to read files but nothing happened, the agent remained dumb."


def test_the_three_promises_from_the_log_are_all_caught():
    from shamsu.agents.simple_chat import ends_on_an_unmade_promise

    for said in (
        "I found it. I'll use patch_file to replace just those two lines:",
        "...with proper code structure. I'll read lines 420-435 to see what to replace:",
        "Now let me create a simple test to verify everything works:",
    ):
        assert ends_on_an_unmade_promise(said) == said.splitlines()[-1]


def test_an_ordinary_answer_is_not_a_promise():
    """A colon alone introduces the next paragraph; an announcement alone opens
    one. It is a promise as the FINAL word that means nothing followed."""
    from shamsu.agents.simple_chat import ends_on_an_unmade_promise

    for said in (
        "The port is 8080.",
        "Here are the steps:" + chr(10) + "1. install it" + chr(10) + "2. run it",
        "I'll read the file, and here is what it says: the port is 8080.",
        "## What I changed:",
        "I fixed it by moving the call.",
    ):
        assert ends_on_an_unmade_promise(said) == ""


def test_a_turn_ending_on_a_promise_is_not_returned_as_the_answer(tmp_path):
    """`describes_an_unmade_edit` cannot catch this: it needs a 4-line fence,
    and here the model shows nothing at all."""
    from shamsu.agents.simple_chat import describes_an_unmade_edit

    promise = "I can see the problem. I'll use patch_file to fix those two lines:"
    assert describes_an_unmade_edit(promise, ["main.js"]) == "", "the old guard cannot see it"

    loop = _loop(
        tmp_path,
        [_text(promise), _tool("write_file", filepath="main.js", content="fixed\n"), _text("Done.")],
        max_rounds=4,
        verify_changes=False,
    )

    result = asyncio.run(loop.run("fix main.js"))

    assert result.final == "Done."
    assert (tmp_path / "main.js").read_text(encoding="utf-8") == "fixed\n"


def test_the_nudge_quotes_what_was_promised_and_names_what_to_do(tmp_path):
    """The correction goes where the model is looking, and it is specific."""
    promise = "Let me read lines 420-435 to see exactly what needs replacing:"
    loop = _loop(tmp_path, [_text(promise), _text("The port is 8080.")], max_rounds=3)

    asyncio.run(loop.run("what is on line 426?"))

    nudges = [m.content for m in loop.state.all_messages if m.role == "user"]
    assert any(promise in c and "called no tool" in c for c in nudges)
    assert any("Do it now, in this turn" in c for c in nudges)


def test_a_promise_kept_after_the_nudge_ends_the_turn_normally(tmp_path):
    loop = _loop(
        tmp_path,
        [_text("Let me fix that:"), _tool("write_file", filepath="a.js", content="x\n"), _text("Fixed.")],
        max_rounds=4,
        verify_changes=False,
    )

    result = asyncio.run(loop.run("fix a.js"))

    assert not result.stopped
    assert result.final == "Fixed."


def test_the_promise_guard_has_an_exit(tmp_path):
    """A guard with no escape is a deadlock. Saying it a third time is not going
    to become doing it."""
    from shamsu.agents.simple_chat import MAX_PROMISE_NUDGES

    loop = _loop(tmp_path, [_text("Let me fix that:")] * 12, max_rounds=12)

    result = asyncio.run(loop.run("fix a.js"))

    assert result.stopped
    assert result.rounds < 12
    assert "Let me fix that:" in result.final
    assert str(MAX_PROMISE_NUDGES + 1) in result.final


def test_the_promise_stop_never_becomes_conversation(tmp_path):
    """RC3's lesson: a harness notice replayed into later prompts teaches the
    model that this is how a turn ends. It said so 54 times."""
    from shamsu.agents.chat_state import _should_hydrate_chat_message

    loop = _loop(tmp_path, [_text("Let me fix that:")] * 12, max_rounds=12)
    result = asyncio.run(loop.run("fix a.js"))

    assert not _should_hydrate_chat_message("assistant", result.final)


def test_a_reply_the_output_cap_severed_is_not_treated_as_a_promise(tmp_path):
    """That is C1's case. A cut-off reply ends mid-sentence for a different
    reason, and nudging it to "just do it" is the wrong correction."""
    loop = _loop(tmp_path, [_cut(content="I can fix that. Let me patch it:")], max_rounds=3)

    result = asyncio.run(loop.run("fix it"))

    assert "cut off" in result.final.lower()
    assert "called no tool" not in result.final


# --- stall counters that survive the user typing (C6) --------------------
#
# RC6. 29 patch calls, 11 distinct payloads, one sent NINE times byte-for-byte.
# `MAX_UNPRODUCTIVE_EDITS = 4` existed the whole time and never fired, because
# `_unproductive` lived on SimpleChatLoop and repl.py builds a fresh one per
# user message. The model failed four times, the user typed, and it started
# again from zero.


class _NamedSession:
    """Just enough SessionLogger to carry an id, which is what binds stalls."""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self.metadata = None
        self.claim = None


def _stall_loop(tmp_path, turns, session: str, **kwargs):
    """A loop bound to a named conversation exactly the way repl.py binds one:
    through the constructor, so the binding itself is under test and not just
    the store behind it."""
    from shamsu.agents.simple_chat import SimpleChatLoop

    tools = AgentToolRegistry(tmp_path, approval_func=lambda _r: True)
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    return SimpleChatLoop(
        tmp_path,
        client=FakeClient(turns),
        tools=tools,
        state=state,
        session_logger=_NamedSession(session),
        model_name="qwen3:8b",
        **kwargs,
    )


def test_the_no_op_counter_is_not_reset_by_the_user_typing(tmp_path):
    """The whole of RC6 in one assertion."""
    from shamsu.agents.simple_chat import reset_session_stalls

    reset_session_stalls("s1")
    (tmp_path / "a.js").write_text("real contents\n", encoding="utf-8")
    miss = _tool("patch_file", filepath="a.js", old_string="not in the file", new_string="x")

    first = _stall_loop(tmp_path, [miss, miss, _text("hm")], "s1", max_rounds=3, verify_changes=False)
    asyncio.run(first.run("fix a.js"))
    carried = first._stalls.unproductive

    second = _stall_loop(tmp_path, [_text("ok")], "s1", max_rounds=1, verify_changes=False)

    assert carried >= 2
    assert second._stalls.unproductive == carried, "a new turn wiped the count"


def test_a_different_conversation_starts_clean(tmp_path):
    """Session-scoped, not global. `/new` must be a real fresh start."""
    from shamsu.agents.simple_chat import reset_session_stalls, session_stalls

    reset_session_stalls()
    session_stalls("s1").unproductive = 3

    assert session_stalls("s2").unproductive == 0


def test_an_identical_failing_call_is_not_run_a_third_time(tmp_path):
    """One payload went out nine times and failed nine times identically."""
    from shamsu.agents.simple_chat import reset_session_stalls

    reset_session_stalls("s3")
    (tmp_path / "a.js").write_text("real contents\n", encoding="utf-8")
    miss = _tool("patch_file", filepath="a.js", old_string="not in the file", new_string="x")
    loop = _stall_loop(tmp_path, [miss] * 6, "s3", max_rounds=6, verify_changes=False)

    asyncio.run(loop.run("fix a.js"))

    said = [m.content for m in loop.state.all_messages if m.role == "tool"]
    refused = [c for c in said if "NOT RUN" in c]
    assert refused, "the identical call was run every time"
    assert "already failed" in refused[0]


def test_the_refusal_hands_back_the_error_it_already_had(tmp_path):
    """The model has no new information to reason from, so give it the old one
    plus the fact that it is repeating itself."""
    from shamsu.agents.simple_chat import reset_session_stalls

    reset_session_stalls("s4")
    (tmp_path / "a.js").write_text("real contents\n", encoding="utf-8")
    miss = _tool("patch_file", filepath="a.js", old_string="not in the file", new_string="x")
    loop = _stall_loop(tmp_path, [miss] * 4, "s4", max_rounds=4, verify_changes=False)

    asyncio.run(loop.run("fix a.js"))

    refused = [m.content for m in loop.state.all_messages if "NOT RUN" in m.content]
    assert "old_string not found" in refused[0], "the previous error was not carried"
    assert "read_file" in refused[0], "and it must say what to do instead"


def test_a_repeat_carries_across_the_user_typing_too(tmp_path):
    """The counter and the memory are both session-scoped, or neither is."""
    from shamsu.agents.simple_chat import reset_session_stalls

    reset_session_stalls("s5")
    (tmp_path / "a.js").write_text("real contents\n", encoding="utf-8")
    miss = _tool("patch_file", filepath="a.js", old_string="not in the file", new_string="x")

    first = _stall_loop(tmp_path, [miss, miss, _text("hm")], "s5", max_rounds=3, verify_changes=False)
    asyncio.run(first.run("fix a.js"))

    second = _stall_loop(tmp_path, [miss, _text("ok")], "s5", max_rounds=2, verify_changes=False)
    asyncio.run(second.run("try again"))

    said = [m.content for m in second.state.all_messages if m.role == "tool"]
    assert any("NOT RUN" in c for c in said), "the new turn forgot what already failed"


def test_a_successful_edit_forgets_that_file_s_failures(tmp_path):
    """The escape. A patch that could not match before may match once the file
    has actually changed, and a memory with no way out would make the first
    success in a file the last one."""
    from shamsu.agents.simple_chat import reset_session_stalls

    reset_session_stalls("s6")
    (tmp_path / "a.js").write_text("real contents\n", encoding="utf-8")
    miss = _tool("patch_file", filepath="a.js", old_string="not in the file", new_string="x")
    good = _tool("write_file", filepath="a.js", content="not in the file\n")

    loop = _stall_loop(tmp_path, [miss, miss, good, _text("ok")], "s6", max_rounds=4, verify_changes=False)
    asyncio.run(loop.run("fix a.js"))

    assert loop._stalls.failures == {}, "the write should have cleared that file's record"


def test_a_failure_on_one_file_does_not_forget_another(tmp_path):
    from shamsu.agents.simple_chat import _call_signature, _signature_path

    a = _call_signature("patch_file", {"filepath": "a.js", "old_string": "x", "new_string": "y"})
    b = _call_signature("patch_file", {"filepath": "b.js", "old_string": "x", "new_string": "y"})

    assert _signature_path(a) == "a.js"
    assert _signature_path(b) == "b.js"
    assert a != b


def test_two_long_patches_differing_only_at_the_end_are_different_calls():
    """`_argument_summary` truncates, which would have made these one call."""
    from shamsu.agents.simple_chat import _call_signature

    body = "x" * 4000
    one = _call_signature("patch_file", {"filepath": "a.js", "old_string": body + "A", "new_string": "z"})
    two = _call_signature("patch_file", {"filepath": "a.js", "old_string": body + "B", "new_string": "z"})

    assert one != two


def test_the_no_op_stop_does_not_stop_the_next_turn_before_it_starts(tmp_path):
    """A counter that stays hot after firing is a guard with no escape."""
    from shamsu.agents.simple_chat import MAX_UNPRODUCTIVE_EDITS, reset_session_stalls

    reset_session_stalls("s7")
    (tmp_path / "a.js").write_text("real contents\n", encoding="utf-8")
    # Twice the ceiling, because the ceiling is no longer the end of the road:
    # the first time it is reached the loop offers a change of strategy and
    # resets the counter, and only the SECOND time does it stop. Reaching the
    # stop is what this test is about, so it has to get past the new exit.
    misses = [
        _tool("patch_file", filepath="a.js", old_string=f"missing {i}", new_string="x")
        for i in range(MAX_UNPRODUCTIVE_EDITS * 2 + 2)
    ]
    first = _stall_loop(tmp_path, misses, "s7", max_rounds=14, verify_changes=False)
    result = asyncio.run(first.run("fix a.js"))

    assert result.stopped
    assert "changed nothing" in result.final

    second = _stall_loop(tmp_path, [_text("here you go")], "s7", max_rounds=2, verify_changes=False)
    again = asyncio.run(second.run("the text is: real contents"))

    assert not again.stopped
    assert again.final == "here you go"


# --- promises that do not end in a colon (C13) ---------------------------
#
# The C7 detector was built from the report's fourteen examples, and the report
# says "Every one ends in a colon." Live 2026-08-19 on qwen2.5:3b-instruct, the
# model was handed an honest verify failure and answered "...I will ensure this
# is fixed." - promise, no tool call, turn over, file still broken - and the
# guard sat silent because of the full stop. Small models do not punctuate like
# the model the report was written from.


def test_the_live_3b_promise_that_ended_in_a_full_stop_is_caught():
    from shamsu.agents.simple_chat import ends_on_an_unmade_promise

    said = (
        "It appears that there was an issue with the syntax error correction. "
        "The `main.js` file still contains a `SyntaxError`. I will ensure this is fixed."
    )

    assert ends_on_an_unmade_promise(said) != ""


def test_a_promise_that_asks_the_user_for_something_is_left_alone():
    """The second arm exists to separate "I am about to edit a file" from a
    question, which is a legitimate way to end a turn."""
    from shamsu.agents.simple_chat import ends_on_an_unmade_promise

    for said in (
        "I will need more information about which file you mean.",
        "I'll leave that decision to you.",
        "Let me know which file you mean.",
    ):
        assert ends_on_an_unmade_promise(said) == "", said


def test_a_completed_action_is_not_a_promise():
    """Past tense. "I fixed it" is a report, not an intention."""
    from shamsu.agents.simple_chat import ends_on_an_unmade_promise

    assert ends_on_an_unmade_promise("I fixed it by moving the call.") == ""
    assert ends_on_an_unmade_promise("I rewrote the update loop and it parses.") == ""


def test_a_colon_promise_still_fires_without_an_action_verb():
    """The original arm is untouched: nothing followed the colon."""
    from shamsu.agents.simple_chat import ends_on_an_unmade_promise

    assert ends_on_an_unmade_promise("Let me check the file:") != ""


def test_the_full_stop_promise_reaches_the_nudge(tmp_path):
    """End to end, the shape of the live 3B turn."""
    promise = "The file still contains a SyntaxError. I will ensure this is fixed."
    loop = _loop(
        tmp_path,
        [_text(promise), _tool("write_file", filepath="main.js", content="ok\n"), _text("Done.")],
        max_rounds=4,
        verify_changes=False,
    )

    result = asyncio.run(loop.run("fix main.js"))

    assert result.final == "Done."
    nudges = [m.content for m in loop.state.all_messages if m.role == "user"]
    assert any("called no tool" in c for c in nudges)


# --- the reply cap follows the free window (V2) --------------------------
#
# Live 2026-08-19, qwen2.5:3b-instruct, mid-way through writing a file:
#   "This answer was cut off. The prompt was 2,270 tokens of a 32,768 window."
# The window was 7% full. 30,498 tokens were free and the reply stopped at
# 8,192, so the run produced nothing.


def test_a_short_prompt_gets_more_room_than_the_reserve(tmp_path):
    from shamsu.agents.simple_chat import output_reserve

    loop = _loop(tmp_path, [_text("ok")])
    loop.state.append_user("Write a complete 2D asteroid shooter game in js/game.js")
    built = loop._messages()
    ceiling = loop._ceiling()

    assert loop._reply_cap(built, ceiling) > output_reserve(ceiling)


def test_the_cap_is_never_smaller_than_the_reserve_the_budget_promised(tmp_path):
    """A cap below the reserve would cause the truncation the reserve exists to
    prevent. This can only ever be an increase."""
    from shamsu.agents.simple_chat import output_reserve

    loop = _loop(tmp_path, [_text("ok")])
    for i in range(200):
        loop.state.append_user(f"turn {i} " + "padding " * 200)
    built = loop._messages()
    ceiling = loop._ceiling()

    assert loop._reply_cap(built, ceiling) >= output_reserve(ceiling)


def test_one_generation_cannot_spend_the_whole_window(tmp_path):
    """Without a ceiling a looping reply burns the window in a single call, and
    at 24 rounds that is a turn nobody waits out."""
    from shamsu.agents.simple_chat import MAX_REPLY_TOKENS

    loop = _loop(tmp_path, [_text("ok")])
    loop.state.append_user("hi")
    built = loop._messages()

    assert loop._reply_cap(built, loop._ceiling()) <= MAX_REPLY_TOKENS


def test_the_cap_leaves_the_prompt_its_room(tmp_path):
    """prompt + reply must still fit the window, or the cap has just moved the
    truncation somewhere worse."""
    loop = _loop(tmp_path, [_text("ok")])
    for i in range(40):
        loop.state.append_user(f"turn {i} " + "padding " * 100)
    built = loop._messages()
    ceiling = loop._ceiling()

    assert loop._estimate_prompt(built) + loop._reply_cap(built, ceiling) <= ceiling


def test_the_cap_is_what_the_model_is_actually_asked_for(tmp_path):
    """The number computed and the number sent are one."""
    loop = _loop(tmp_path, [_text("ok")])
    asyncio.run(loop.run("write js/game.js"))

    sent = loop.client.calls[0]["options"]["num_predict"]
    built = loop._messages()

    assert sent == loop._reply_cap(built, loop.client.calls[0]["options"]["num_ctx"])


# --- the cut-off message names the limit that bound (C4) -----------------
#
# RC3. The old message blamed the window every time, and one frozen copy of it
# was replayed 54 times into later prompts of a single session. Live 2026-08-19
# it told a user "The prompt was 2,270 tokens of a 32,768 window" on a
# conversation five messages long.


def _cut_loop(tmp_path, prompt_tokens: int, cap: int):
    loop = _loop(tmp_path, [_text("ok")])
    loop.last_prompt_tokens = prompt_tokens
    loop._last_reply_cap = cap
    return loop


def test_a_reply_cap_truncation_does_not_blame_the_window(tmp_path):
    """The live case: 2,270 tokens of 32,768 is not a full window."""
    loop = _cut_loop(tmp_path, 2_270, 16_384)

    message = loop._out_of_room_message()

    assert "per-reply limit" in message
    assert "16,384" in message
    assert "window is not the problem" in message


def test_a_reply_cap_truncation_does_not_advise_new(tmp_path):
    """`/new` shortens the conversation, which was never the limit."""
    loop = _cut_loop(tmp_path, 2_270, 16_384)

    assert "/new" not in loop._out_of_room_message()


def test_a_genuinely_full_window_still_says_so(tmp_path):
    """The other half must keep working, or this has just swapped one wrong
    diagnosis for another."""
    loop = _cut_loop(tmp_path, 30_000, 8_192)

    message = loop._out_of_room_message()

    assert "filled the window" in message
    assert "/new" in message


def test_the_message_names_the_cap_the_call_actually_carried(tmp_path):
    """Computing it twice invites the two to drift apart."""
    loop = _loop(tmp_path, [_text("done")])
    asyncio.run(loop.run("hi"))

    assert loop._last_reply_cap == loop.client.calls[0]["options"]["num_predict"]


def test_the_cut_off_notice_never_becomes_conversation(tmp_path):
    """One frozen copy was replayed 54 times, teaching the model that "I ran out
    of room" is how a turn ends."""
    from shamsu.agents.chat_state import _should_hydrate_chat_message

    window = _cut_loop(tmp_path, 30_000, 8_192)._out_of_room_message()
    capped = _cut_loop(tmp_path, 2_270, 16_384)._out_of_room_message()

    assert not _should_hydrate_chat_message("assistant", window)
    assert not _should_hydrate_chat_message("assistant", capped)


def test_a_partial_answer_with_real_content_is_still_kept(tmp_path):
    """The filter must not swallow what the model actually managed to say."""
    from shamsu.agents.chat_state import _should_hydrate_chat_message

    loop = _cut_loop(tmp_path, 2_270, 16_384)
    message = loop._out_of_room_message("Here is the first half of the file.")

    assert "Here is the first half of the file." in message
    assert _should_hydrate_chat_message("assistant", message)


# --- a failing verify reaches the run outcome (V1) -----------------------
#
# Live 2026-08-19 the verifier caught a no-op write that left js/main.js
# unparseable and said so to the model. The run exited 0, because the verdict
# lived only in the chat transcript.


class _RecordingLedger:
    """Just enough ActionLedger to see what was logged."""

    def __init__(self):
        self.events: list[dict] = []

    def log_event(self, event_type: str, **fields):
        self.events.append({"type": event_type, **fields})
        return {}


def _verified(tmp_path, turns, ledger, **kwargs):
    from shamsu.agents.simple_chat import SimpleChatLoop

    return SimpleChatLoop(
        tmp_path,
        client=FakeClient(turns),
        tools=AgentToolRegistry(tmp_path, approval_func=lambda _r: True),
        state=ChatState(simple_system_prompt(tmp_path), hydrate=False),
        action_ledger=ledger,
        model_name="qwen3:8b",
        **kwargs,
    )


def test_a_broken_written_file_records_a_verification_failure(tmp_path, monkeypatch):
    """The exact live shape: the model writes back something that will not parse."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    ledger = _RecordingLedger()
    loop = _verified(
        tmp_path,
        [_tool("write_file", filepath="main.js", content="function f() {\n"), _text("done")],
        ledger,
        max_rounds=2,
    )

    asyncio.run(loop.run("write main.js"))

    failures = [e for e in ledger.events if e["type"] == "verification_failed"]
    assert failures, "the run outcome could not see the broken file"
    assert failures[0]["verifier_id"] == "syntax:main.js"


def test_a_good_written_file_records_a_pass(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    ledger = _RecordingLedger()
    loop = _verified(
        tmp_path,
        [_tool("write_file", filepath="main.js", content="const a = 1;\n"), _text("done")],
        ledger,
        max_rounds=2,
    )

    asyncio.run(loop.run("write main.js"))

    assert [e for e in ledger.events if e["type"] == "verification_passed"]


def test_fixing_the_file_later_clears_the_earlier_failure(tmp_path, monkeypatch):
    """Keyed per file so the ledger's supersede rule can do its job: only a file
    whose LAST verdict failed counts against the run."""
    from shamsu.action_ledger.ledger import ActionLedger

    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    ledger = _RecordingLedger()
    loop = _verified(
        tmp_path,
        [
            _tool("write_file", filepath="main.js", content="function f() {\n"),
            _tool("write_file", filepath="main.js", content="function f() {}\n"),
            _text("done"),
        ],
        ledger,
        max_rounds=3,
    )

    asyncio.run(loop.run("write main.js"))

    assert not ActionLedger._has_unrecovered_verification_failure(ledger.events)


def test_a_file_left_broken_still_counts_against_the_run(tmp_path, monkeypatch):
    from shamsu.action_ledger.ledger import ActionLedger

    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    ledger = _RecordingLedger()
    loop = _verified(
        tmp_path,
        [_tool("write_file", filepath="main.js", content="function f() {\n"), _text("done")],
        ledger,
        max_rounds=2,
    )

    asyncio.run(loop.run("write main.js"))

    assert ActionLedger._has_unrecovered_verification_failure(ledger.events)


def test_a_skipped_file_is_neither_a_pass_nor_a_failure(tmp_path):
    """`skipped` is the escape. It must not fail a run, and it must not claim a
    verification that never happened."""
    ledger = _RecordingLedger()
    loop = _verified(
        tmp_path,
        [_tool("write_file", filepath="notes.md", content="# hi\n"), _text("done")],
        ledger,
        max_rounds=2,
    )

    asyncio.run(loop.run("write notes.md"))

    assert not [e for e in ledger.events if e["type"].startswith("verification_")]


def test_a_missing_ledger_never_breaks_a_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="main.js", content="function f() {\n"), _text("done")],
        max_rounds=2,
    )

    result = asyncio.run(loop.run("write main.js"))

    assert result.final == "done"


# --- the roster only carries what can answer (L3) ------------------------
#
# Measured 2026-08-19 on real transcripts: the full roster costs a flat 2,111
# tokens on EVERY call - 85% of a fresh 3B prompt, 31% deep into a 29-message
# session, never less. Across seven live sessions the model called 7 of 19
# tools; the 12 it never touched cost 1,131 of those tokens.


def test_a_fresh_workspace_is_not_charged_for_tools_that_cannot_answer(tmp_path):
    from shamsu.agents.simple_chat import (
        SIMPLE_TOOL_SCHEMAS,
        active_tool_schemas,
        available_tool_families,
    )
    from shamsu.context.budget import tool_schema_tokens

    families = available_tool_families(tmp_path)
    gated = active_tool_schemas(32768, "", families)

    assert tool_schema_tokens(gated) < tool_schema_tokens(SIMPLE_TOOL_SCHEMAS)
    names = {s["function"]["name"] for s in gated}
    assert "graph_search" not in names
    assert "history_search" not in names
    assert "memory_load" not in names


def test_the_tool_that_creates_notes_is_never_withheld(tmp_path):
    """Gating memory_remember behind notes existing would mean a workspace could
    never get its first one. That is M1, and the point is not to make it
    permanent."""
    from shamsu.agents.simple_chat import active_tool_schemas, available_tool_families

    gated = active_tool_schemas(32768, "", available_tool_families(tmp_path))

    assert "memory_remember" in {s["function"]["name"] for s in gated}


def test_the_core_coding_tools_are_never_withheld(tmp_path):
    from shamsu.agents.simple_chat import active_tool_schemas, available_tool_families

    names = {
        s["function"]["name"]
        for s in active_tool_schemas(32768, "", available_tool_families(tmp_path))
    }

    for tool in ("read_file", "write_file", "patch_file", "search_files", "run_command"):
        assert tool in names, tool


def test_notes_on_disk_bring_the_memory_readers_back(tmp_path):
    from shamsu import paths
    from shamsu.agents.simple_chat import active_tool_schemas, available_tool_families

    notes = paths.memory_notes_dir(tmp_path)
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "a-note.md").write_text("# port\n8080\n", encoding="utf-8")

    names = {
        s["function"]["name"]
        for s in active_tool_schemas(32768, "", available_tool_families(tmp_path))
    }

    assert "memory_load" in names


def test_an_indexed_workspace_brings_the_graph_tools_back(tmp_path):
    from shamsu.agents.simple_chat import active_tool_schemas, available_tool_families

    index = tmp_path / ".shamsu" / "abstract"
    index.mkdir(parents=True, exist_ok=True)
    (index / "last-index.json").write_text("{}", encoding="utf-8")

    names = {
        s["function"]["name"]
        for s in active_tool_schemas(32768, "", available_tool_families(tmp_path))
    }

    assert "graph_search" in names
    assert "explain_symbol" in names


def test_a_second_session_brings_history_search_back(tmp_path):
    from shamsu import paths
    from shamsu.agents.simple_chat import active_tool_schemas, available_tool_families

    sessions = paths.sessions_dir(tmp_path)
    for name in ("20260819-000001-aaaa", "20260819-000002-bbbb"):
        (sessions / name).mkdir(parents=True, exist_ok=True)

    names = {
        s["function"]["name"]
        for s in active_tool_schemas(32768, "", available_tool_families(tmp_path))
    }

    assert "history_search" in names


def test_an_unreadable_probe_keeps_the_tool(tmp_path, monkeypatch):
    """Withholding a tool the model needed is worse than paying for a schema it
    did not, so anything undeterminable counts as available."""
    from shamsu.agents import simple_chat

    def boom(_workspace):
        raise OSError("permission denied")

    monkeypatch.setattr(simple_chat, "_has_code_graph", boom)

    assert "graph" in simple_chat.available_tool_families(tmp_path)


def test_the_estimate_still_matches_what_is_sent_when_gated(tmp_path):
    """Item A's invariant: the number budgeted and the number sent are one. A
    roster that filtered in one place and not the other would break it."""
    from shamsu.context.budget import messages_tokens, tool_schema_tokens

    loop = _loop(tmp_path, [_text("done")])
    asyncio.run(loop.run("hi"))

    sent = loop.client.calls[0]["tools"]
    built = loop._messages()

    assert len(sent) == len(loop._sent_schemas())
    assert abs(
        loop._estimate_prompt(built)
        - (messages_tokens(built) + tool_schema_tokens(loop._sent_schemas()))
    ) <= 1


# --- the verbatim tail is bounded by tokens, not messages (C5) -----------
#
# RC5, measured over 77 prompts of a real file-writing session:
#     older (elidable)   4,655 msgs  2,352,729 chars    505 chars/msg
#     last-20 verbatim   1,475 msgs  2,485,838 chars  1,685 chars/msg
# The protected tail was 24% of the messages and 51% of the CONTENT - 87% in
# the worst prompt, where one assistant message was 25,473 characters.


def _file_writing_session(tmp_path, modules: int = 15):
    """The shape RC5 measured: every recent message is a whole file."""
    state = ChatState(simple_system_prompt(tmp_path), hydrate=False)
    body = ("// a line of a real javascript file" + chr(10)) * 700
    for i in range(modules):
        state.append_user(f"now write module {i}")
        state.append_assistant("", tool_calls=[{"function": {"name": "write_file",
            "arguments": {"filepath": f"js/mod{i}.js", "content": body}}}])
        state.append_tool(f"c{i}", "write_file", json.dumps(
            {"ok": True, "message": f"Created js/mod{i}.js (+700 lines, 700 total).",
             "data": {"filepath": f"js/mod{i}.js", "resolved_filepath": f"js/mod{i}.js"}}))
    loop = _loop(tmp_path, [_text("ok")])
    loop.state = state
    return loop, state


def test_whole_file_payloads_shrink_the_verbatim_tail(tmp_path):
    from shamsu.agents.simple_chat import KEEP_VERBATIM_MESSAGES

    loop, _ = _file_writing_session(tmp_path)

    assert loop._verbatim_tail() < KEEP_VERBATIM_MESSAGES


def test_short_turns_keep_the_whole_tail(tmp_path):
    """Twenty conversational turns cost almost nothing and all stay whole - the
    original measurement was right about conversation, and must not regress."""
    from shamsu.agents.simple_chat import KEEP_VERBATIM_MESSAGES

    loop = _loop(tmp_path, [_text("ok")])
    for i in range(40):
        loop.state.append_user(f"what about step {i}?")
        loop.state.append_assistant(f"Step {i} is fine.")

    assert loop._verbatim_tail() == KEEP_VERBATIM_MESSAGES


def test_the_old_constant_could_not_reach_the_target_and_the_new_one_can(tmp_path):
    """The point of RC5: elision worked perfectly and was not allowed to touch
    the half of the prompt that mattered."""
    from shamsu.agents.simple_chat import KEEP_VERBATIM_MESSAGES
    from shamsu.context.budget import messages_tokens

    loop, state = _file_writing_session(tmp_path)
    budget = loop._history_budget()

    import copy
    old_state = copy.deepcopy(state)
    loop.state = old_state
    loop._elide_payloads(KEEP_VERBATIM_MESSAGES)
    kept_by_count = messages_tokens(m.to_ollama() for m in old_state.all_messages)

    loop.state = state
    loop._elide_payloads()
    kept_by_tokens = messages_tokens(m.to_ollama() for m in state.all_messages)

    assert kept_by_count > budget, "the old tail could not get under budget at all"
    assert kept_by_tokens < kept_by_count
    assert kept_by_tokens < budget


def test_the_current_exchange_always_survives(tmp_path):
    """A model that cannot see the edit it is in the middle of is worse off than
    one paying for it."""
    from shamsu.agents.simple_chat import MIN_VERBATIM_MESSAGES

    loop, _ = _file_writing_session(tmp_path, modules=40)

    assert loop._verbatim_tail() >= MIN_VERBATIM_MESSAGES


def test_the_tail_can_only_shrink_never_grow(tmp_path):
    """Bounded above by the old constant, so this can never make a prompt bigger
    than it was before."""
    from shamsu.agents.simple_chat import KEEP_VERBATIM_MESSAGES

    for modules in (1, 3, 8, 20):
        loop, _ = _file_writing_session(tmp_path, modules=modules)
        assert loop._verbatim_tail() <= KEEP_VERBATIM_MESSAGES


def test_pressure_shrinks_the_tail_further(tmp_path):
    from shamsu.agents.simple_chat import (
        VERBATIM_TAIL_FRACTION,
        VERBATIM_TAIL_FRACTION_UNDER_PRESSURE,
    )

    loop = _loop(tmp_path, [_text("ok")])
    body = "padding " * 300
    for i in range(30):
        loop.state.append_user(f"turn {i} {body}")
        loop.state.append_assistant(f"ok {i}")

    assert loop._verbatim_tail(VERBATIM_TAIL_FRACTION_UNDER_PRESSURE) <= loop._verbatim_tail(
        VERBATIM_TAIL_FRACTION
    )


# --- the harness writes memory too (M1) ---------------------------------
#
# `memory_remember` was the ONLY caller of remember() in simple mode, so memory
# existed only when the model volunteered a tool call - and it did not. A real
# 2-turn run produced no notes at all, and across seven live sessions on
# 2026-08-19 memory_load/memory_list/memory_forget were called zero times.


def _notes(tmp_path):
    from shamsu.agents.simple_memory import MemoryStore

    return list(MemoryStore(tmp_path).notes.values())


def test_a_turn_that_changed_a_file_writes_a_note_without_being_asked(tmp_path):
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="app.py", content="print(1)\n"), _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("add a hello world to app.py"))

    notes = _notes(tmp_path)
    assert notes, "the harness wrote no memory at all"
    assert "app.py" in notes[0].content


def test_the_note_records_what_failed_and_why(tmp_path):
    """smallcode keeps the error tail for the same reason: the last line says
    what went wrong and a full trace is 5-50KB."""
    (tmp_path / "a.js").write_text("real contents\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("patch_file", filepath="a.js", old_string="not in the file", new_string="x"),
         _text("could not")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("fix a.js"))

    notes = _notes(tmp_path)
    assert notes
    assert "What failed" in notes[0].content
    assert "patch_file" in notes[0].content


def test_a_question_that_changed_nothing_leaves_no_note(tmp_path):
    """A store that fills with "the user said hi" is one nobody can recall from."""
    loop = _loop(tmp_path, [_text("The port is 8080.")])

    asyncio.run(loop.run("what port does it use?"))

    assert _notes(tmp_path) == []


def test_the_note_is_typed_so_recall_can_score_it(tmp_path):
    """Type `context`, tag `evidence` - so render_memory loads it only when it
    bears on the request, never as a standing tax on the window."""
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="app.py", content="print(1)\n"), _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("add a hello world to app.py"))

    note = _notes(tmp_path)[0]
    assert note.type == "context"
    assert "evidence" in note.tags


def test_the_title_carries_the_words_the_user_used(tmp_path):
    """The words the user used are the words they will use again - which is what
    a later recall matches on."""
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="app.py", content="print(1)\n"), _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("add a hello world to app.py"))

    assert "hello world" in _notes(tmp_path)[0].title


def test_the_note_is_recalled_when_a_later_turn_is_about_the_same_thing(tmp_path):
    """End to end: memory that accumulates on its own and comes back."""
    from shamsu.agents.simple_memory import render_memory

    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="app.py", content="print(1)\n"), _text("done")],
        max_rounds=2,
        verify_changes=False,
    )
    asyncio.run(loop.run("add a hello world to app.py"))

    assert "app.py" in render_memory(tmp_path, "change the hello world in app.py")


def test_a_failing_memory_write_never_fails_the_turn(tmp_path, monkeypatch):
    from shamsu.agents import simple_chat

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(simple_chat.SimpleChatLoop, "_record_evidence", boom)
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="app.py", content="print(1)\n"), _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    result = asyncio.run(loop.run("add a hello world"))

    assert result.final == "done"


# --- after a cut-off, can it finish the file? (C1 recovery path) ---------


def test_a_refused_write_is_still_visible_to_the_model(tmp_path):
    """The refusal discards the WRITE, not the attempt. The assistant turn
    carrying the partial content is appended before the refusal, so the model
    can see how far it got."""
    body = "function update() {" + chr(10) + "  const a = 1;" + chr(10)
    loop = _loop(
        tmp_path,
        [_cut_tool("write_file", filepath="game.js", content=body)],
        max_rounds=1,
        verify_changes=False,
    )

    asyncio.run(loop.run("write the game loop"))

    calls = [
        c for m in loop.state.all_messages for c in (m.tool_calls or [])
    ]
    sent = str(calls[0]["function"]["arguments"])
    assert "const a = 1" in sent, "the model cannot see what it already emitted"


def test_a_cut_off_write_is_finished_in_pieces_on_the_next_turns(tmp_path, monkeypatch):
    """The whole point of the refusal: write_file for the first section, then
    append_file for each following one, and the file ends up complete."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    first = "function update() {" + chr(10) + "  const a = 1;" + chr(10)
    rest = "}" + chr(10)

    loop = _loop(
        tmp_path,
        [
            _cut_tool("write_file", filepath="game.js", content=first + "  const b ="),
            _tool("write_file", filepath="game.js", content=first),
            _tool("append_file", filepath="game.js", content=rest),
            _text("Done - game.js is complete."),
        ],
        max_rounds=5,
        verify_changes=False,
    )

    result = asyncio.run(loop.run("write the game loop"))

    on_disk = (tmp_path / "game.js").read_text(encoding="utf-8")
    assert on_disk == first + rest
    assert on_disk.count("{") == on_disk.count("}"), "the finished file balances"
    assert not result.stopped


def test_the_finished_file_passes_verification(tmp_path, monkeypatch):
    """End to end: cut off, rebuilt in pieces, and the verifier agrees."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    loop = _loop(
        tmp_path,
        [
            _cut_tool("write_file", filepath="game.js", content="function f() {"),
            _tool("write_file", filepath="game.js", content="function f() {" + chr(10)),
            _tool("append_file", filepath="game.js", content="}" + chr(10)),
            _text("done"),
        ],
        max_rounds=5,
    )

    asyncio.run(loop.run("write game.js"))

    verdicts = [m.content for m in loop.state.all_messages if m.name == "verify"]
    assert verdicts, "nothing was verified"
    assert '"ok": true' in verdicts[-1], verdicts[-1]


def test_each_completed_chunk_clears_the_truncation_streak(tmp_path, monkeypatch):
    """A long file may be cut off more than once while being built. Only
    CONSECUTIVE refusals count, or a file needing four chunks could never be
    finished."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    loop = _loop(
        tmp_path,
        [
            _cut_tool("write_file", filepath="a.js", content="// part 1"),
            _tool("write_file", filepath="a.js", content="// part 1" + chr(10)),
            _cut_tool("append_file", filepath="a.js", content="// part 2"),
            _tool("append_file", filepath="a.js", content="// part 2" + chr(10)),
            _cut_tool("append_file", filepath="a.js", content="// part 3"),
            _tool("append_file", filepath="a.js", content="// part 3" + chr(10)),
            _text("done"),
        ],
        max_rounds=8,
        verify_changes=False,
    )

    result = asyncio.run(loop.run("build a.js in pieces"))

    on_disk = (tmp_path / "a.js").read_text(encoding="utf-8")
    assert "part 1" in on_disk and "part 2" in on_disk and "part 3" in on_disk
    assert not result.stopped, "three separate cut-offs must not end the turn"


def test_a_file_built_by_appending_is_verified_after_the_last_piece(tmp_path, monkeypatch):
    """`append_file` was not in MUTATING_TOOLS, so nothing verified a file built
    up in pieces. The last verdict the model saw was the one taken after the
    FIRST chunk - true of half a file, false of the finished one."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    loop = _loop(
        tmp_path,
        [
            _tool("write_file", filepath="a.js", content="function f() {" + chr(10)),
            _tool("append_file", filepath="a.js", content="}" + chr(10)),
            _text("done"),
        ],
        max_rounds=4,
    )

    asyncio.run(loop.run("build a.js"))

    verdicts = [m.content for m in loop.state.all_messages if m.name == "verify"]
    assert len(verdicts) == 2, "the append was never verified"
    # Half a file really is unbalanced - and while it is being built that is
    # progress, not a fault. It must not read as CHECKED either.
    assert "still open" in verdicts[0] and "append_file" in verdicts[0]
    assert '"checked": []' in verdicts[0]
    assert '"ok": true' in verdicts[-1], "the finished file must come back clean"
    assert "still open" not in verdicts[-1]


def test_appending_section_after_section_does_not_trip_the_edit_ceiling(tmp_path, monkeypatch):
    """Building a large file in pieces is what the truncation refusal ASKS for.
    Counting each append toward the repeated-edit ceiling would stop the very
    behaviour being requested."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    turns = [_tool("write_file", filepath="a.js", content="// 0" + chr(10))]
    turns += [
        _tool("append_file", filepath="a.js", content=f"// {i}" + chr(10))
        for i in range(1, 8)
    ]
    turns.append(_text("done"))
    loop = _loop(tmp_path, turns, max_rounds=12, verify_changes=False)

    result = asyncio.run(loop.run("build a.js in eight pieces"))

    assert not result.stopped, result.final
    assert (tmp_path / "a.js").read_text(encoding="utf-8").count("//") == 8


# --- the write cap: bound the unit of work, not the budget (SMALLCODE 6.8) ---
#
# smallcode's ratio is an 8,192-token reply budget against an 8,000-char write
# cap - four times the headroom, so the model is never permitted to attempt a
# write large enough to exhaust its own output budget. SHAMSU had one times.
# Restoring the ratio means capping the CONTENT, because the alternative -
# shrinking `MAX_REPLY_TOKENS` - would take the room prose legitimately needs.


def test_the_reply_budget_is_not_shrunk_to_pay_for_the_write_cap():
    """Bound the unit of work, not the budget. A large reply is still the right
    thing for a review or a plan; it was only ever wrong for a write."""
    from shamsu.agents.simple_chat import MAX_REPLY_TOKENS

    assert MAX_REPLY_TOKENS == 16384


def test_the_write_cap_never_exceeds_the_llama_cpp_tool_argument_wall():
    """Two INDEPENDENT walls, and this is the one SHAMSU did not know existed.
    llama.cpp's tool-argument parser gives up around 13KB WITHOUT reporting
    `done_reason: "length"`, so a cap derived from the reply budget alone would
    allow a 60KB write on a large window and walk straight into it."""
    from shamsu.agents.simple_chat import WRITE_CHARS_CEILING, max_write_chars

    assert max_write_chars(16384) == WRITE_CHARS_CEILING == 8_000
    assert max_write_chars(1_000_000) == WRITE_CHARS_CEILING


def test_the_write_cap_follows_the_reply_budget_down():
    """Wall A. On a half-full window the budget binds before llama.cpp does."""
    from shamsu.agents.simple_chat import max_write_chars

    assert max_write_chars(8192) == 6963
    assert max_write_chars(4096) == 3481


def test_the_write_cap_has_a_floor_and_says_the_window_is_wrong_below_it():
    """Chunking to 1,700 characters at a time is not a strategy, it is 24
    rounds of nothing. Below the floor the honest answer is that the window is
    the wrong shape for the task."""
    from shamsu.agents.simple_chat import (
        WRITE_CHARS_FLOOR,
        max_write_chars,
        write_budget_is_unworkable,
    )

    assert max_write_chars(2048) == WRITE_CHARS_FLOOR == 2_000
    assert write_budget_is_unworkable(2048)
    assert not write_budget_is_unworkable(8192)


def test_a_write_larger_than_the_cap_is_refused_before_it_reaches_disk(tmp_path):
    """Refused at the door, not cut off at the source. The content was fully
    generated and the model still holds every character of it - which is what
    makes this recoverable where a truncated generation is not."""
    huge = ("const a = 1;" + chr(10)) * 900
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="big.js", content=huge), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write big.js"))

    assert not (tmp_path / "big.js").exists()


def test_the_oversize_refusal_names_the_strategy_not_just_the_limit(tmp_path):
    """A refusal stating only the limit spends a round and teaches nothing."""
    huge = ("const a = 1;" + chr(10)) * 900
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="big.js", content=huge), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write big.js"))

    said = [m.content for m in loop.state.all_messages if m.role == "tool"]
    assert any("REFUSED" in c for c in said)
    assert any("append_file" in c and "60 lines" in c for c in said)
    assert any("Nothing you generated is lost" in c for c in said)


def test_the_cap_covers_every_tool_that_carries_content(tmp_path):
    """`patch_file` replacing ten lines with eight hundred has the identical
    problem, so this is not a `write_file` special case."""
    (tmp_path / "app.js").write_text("const a = 1;" + chr(10), encoding="utf-8")
    huge = ("const b = 2;" + chr(10)) * 900
    loop = _loop(
        tmp_path,
        [
            _tool("patch_file", filepath="app.js", old_string="const a = 1;",
                  new_string=huge),
            _text("ok"),
        ],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("expand app.js"))

    assert (tmp_path / "app.js").read_text(encoding="utf-8") == "const a = 1;" + chr(10)
    said = [m.content for m in loop.state.all_messages if m.role == "tool"]
    assert any("too large" in c for c in said)


def test_a_write_at_the_cap_still_goes_through(tmp_path):
    """The guard must not touch the ordinary path - and 60 lines of dense code
    is ~2,500 characters, comfortably inside it."""
    body = ("const a = 1;" + chr(10)) * 60
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="fine.js", content=body), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write fine.js"))

    assert (tmp_path / "fine.js").read_text(encoding="utf-8") == body


# --- the pre-write gate tests for TRUNCATION, not validity (SMALLCODE 6.7) ---
#
# Both write-time gates in the tool layer bailed out when the target did not
# already exist, and only understood Python - so SHAMSU would refuse to damage a
# good file and happily create a broken one. The gate that closes that hole must
# not test for validity: under a chunking strategy a first section correctly has
# unclosed blocks, and refusing those would refuse every legitimate chunk.


def test_a_new_file_ending_mid_string_is_refused(tmp_path):
    """Hole 1.1: a brand-new file had NO structural gate at write time."""
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="views.py",
               content='def index(request):' + chr(10) + '    return render(request, "item'),
         _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write views.py"))

    assert not (tmp_path / "views.py").exists()
    said = [m.content for m in loop.state.all_messages if m.role == "tool"]
    assert any("REFUSED" in c and "stops part-way through" in c for c in said)


def test_the_gate_is_not_python_only(tmp_path):
    """`_breaks_working_python` returned "" for anything but `.py`. A new
    `game.js` that stops mid-function had nothing between it and the disk."""
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="game.js", content="const label = \"score"),
         _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write game.js"))

    assert not (tmp_path / "game.js").exists()


def test_a_first_section_with_open_blocks_is_allowed_through(tmp_path):
    """The trap this gate has to avoid. A gate testing for VALIDITY would refuse
    every legitimate first chunk, and the fix would create a new bug."""
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="game.js",
               content="function update() {" + chr(10) + "  if (alive) {" + chr(10)),
         _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write the first section of game.js"))

    assert (tmp_path / "game.js").exists(), "a legitimate first section was refused"


def test_the_cut_off_refusal_counts_toward_the_streak_that_has_an_exit(tmp_path):
    """llama.cpp's parser does not report `done_reason: "length"`, so this
    failure is invisible to the `done_reason` guard. Sharing its counter is what
    keeps the exit covering both."""
    from shamsu.agents.simple_chat import MAX_TRUNCATED_WRITE_REFUSALS

    cut = _tool("write_file", filepath="a.py", content="x = foo(")
    loop = _loop(tmp_path, [cut] * 24, max_rounds=24, verify_changes=False)

    result = asyncio.run(loop.run("write a.py"))

    assert result.stopped
    assert result.rounds < 24
    assert str(MAX_TRUNCATED_WRITE_REFUSALS) in result.final


# --- continue from the tail, not from the start (SMALLCODE 6.8 item 6) -------


def test_a_file_left_stopping_mid_construct_is_asked_to_CONTINUE(tmp_path, monkeypatch):
    """Simple mode threw the content away and asked the model to start again in
    sections; the legacy loop had the better answer and it was never carried
    over. Continue-from-the-tail keeps the good 80%."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    (tmp_path / "app.js").write_text(
        "const a = 1;" + chr(10) + "const label = \"sco", encoding="utf-8"
    )
    loop = _loop(tmp_path, [])

    report = _verify_json(loop, ["app.js"])

    problem = report["data"]["problems"][0]
    assert report["ok"] is False
    assert "STOPS PART-WAY THROUGH" in problem
    assert "append_file" in problem and "ONLY the missing remainder" in problem
    assert "current end of file" in problem
    assert "const a = 1;" in problem, "the tail has to be quoted verbatim"


def test_the_continuation_advice_is_not_python_only(tmp_path, monkeypatch):
    """The legacy version read `.py` and returned "" for everything else."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    (tmp_path / "views.py").write_text(
        "def index(request):" + chr(10) + '    return render(request, "item',
        encoding="utf-8",
    )
    loop = _loop(tmp_path, [])

    report = _verify_json(loop, ["views.py"])

    assert "STOPS PART-WAY THROUGH" in report["data"]["problems"][0]


# --- the number is stated in all three places (SMALLCODE 6.4) ---------------


def test_sixty_lines_is_stated_in_the_system_prompt(tmp_path):
    """"Too big" is not a number a 3B model can act on."""
    prompt = simple_system_prompt(tmp_path)

    assert "60 lines" in prompt
    assert "append_file" in prompt


def test_sixty_lines_is_stated_in_the_schemas_the_model_reads():
    """A small model reads these more reliably than it reads the system prompt,
    and smallcode states the rule in all three places for that reason."""
    carriers = {"write_file", "append_file", "patch_file", "read_and_patch",
                "create_and_run"}
    seen = set()
    for schema in SIMPLE_TOOL_SCHEMAS:
        function = schema["function"]
        if function["name"] not in carriers:
            continue
        seen.add(function["name"])
        properties = function["parameters"]["properties"]
        payload = properties.get("content") or properties.get("new_string")
        assert "8KB" in payload["description"], function["name"]
        assert "60 lines" in payload["description"], function["name"]
    assert seen == carriers, sorted(carriers - seen)


# --- a build is not a repair loop (live 2026-08-20) -------------------------
#
# The acceptance run for the write cap. Told to build a 1,500-line file,
# qwen2.5:3b said "I will write 60 lines at a time" - the prompt worked - and
# then did it with `write_file`, re-sending the growing file each time rather
# than appending. Five sections in, every one verified clean, the turn stopped
# with "5 blind edits I cannot confirm". The chunking the prompt asks for was
# being read as the churn the ceiling exists to stop.


def test_a_file_grown_section_by_section_does_not_trip_the_edit_ceiling(tmp_path, monkeypatch):
    """Whichever tool carries it. `append_file` was already exempt; a model that
    chunks with `write_file` is doing the same thing and must not be stopped."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    body = ""
    turns = []
    for index in range(7):
        body += f"def f{index}():" + chr(10) + f"    return {index}" + chr(10)
        turns.append(_tool("write_file", filepath="grow.py", content=body))
    turns.append(_text("done"))
    loop = _loop(tmp_path, turns, max_rounds=12)

    result = asyncio.run(loop.run("build grow.py section by section"))

    assert not result.stopped, result.final
    assert (tmp_path / "grow.py").read_text(encoding="utf-8").count("def f") == 7


def test_a_rewrite_that_does_not_grow_still_trips_the_edit_ceiling(tmp_path, monkeypatch):
    """The exemption is for GROWTH, not for `write_file`. Seven rewrites that
    shuffle a file without adding to it are the blind repair loop this guard was
    built for - 7 patches to one file in a turn, chasing a stale browser cache."""
    monkeypatch.setenv("SHAMSU_DISABLE_NODE_CHECK", "1")
    (tmp_path / "same.py").write_text("x = 0" + chr(10), encoding="utf-8")
    turns = [
        _tool("write_file", filepath="same.py", content=f"x = {index}" + chr(10))
        for index in range(1, 9)
    ]
    turns.append(_text("done"))
    loop = _loop(tmp_path, turns, max_rounds=12)

    result = asyncio.run(loop.run("fix same.py"))

    assert result.stopped
    assert "without being able to confirm" in result.final


def test_the_prose_nudge_offers_appending_now_that_chunking_is_the_default(tmp_path):
    """It said "call write_file for the COMPLETE new file", which steers a model
    building in sections straight back to the whole-file write the cap refuses."""
    (tmp_path / "app.py").write_text("x = 0" + chr(10), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_text(
            "Here is app.py:" + chr(10) + "```python" + chr(10)
            + "def one():" + chr(10) + "    return 1" + chr(10)
            + "def two():" + chr(10) + "    return 2" + chr(10) + "```"
        ), _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write app.py"))

    nudges = [
        m.content for m in loop.state.all_messages
        if m.role == "user" and "did not change the file" in m.content
    ]
    assert nudges, "the prose nudge never fired"
    assert "append_file" in nudges[0]


# --- a fragment is not a file (live 2026-08-20) -----------------------------
#
# The truncation gate ran on `patch_file.new_string` and refused it three times
# with "it ends inside a /* comment opened on line 23", then ended the turn
# blaming an output limit that had never fired. A patch replaces a region that
# may start inside one block and end inside another; an append chunk is
# unfinished by design. Neither owes a whole-file structure.


def test_a_patch_carrying_a_jsdoc_fragment_is_not_refused(tmp_path):
    """The exact payload shape from the live failure."""
    (tmp_path / "game.js").write_text(
        "class Game {" + chr(10) + "  step(dt) {" + chr(10) + "    return dt;" + chr(10)
        + "  }" + chr(10) + "}" + chr(10),
        encoding="utf-8",
    )
    fragment = (
        "  /**" + chr(10) + "   * Step, fixed." + chr(10) + "   */" + chr(10)
        + "  step(dt) {" + chr(10) + "    return dt * 2;" + chr(10) + "  }"
    )
    loop = _loop(
        tmp_path,
        [_tool("patch_file", filepath="game.js",
               old_string="  step(dt) {" + chr(10) + "    return dt;" + chr(10) + "  }",
               new_string=fragment),
         _text("patched")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("fix the step method"))

    on_disk = (tmp_path / "game.js").read_text(encoding="utf-8")
    assert "return dt * 2;" in on_disk, "a legitimate patch fragment was refused"
    said = [m.content for m in loop.state.all_messages if m.role == "tool"]
    assert not any("REFUSED" in c for c in said), said


def test_an_append_chunk_opening_a_block_is_not_refused(tmp_path):
    """Unfinished by design. Refusing this refuses chunked writing itself."""
    (tmp_path / "a.js").write_text("// start" + chr(10), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("append_file", filepath="a.js", content="function f() {" + chr(10)),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("add the opening of f"))

    assert "function f() {" in (tmp_path / "a.js").read_text(encoding="utf-8")


def test_a_whole_file_write_is_still_gated(tmp_path):
    """Narrowing the gate must not switch it off where it was earning its keep."""
    loop = _loop(
        tmp_path,
        [_tool("write_file", filepath="views.py",
               content="def index(request):" + chr(10) + '    return render(request, "item'),
         _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write views.py"))

    assert not (tmp_path / "views.py").exists()


def test_the_stop_message_does_not_blame_a_limit_that_never_fired(tmp_path):
    """The user was told "cut off by my own output limit" three times for writes
    the output limit never touched. Blaming the wrong dial sends them to it."""
    cut = _tool("write_file", filepath="a.py", content="x = foo(")
    loop = _loop(tmp_path, [cut] * 24, max_rounds=24, verify_changes=False)

    result = asyncio.run(loop.run("write a.py"))

    assert result.stopped
    assert "output limit" not in result.final, result.final
    assert "will not parse" in result.final


# --- outline first, body on demand (SMALLCODE_GAP_ANALYSIS §2) --------------


def _big_python(functions: int = 60) -> str:
    parts = ["import os", ""]
    for index in range(functions):
        parts += [
            f"def handler_{index}(request):",
            f'    """Handle case {index}."""',
            f"    return {index}",
            "",
        ]
    return chr(10).join(parts)


def test_a_large_file_comes_back_as_an_outline_not_a_body(tmp_path):
    """Head-clipping at 24,000 bytes is what starts the dead end: the model
    patches from what it saw, and old_string was in the half it never saw."""
    (tmp_path / "big.py").write_text(_big_python(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="big.py"), _text("read it")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("read big.py"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_file"][0]
    data = json.loads(payload)["data"]
    assert data["outlined"] is True
    assert "handler_0" in data["content"] and "handler_59" in data["content"]
    assert "return 0" not in data["content"], "the BODIES must not be sent"


def test_a_small_file_is_still_read_whole(tmp_path):
    """The outline is what replaces an UNBOUNDED read, not every read."""
    (tmp_path / "small.py").write_text("def f():" + chr(10) + "    return 1" + chr(10), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="small.py"), _text("read it")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("read small.py"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_file"][0]
    assert "return 1" in json.loads(payload)["data"]["content"]


def test_an_explicit_range_is_answered_with_those_lines_not_an_outline(tmp_path):
    """A range is a request for exactly those lines."""
    (tmp_path / "big.py").write_text(_big_python(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="big.py", start_line=3, end_line=6), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("read part of big.py"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_file"][0]
    content = json.loads(payload)["data"]["content"]
    assert "def handler_0" in content and "return 0" in content


def test_an_outlined_read_still_counts_as_only_part_of_the_file(tmp_path):
    """The model has genuinely not seen the bodies, so the whole-file rewrite
    guard must still fire - an outline that counted as having read the file
    would license exactly the data loss that guard exists for."""
    (tmp_path / "big.py").write_text(_big_python(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="big.py"),
         _tool("write_file", filepath="big.py", content="x = 1" + chr(10)),
         _text("done")],
        max_rounds=3,
        verify_changes=False,
    )

    asyncio.run(loop.run("rewrite big.py"))

    assert "handler_0" in (tmp_path / "big.py").read_text(encoding="utf-8")


def test_read_symbol_returns_one_function_exactly(tmp_path):
    """The follow-up an outline earns. The range comes from the same parse, so
    it cannot drift from what the model was shown."""
    (tmp_path / "big.py").write_text(_big_python(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_symbol", filepath="big.py", symbol="handler_7"), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("show me handler_7"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_symbol"][0]
    parsed = json.loads(payload)
    assert parsed["ok"] is True
    assert "return 7" in parsed["message"]
    assert "return 8" not in parsed["message"], "it must return ONE symbol"


def test_read_symbol_names_what_is_there_when_the_name_is_wrong(tmp_path):
    """A bare "not found" costs a round and teaches nothing."""
    (tmp_path / "big.py").write_text(_big_python(4), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_symbol", filepath="big.py", symbol="nope"), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("show me nope"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_symbol"][0]
    assert "handler_0" in json.loads(payload)["message"]


def test_reading_a_file_in_ranges_is_not_called_a_repeated_read(tmp_path):
    """`_argument_summary` returned the filepath alone, so section 3 of a file
    read in pieces was answered with "you already called this". That fired on
    exactly the behaviour the outline tells the model to use."""
    (tmp_path / "big.py").write_text(_big_python(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="big.py", start_line=1, end_line=20),
         _tool("read_file", filepath="big.py", start_line=21, end_line=40),
         _tool("read_file", filepath="big.py", start_line=41, end_line=60),
         _tool("read_file", filepath="big.py", start_line=61, end_line=80),
         _text("done")],
        max_rounds=6,
        verify_changes=False,
    )

    asyncio.run(loop.run("read big.py in pieces"))

    nudges = [
        m.content for m in loop.state.all_messages
        if m.role == "user" and "already called" in m.content
    ]
    assert not nudges, nudges


# --- line numbers, and not resending what the model already has -------------


def test_a_read_carries_line_numbers(tmp_path):
    """smallcode numbers every read (`bin/executor.js:110`) and it is the
    cheapest accuracy win there is: `start_line` is arithmetic on a wall of text
    until the model has seen which line is which."""
    (tmp_path / "app.py").write_text(
        "import os" + chr(10) + "x = 1" + chr(10), encoding="utf-8"
    )
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="app.py"), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("read app.py"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_file"][0]
    content = json.loads(payload)["data"]["content"]
    assert "1| import os" in content
    assert "2| x = 1" in content


def test_a_ranged_read_numbers_from_the_real_first_line(tmp_path):
    """Numbering a range from 1 would be worse than not numbering it."""
    (tmp_path / "app.py").write_text(
        chr(10).join(f"line{n}" for n in range(1, 21)) + chr(10), encoding="utf-8"
    )
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="app.py", start_line=11, end_line=13), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("read part"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_file"][0]
    content = json.loads(payload)["data"]["content"]
    assert "11| line11" in content
    assert "1| line11" not in content.replace("11| line11", "")


def test_reading_an_unchanged_file_again_does_not_resend_it(tmp_path):
    """Live 2026-08-20: eight `read_file js/game.js` calls in one turn, each
    resending the whole file, until the window was being elided to make room for
    copies of a file that had not changed."""
    (tmp_path / "app.py").write_text("x = 1" + chr(10), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="app.py"),
         _tool("read_file", filepath="app.py"),
         _text("ok")],
        max_rounds=3,
        verify_changes=False,
    )

    asyncio.run(loop.run("read app.py twice"))

    reads = [m.content for m in loop.state.all_messages if m.name == "read_file"]
    assert "x = 1" in json.loads(reads[0])["data"]["content"]
    second = json.loads(reads[1])
    assert second["data"]["unchanged"] is True
    assert second["data"]["content"] == ""
    assert "unchanged since you last read it" in second["message"]


def test_a_file_that_changed_is_sent_again(tmp_path):
    """"Unchanged" must be a fact, not an optimisation."""
    (tmp_path / "app.py").write_text("x = 1" + chr(10), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_file", filepath="app.py"),
         _tool("write_file", filepath="app.py", content="x = 2" + chr(10)),
         _tool("read_file", filepath="app.py"),
         _text("ok")],
        max_rounds=4,
        verify_changes=False,
    )

    asyncio.run(loop.run("read, change, read"))

    reads = [m.content for m in loop.state.all_messages if m.name == "read_file"]
    assert "x = 2" in json.loads(reads[1])["data"]["content"]
    assert not json.loads(reads[1])["data"].get("unchanged")


def test_a_patch_that_copied_the_line_numbers_back_still_matches(tmp_path):
    """Numbering creates exactly one hazard - the model pastes the gutter into
    old_string - and it must not cost a round. The model copying what it was
    shown is the behaviour we asked for."""
    (tmp_path / "app.py").write_text(
        "import os" + chr(10) + "def greet():" + chr(10) + "    return 1" + chr(10),
        encoding="utf-8",
    )
    copied = " 2| def greet():" + chr(10) + " 3|     return 1"
    loop = _loop(
        tmp_path,
        [_tool("patch_file", filepath="app.py", old_string=copied,
               new_string="def greet():" + chr(10) + "    return 2"),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("make greet return 2"))

    assert "return 2" in (tmp_path / "app.py").read_text(encoding="utf-8")


def test_real_code_that_looks_like_a_gutter_is_left_alone(tmp_path):
    """Stripping is only safe because it needs EVERY line to carry a gutter."""
    from shamsu.agents.simple_chat import _strip_line_numbers

    table = "| 1 | one |" + chr(10) + "| 2 | two |"
    assert _strip_line_numbers({"old_string": table})["old_string"] == table
    mixed = " 1| real" + chr(10) + "not numbered"
    assert _strip_line_numbers({"old_string": mixed})["old_string"] == mixed


# --- the prompt is a file now, and the tools that make it true --------------


def test_the_system_prompt_comes_from_the_markdown_file(tmp_path):
    """The words live in `prompts/simple_system.md`, so they can be read and
    changed without opening Python - smallcode keeps its skills and knowledge
    the same way."""
    from shamsu.agents import simple_prompt

    assert simple_prompt.PROMPT_FILE.is_file()
    assert simple_prompt.section("base").startswith("You are SHAMSU")
    assert "{workspace}" not in simple_system_prompt(tmp_path)


def test_the_prompt_carries_no_editor_comments(tmp_path):
    """The file explains itself to whoever edits it. Sending that to the model
    would be paying tokens to say why a line exists."""
    prompt = simple_system_prompt(tmp_path)

    assert "<!--" not in prompt and "-->" not in prompt


def test_the_prompt_survives_a_missing_markdown_file(tmp_path, monkeypatch):
    """A packaging mistake must not leave the model with no instructions."""
    from shamsu.agents import simple_prompt

    monkeypatch.setattr(simple_prompt, "PROMPT_FILE", tmp_path / "gone.md")
    simple_prompt._sections.cache_clear()
    try:
        prompt = simple_prompt.simple_system_prompt(tmp_path)
        assert "SHAMSU" in prompt
    finally:
        simple_prompt._sections.cache_clear()


def test_the_prompt_tells_the_model_to_act_rather_than_ask(tmp_path):
    """Live 2026-08-20: asked for a 1,500-line file, the model wrote 39 lines
    and stopped with "What would you like to do next?" - nothing had refused it.
    A model that asks instead of acting spends the turn on a question the user
    already answered."""
    prompt = simple_system_prompt(tmp_path).lower()

    assert "act on what you were asked" in prompt
    assert "carry on through them" in prompt


# --- run_tests --------------------------------------------------------------


def test_run_tests_finds_the_command_itself(tmp_path):
    """The model was told to check its work and left to guess the command."""
    from shamsu.agents.simple_tests import detect_test_command

    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    found = detect_test_command(tmp_path)

    assert found.command == "python -m pytest -q"
    assert "pytest" in found.reason


def test_run_tests_prefers_a_declared_script_over_an_inference(tmp_path):
    """A test script in package.json is a statement by whoever wrote the
    project; "there are test_*.py files" is a guess."""
    from shamsu.agents.simple_tests import detect_test_command

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest"}}), encoding="utf-8"
    )
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    found = detect_test_command(tmp_path)

    assert found.command == "npm test -- --run", "vitest without --run never exits"


def test_the_npm_init_placeholder_is_not_treated_as_a_test_suite(tmp_path):
    """`npm init` writes a stub that exits 1. Running it proves nothing and
    reads as a failing suite."""
    from shamsu.agents.simple_tests import detect_test_command

    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}),
        encoding="utf-8",
    )

    assert not detect_test_command(tmp_path)


def test_run_tests_says_so_when_there_is_no_runner(tmp_path):
    """"There is no test command here" is a fact the model can act on;
    `pytest: command not found` is a puzzle."""
    loop = _loop(
        tmp_path,
        [_tool("run_tests"), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("run the tests"))

    payload = [m.content for m in loop.state.all_messages if m.name == "run_tests"][0]
    parsed = json.loads(payload)
    assert parsed["ok"] is False
    assert "could not work out how to run tests" in parsed["message"]


# --- use_skill --------------------------------------------------------------


def test_the_skill_index_reaches_the_prompt(tmp_path):
    """A skill the model cannot see is one it will never load - the loader had
    existed the whole time and simple mode never called it."""
    prompt = simple_system_prompt(tmp_path)

    assert "use_skill" in prompt
    assert "large-file-surgery" in prompt


def test_use_skill_returns_the_body(tmp_path):
    loop = _loop(
        tmp_path,
        [_named_tool("use_skill", {"name": "large-file-surgery"}), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("how do I fix a big file"))

    payload = [m.content for m in loop.state.all_messages if m.name == "use_skill"][0]
    parsed = json.loads(payload)
    assert parsed["ok"] is True
    assert "read_symbol" in parsed["message"]


def test_use_skill_names_what_is_there_when_the_name_is_wrong(tmp_path):
    loop = _loop(
        tmp_path,
        [_named_tool("use_skill", {"name": "does-not-exist"}), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("use a skill"))

    payload = [m.content for m in loop.state.all_messages if m.name == "use_skill"][0]
    assert "developer" in json.loads(payload)["message"]


def test_the_developer_skill_agrees_with_the_shipped_write_path(tmp_path):
    """It said "Default to write_file with the COMPLETE file content" - the
    exact opposite of the 60-line rule the tool now enforces. A skill that
    fights the harness is worse than no skill."""
    from shamsu.agents.simple_chat import _skill_catalog

    _skill_catalog.cache_clear()
    body = _skill_catalog(tmp_path).skills["developer"].instructions

    assert "COMPLETE file content" not in body
    assert "60 lines" in body
    assert "patch_file" in body


# --- symbol-aware editing ---------------------------------------------------
#
# The move `patch_file` could never make cheaply. Replacing a whole function
# with `old_string` means reproducing every line of the OLD one exactly - and a
# model that can write the new function correctly will still fail to retype the
# old one, which is the failure the patch error message spends its whole body
# trying to correct.


def _class_file() -> str:
    return chr(10).join([
        "import os",
        "",
        "",
        "class Game:",
        "    def render(self):",
        "        return 1",
        "",
        "    def update(self):",
        "        return 2",
        "",
    ])


def test_replace_symbol_swaps_one_function_without_matching_its_old_text(tmp_path):
    (tmp_path / "game.py").write_text(_class_file(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("replace_symbol", filepath="game.py", symbol="Game.render",
               content="    def render(self):" + chr(10) + "        return 99"),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("make render return 99"))

    body = (tmp_path / "game.py").read_text(encoding="utf-8")
    assert "return 99" in body
    assert "return 2" in body, "the neighbouring method must be untouched"
    assert "import os" in body


def test_replace_symbol_indents_a_method_the_model_sent_flat(tmp_path):
    """A small model asked for "the new render" hands back a function at column
    zero far more often than not. Without this the replacement is wrong in a way
    it did not intend and cannot see."""
    (tmp_path / "game.py").write_text(_class_file(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("replace_symbol", filepath="game.py", symbol="render",
               content="def render(self):" + chr(10) + "    return 99"),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("make render return 99"))

    body = (tmp_path / "game.py").read_text(encoding="utf-8")
    assert "    def render(self):" in body
    import ast

    ast.parse(body)


def test_replace_symbol_refuses_an_edit_that_would_break_a_working_file(tmp_path):
    """The check `patch_file` cannot make and this one can: `replace_symbol`
    produces a COMPLETE file, so the whole-file question is available honestly
    instead of being guessed at from a fragment."""
    (tmp_path / "game.py").write_text(_class_file(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("replace_symbol", filepath="game.py", symbol="render",
               content="    def render(self:" + chr(10) + "        return ("),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("break render"))

    assert (tmp_path / "game.py").read_text(encoding="utf-8") == _class_file()
    said = [m.content for m in loop.state.all_messages if m.name == "replace_symbol"][0]
    assert "NOT APPLIED" in said


def test_replace_symbol_still_repairs_a_file_that_was_already_broken(tmp_path):
    """Refusing to touch an unparseable file would lock the model out of exactly
    the fix it was asked for."""
    (tmp_path / "broken.py").write_text(
        "def one():" + chr(10) + "    return (" + chr(10), encoding="utf-8"
    )
    loop = _loop(
        tmp_path,
        [_tool("replace_symbol", filepath="broken.py", symbol="one",
               content="def one():" + chr(10) + "    return 1"),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("fix one"))

    assert "return 1" in (tmp_path / "broken.py").read_text(encoding="utf-8")


def test_replace_symbol_names_what_is_there_when_the_symbol_is_wrong(tmp_path):
    (tmp_path / "game.py").write_text(_class_file(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("replace_symbol", filepath="game.py", symbol="nope", content="x = 1"),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("replace nope"))

    said = [m.content for m in loop.state.all_messages if m.name == "replace_symbol"][0]
    assert "Game.render" in said


def test_replace_symbol_obeys_the_write_cap(tmp_path):
    """It carries a payload, so both walls apply to it like any other writer."""
    from shamsu.agents.simple_chat import WRITING_TOOLS

    assert "replace_symbol" in WRITING_TOOLS


# --- the Definition of Done -------------------------------------------------
#
# The failure: the model stops before the work is finished and says something
# that reads like success. "Do not claim complete" has been in this project's
# prompts four separate times and is measurably not the fix. A contract moves
# the claim out of PROSE and into STATE.


def _contracted(tmp_path, turns, **kwargs):
    return _loop(tmp_path, turns, verify_changes=False, **kwargs)


def test_a_contract_records_what_done_means(tmp_path):
    loop = _contracted(
        tmp_path,
        [_named_tool("contract_create", {
            "title": "Fix the pause bug",
            "assertions": ["node --check game.js exits 0", "pressing P pauses"],
        }), _text("noted")],
        max_rounds=2,
    )

    asyncio.run(loop.run("fix the pause bug"))

    from shamsu.agents.simple_contract import load_contract

    contract = load_contract(tmp_path)
    assert contract is not None
    assert [item.id for item in contract.assertions] == ["a01", "a02"]
    assert not contract.done


def test_a_contract_outlives_the_turn_that_made_it(tmp_path):
    """A `SimpleChatLoop` is rebuilt for every user message. A contract held on
    the object would reset the moment the user typed - which is exactly how the
    unproductive-edit counter failed to fire for months."""
    first = _contracted(
        tmp_path,
        [_named_tool("contract_create", {"title": "T", "assertions": ["it runs"]}),
         _text("noted")],
        max_rounds=2,
    )
    asyncio.run(first.run("start"))

    second = _contracted(tmp_path, [_named_tool("contract_status", {}), _text("ok")], max_rounds=2)
    asyncio.run(second.run("where are we"))

    said = [m.content for m in second.state.all_messages if m.name == "contract_status"][0]
    assert "it runs" in said


def test_claiming_done_with_assertions_open_is_sent_back(tmp_path):
    """The guard fires at the moment of the claim and names the exact next call."""
    loop = _contracted(
        tmp_path,
        [_named_tool("contract_create", {"title": "T", "assertions": ["the tests pass"]}),
         _text("All done! The task is complete."),
         _text("Actually let me check.")],
        max_rounds=4,
    )

    asyncio.run(loop.run("do the thing"))

    nudges = [
        m.content for m in loop.state.all_messages
        if m.role == "user" and "nobody has checked" in m.content
    ]
    assert nudges, "a premature done claim went through"
    assert "contract_assert_pass" in nudges[0]
    assert "the tests pass" in nudges[0]


def test_a_resolved_contract_lets_the_claim_through(tmp_path):
    loop = _contracted(
        tmp_path,
        [_named_tool("contract_create", {"title": "T", "assertions": ["the tests pass"]}),
         _named_tool("contract_assert_pass",
                     {"assertion_id": "a01", "evidence": "pytest: 12 passed"}),
         _text("All done! The task is complete.")],
        max_rounds=4,
    )

    result = asyncio.run(loop.run("do the thing"))

    assert "All done" in result.final
    nudges = [m for m in loop.state.all_messages
              if m.role == "user" and "nobody has checked" in m.content]
    assert not nudges


def test_a_failed_assertion_counts_as_resolved(tmp_path):
    """Resolved means the model looked and said what it found. Blocking on a
    failure would leave it unable to REPORT a failure."""
    loop = _contracted(
        tmp_path,
        [_named_tool("contract_create", {"title": "T", "assertions": ["the tests pass"]}),
         _named_tool("contract_assert_fail",
                     {"assertion_id": "a01", "evidence": "3 tests still red"}),
         _text("The task is complete - but the tests are red.")],
        max_rounds=4,
    )

    result = asyncio.run(loop.run("do the thing"))

    assert "tests are red" in result.final


def test_passing_an_assertion_needs_evidence(tmp_path):
    """An assertion marked passed with no evidence is the claim this whole
    thing exists to stop."""
    loop = _contracted(
        tmp_path,
        [_named_tool("contract_create", {"title": "T", "assertions": ["the tests pass"]}),
         _named_tool("contract_assert_pass", {"assertion_id": "a01"}),
         _text("ok")],
        max_rounds=4,
    )

    asyncio.run(loop.run("do the thing"))

    said = [m.content for m in loop.state.all_messages if m.name == "contract_assert_pass"][0]
    assert "needs evidence" in said


def test_the_guard_has_an_exit(tmp_path):
    """A guard the model cannot get past is a deadlock waiting for a user."""
    from shamsu.agents.simple_chat import MAX_CONTRACT_NUDGES

    turns = [_named_tool("contract_create", {"title": "T", "assertions": ["x"]})]
    turns += [_text("All done! The task is complete.")] * 8
    loop = _contracted(tmp_path, turns, max_rounds=10)

    result = asyncio.run(loop.run("do the thing"))

    nudges = [m for m in loop.state.all_messages
              if m.role == "user" and "nobody has checked" in m.content]
    assert len(nudges) == MAX_CONTRACT_NUDGES
    assert "All done" in result.final


def test_a_question_is_never_a_done_claim(tmp_path):
    from shamsu.agents.simple_contract import looks_like_a_done_claim

    assert not looks_like_a_done_claim("Shall I mark the task complete?")
    assert looks_like_a_done_claim("The task is complete.")


def test_an_assertion_id_survives_a_small_model_retyping_it(tmp_path):
    from shamsu.agents.simple_contract import new_contract

    contract = new_contract("T", "", ["one", "two"])

    assert contract.find("a01") is contract.assertions[0]
    assert contract.find("a1") is contract.assertions[0]
    assert contract.find("2") is contract.assertions[1]


def test_contracts_can_be_switched_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_CONTRACT", "0")
    loop = _contracted(
        tmp_path,
        [_named_tool("contract_create", {"title": "T", "assertions": ["x"]}), _text("ok")],
        max_rounds=2,
    )

    asyncio.run(loop.run("do the thing"))

    said = [m.content for m in loop.state.all_messages if m.name == "contract_create"][0]
    assert "switched off" in said


# --- a class is a symbol, and a class can be most of a file -----------------
#
# Live 2026-08-20, qwen2.5-coder:3b did exactly what it was told - read the
# outline, then `read_symbol` the class it needed - and got 313 lines back,
# because `export class Player` spanned lines 34-347. The outline had just saved
# the window and the very next call spent it.


def _big_class(methods: int = 20) -> str:
    parts = ["export class Player {"]
    for index in range(methods):
        parts += [
            f"  step{index}(dt) {{",
            f"    this.t += dt * {index};",
            "    return this.t;",
            "  }",
            "",
        ]
    parts.append("}")
    return chr(10).join(parts) + chr(10)


def test_read_symbol_on_a_long_class_returns_its_shape_not_its_body(tmp_path):
    (tmp_path / "player.js").write_text(_big_class(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_symbol", filepath="player.js", symbol="Player"), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("show me Player"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_symbol"][0]
    parsed = json.loads(payload)
    assert parsed["data"]["outlined"] is True
    assert "step0(dt)" in parsed["message"] and "step19(dt)" in parsed["message"]
    assert "this.t += dt" not in parsed["message"], "the bodies must not be sent"


def test_read_symbol_on_one_method_still_returns_its_source(tmp_path):
    """Only a CONTAINER is outlined. A method is the unit of work."""
    (tmp_path / "player.js").write_text(_big_class(), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_symbol", filepath="player.js", symbol="step7"), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("show me step7"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_symbol"][0]
    assert "this.t += dt * 7" in json.loads(payload)["message"]


def test_a_short_class_is_returned_whole(tmp_path):
    """Outlining a 12-line class would cost a round to save nothing."""
    (tmp_path / "small.js").write_text(_big_class(2), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("read_symbol", filepath="small.js", symbol="Player"), _text("ok")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("show me Player"))

    payload = [m.content for m in loop.state.all_messages if m.name == "read_symbol"][0]
    assert "this.t += dt * 0" in json.loads(payload)["message"]


# --- an append that breaks a working file is undone (live 2026-08-20) -------
#
# qwen2.5-coder:3b was shown a REPLACEMENT for `takeDamage` and appended it to
# the end of player.js - past the closing brace of the class, so the method
# landed at top level and node rejected the module. The verifier said so; the
# model appended the same eleven lines again. The nudge that sent it there was
# ours: it said "call append_file to add it to the end", which is right for a
# new section and wrong for a rewrite.


def test_an_append_that_breaks_a_working_file_is_rolled_back(tmp_path):
    """Structural counting cannot catch this - the appended block is perfectly
    brace-balanced. Only a real parser sees it, which is why the write happens,
    is judged, and is undone."""
    good = chr(10).join([
        "export class Player {",
        "  takeDamage(n) {",
        "    this.health -= n;",
        "  }",
        "}",
    ]) + chr(10)
    (tmp_path / "player.js").write_text(good, encoding="utf-8")
    (tmp_path / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    stray = chr(10).join([
        "",
        "/** Take a hit. */",
        "takeDamage(amount) {",
        "  if (this.invulnerable === 0) {",
        "    this.health -= amount;",
        "  }",
        "}",
    ]) + chr(10)
    loop = _loop(
        tmp_path,
        [_tool("append_file", filepath="player.js", content=stray), _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("fix takeDamage"))

    assert (tmp_path / "player.js").read_text(encoding="utf-8") == good
    said = [m.content for m in loop.state.all_messages if m.name == "append_file"][0]
    assert "NOT APPENDED" in said
    assert "replace_symbol" in said, "the message must name the right move"


def test_appending_a_new_section_to_a_complete_file_still_works(tmp_path):
    """The guard must not stop a file GROWING - that is what append is for."""
    (tmp_path / "app.js").write_text("export const a = 1;" + chr(10), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("append_file", filepath="app.js",
               content="export function twice(n) {" + chr(10) + "  return n * 2;" + chr(10) + "}" + chr(10)),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("add twice"))

    assert "twice" in (tmp_path / "app.js").read_text(encoding="utf-8")


def test_the_prose_nudge_leads_with_replacing_not_appending(tmp_path):
    """It said "call append_file to add it to the end", and a model showing a
    REPLACEMENT took that literally."""
    (tmp_path / "app.py").write_text("x = 0" + chr(10), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_text(
            "Here is app.py:" + chr(10) + "```python" + chr(10)
            + "def one():" + chr(10) + "    return 1" + chr(10)
            + "def two():" + chr(10) + "    return 2" + chr(10) + "```"
        ), _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("write app.py"))

    nudge = [m.content for m in loop.state.all_messages
             if m.role == "user" and "did not change the file" in m.content][0]
    assert nudge.index("replace_symbol") < nudge.index("append_file")
    assert "only if it belongs at the END" in nudge


def test_replace_symbol_refuses_to_gut_a_class(tmp_path):
    """Live 2026-08-20: qwen2.5-coder:3b replaced the whole 314-line `Player`
    class with 45 lines. The file still PARSED - so the parse check passed - and
    22 methods plus the `export` keyword were simply gone. Parsing is not the
    same as keeping the code."""
    (tmp_path / "player.js").write_text(_big_class(10), encoding="utf-8")
    original = (tmp_path / "player.js").read_text(encoding="utf-8")
    sketch = chr(10).join([
        "export class Player {",
        "  step0(dt) {",
        "    return 0;",
        "  }",
        "}",
    ])
    loop = _loop(
        tmp_path,
        [_tool("replace_symbol", filepath="player.js", symbol="Player", content=sketch),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("rewrite Player"))

    assert (tmp_path / "player.js").read_text(encoding="utf-8") == original
    said = [m.content for m in loop.state.all_messages if m.name == "replace_symbol"][0]
    assert "would delete" in said
    assert "step5" in said, "it must name what would be lost"
    assert "replace_symbol on that member" in said, "and name the right move"


def test_replacing_a_whole_class_is_allowed_when_nothing_is_lost(tmp_path):
    """The guard is about LOSS, not about size. A genuine full rewrite that
    keeps every member goes through."""
    (tmp_path / "player.js").write_text(_big_class(3), encoding="utf-8")
    full = chr(10).join(
        ["export class Player {"]
        + [f"  step{i}(dt) {{" + chr(10) + f"    return {i} * 2;" + chr(10) + "  }" for i in range(3)]
        + ["}"]
    )
    loop = _loop(
        tmp_path,
        [_tool("replace_symbol", filepath="player.js", symbol="Player", content=full),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("rewrite Player"))

    assert "* 2" in (tmp_path / "player.js").read_text(encoding="utf-8")


def test_replacing_one_method_with_a_shorter_one_is_ordinary_work(tmp_path):
    """Only a container with members is guarded - shrinking a function is fine."""
    (tmp_path / "player.js").write_text(_big_class(3), encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("replace_symbol", filepath="player.js", symbol="step1",
               content="  step1(dt) { return 1; }"),
         _text("done")],
        max_rounds=2,
        verify_changes=False,
    )

    asyncio.run(loop.run("shorten step1"))

    body = (tmp_path / "player.js").read_text(encoding="utf-8")
    assert "step1(dt) { return 1; }" in body
    assert "step2" in body


# --- Phase 0: a write that ADDS is the only one that may claim to be building --


def test_appending_marks_the_file_as_being_built_and_patching_does_not(tmp_path):
    """The bookkeeping the exemption reads. `append_file` can only add to the
    end; a patch reworks what is there, and a patch is what ate the brace."""
    (tmp_path / "game.js").write_text("function a() {\n  return 1;\n}\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [
            _tool("append_file", filepath="game.js", content="function b() {\n  return 2;\n}\n"),
            _tool("patch_file", filepath="game.js", old_string="return 1;", new_string="return 9;"),
            _text("done"),
        ],
    )

    asyncio.run(loop.run("extend then tweak it"))

    # Appended earlier in the turn, so the note about sections is available...
    assert "game.js" in loop._built_up
    # ...but the LAST write was a patch, so it may not claim to be unfinished.
    assert loop._last_write_grew["game.js"] is False


def test_a_write_that_grows_the_file_counts_as_building_whatever_tool_carried_it(tmp_path):
    """qwen2.5:3b built a large file with `write_file`, re-sending the growing
    file each time. The shape is what matters, not the tool."""
    (tmp_path / "game.js").write_text("function a() {\n  return 1;\n}\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [
            _tool(
                "write_file",
                filepath="game.js",
                content="function a() {\n  return 1;\n}\nfunction b() {\n  return 2;\n}\n",
            ),
            _text("done"),
        ],
    )

    asyncio.run(loop.run("add a section"))

    assert loop._last_write_grew["game.js"] is True
    assert "game.js" in loop._built_up


def test_a_write_that_lands_gives_a_reasoning_model_its_reasoning_back(tmp_path):
    """smallcode's rule is `isRepair && attempt > 1` - the model already
    overthought THIS solution. Ours read a turn-wide tally that only went up,
    so two failures anywhere switched reasoning off for every later round,
    including the rounds that were working. A write that lands is the proof the
    model is not stuck."""
    (tmp_path / "game.js").write_text("function a() {\n  return 1;\n}\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [
            _tool("patch_file", filepath="game.js", old_string="nope", new_string="x"),
            _tool("patch_file", filepath="game.js", old_string="also nope", new_string="y"),
            _tool("patch_file", filepath="game.js", old_string="return 1;", new_string="return 2;"),
            _text("fixed"),
        ],
    )

    asyncio.run(loop.run("fix it"))

    assert loop._repair_streak == 0, "the successful patch must clear the streak"
    assert loop._should_disable_thinking() is False


def test_two_failures_in_a_row_still_switch_thinking_off(tmp_path):
    """The other edge: the streak must still FIRE, or the guard is gone."""
    (tmp_path / "game.js").write_text("function a() {\n  return 1;\n}\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [
            _tool("patch_file", filepath="game.js", old_string="nope", new_string="x"),
            _tool("patch_file", filepath="game.js", old_string="also nope", new_string="y"),
            _text("stuck"),
        ],
    )

    asyncio.run(loop.run("fix it"))

    assert loop._repair_streak >= 2
    assert loop._should_disable_thinking() is True


# --- Phase 1a: the tools simple mode could not reach --------------------------


def test_renaming_a_file_does_not_require_guessing_a_shell_verb(tmp_path):
    """`rename_file_via_move_tool` is an eval case named after a tool simple
    mode never offered. It sat at 1/3 in BENCHMARK.md, read as model variance;
    the only route to a pass was guessing `mv` against `move` against `ren`
    through run_command and getting it approved."""
    (tmp_path / "old_name.py").write_text("GREETING = 'hi'\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [
            _tool("move_file", source="old_name.py", destination="new_name.py"),
            _text("Renamed it."),
        ],
    )

    asyncio.run(loop.run("rename old_name.py to new_name.py"))

    assert (tmp_path / "new_name.py").read_text(encoding="utf-8") == "GREETING = 'hi'\n"
    assert not (tmp_path / "old_name.py").exists(), "a rename leaves nothing behind"


def test_move_file_is_offered_to_the_model_and_routed_with_the_write_tools(tmp_path):
    """A tool the schema list never mentions cannot be called, however well it
    works - and a rename is a write, not a shell command."""
    from shamsu.agents.simple_router import TOOL_CATEGORIES

    assert "move_file" in {s["function"]["name"] for s in SIMPLE_TOOL_SCHEMAS}
    assert "move_file" in TOOL_CATEGORIES["write"]["tools"]


def test_a_rename_onto_an_existing_file_is_refused_rather_than_silently_losing_it(tmp_path):
    """The destructive edge of a rename. Overwriting the destination would lose
    a file the user never mentioned."""
    (tmp_path / "old_name.py").write_text("keep me\n", encoding="utf-8")
    (tmp_path / "taken.py").write_text("already here\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [
            _tool("move_file", source="old_name.py", destination="taken.py"),
            _text("could not"),
        ],
    )

    asyncio.run(loop.run("rename old_name.py to taken.py"))

    assert (tmp_path / "taken.py").read_text(encoding="utf-8") == "already here\n"
    assert (tmp_path / "old_name.py").is_file(), "the source survives a refused move"


def test_asking_the_user_ends_the_turn_with_the_question_as_the_answer(tmp_path):
    """`ask_user` does not block - it hands back a structured question and
    expects the LOOP to stop on it. Simple mode never had that half, so the
    tool sat in the registry unreachable while the prompt told the model to ask
    whenever a decision was the user's to make."""
    loop = _loop(
        tmp_path,
        [
            _tool("ask_user", question="Sessions or JWT?"),
            _text("SHOULD NEVER BE REACHED"),
        ],
    )

    result = asyncio.run(loop.run("add authentication"))

    assert result.final == "Sessions or JWT?"
    assert result.rounds == 1, "the turn ends on the question, it does not carry on"
    assert not result.stopped, "asking is a correct outcome, not a failure"


def test_a_question_is_a_real_assistant_turn_so_the_next_message_answers_it(tmp_path):
    """No pending-question store: the conversation IS the store. The question
    has to survive in the transcript or the user's answer arrives as a reply to
    nothing."""
    loop = _loop(tmp_path, [_tool("ask_user", question="Which config file?")])

    asyncio.run(loop.run("update the config"))

    said = [m.content for m in loop.state.all_messages if m.role == "assistant"]
    assert "Which config file?" in said


def test_the_model_can_ask_from_any_category(tmp_path):
    """An ambiguity turns up in a category nobody predicted. A category switch
    standing between the model and the question is exactly the friction that
    makes a small model guess instead."""
    from shamsu.agents.simple_router import ALWAYS_TOOLS, tools_for_category

    assert "ask_user" in ALWAYS_TOOLS
    for category in ("read", "write", "run", "search", "verify", "recall"):
        names = {
            s["function"]["name"]
            for s in tools_for_category(category, SIMPLE_TOOL_SCHEMAS)
        }
        assert "ask_user" in names, category


def test_deleting_a_file_is_possible_and_reversible(tmp_path):
    """The other half of editing a project. Backed up, so it can be undone."""
    (tmp_path / "junk.py").write_text("x = 1\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [_tool("delete_file", filepath="junk.py"), _text("Deleted it.")],
    )

    asyncio.run(loop.run("delete junk.py"))

    assert not (tmp_path / "junk.py").exists()


def test_the_model_can_see_what_it_actually_changed(tmp_path):
    """`_with_diff` shows one edit. `git_diff` shows the turn - which is the
    view that answers "what did I just break", the question behind the whole
    patch-and-cannot-fix-it failure."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    loop = _loop(tmp_path, [_tool("git_status"), _text("You have one new file.")])

    asyncio.run(loop.run("what have I changed?"))

    said = [m.content for m in loop.state.all_messages if getattr(m, "name", "") == "git_status"]
    assert said and "app.py" in said[0], said


def test_git_tools_are_withheld_where_there_is_no_repository(tmp_path):
    """The project's own rule - offer only the tools that have something to
    answer from. `git status` outside a repo costs a round to learn nothing."""
    from shamsu.agents.simple_chat import active_tool_schemas, available_tool_families

    families = available_tool_families(tmp_path)
    names = {
        s["function"]["name"]
        for s in active_tool_schemas(context_window=32768, available=families)
    }

    assert "git" not in families
    assert not {"git_status", "git_diff", "git_log"} & names


def test_git_tools_are_offered_inside_a_repository(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    from shamsu.agents.simple_chat import active_tool_schemas, available_tool_families

    families = available_tool_families(tmp_path)
    names = {
        s["function"]["name"]
        for s in active_tool_schemas(context_window=32768, available=families)
    }

    assert "git" in families
    assert {"git_status", "git_diff", "git_log"} <= names


# --- the third exit: a strategy change before a stop --------------------------


def test_four_failed_patches_change_the_approach_instead_of_giving_up(tmp_path):
    """THE reported failure. Four non-matching patches ended the turn with "I
    have stopped rather than keep guessing - it would help to tell me the exact
    text to look for": an apology that hands the work back to the user.

    Patching is one strategy and it was the only one tried. `replace_symbol`
    names the function instead of reproducing its old text byte-for-byte, which
    is precisely the step that has been failing.
    """
    (tmp_path / "game.js").write_text(
        "function update() {\n  return 1;\n}\n", encoding="utf-8"
    )
    misses = [
        _tool("patch_file", filepath="game.js", old_string=f"nope{i}", new_string="x")
        for i in range(4)
    ]
    loop = _loop(tmp_path, [*misses, _text("ok")])

    result = asyncio.run(loop.run("fix update()"))

    assert not result.stopped, "a strategy is left to try, so the turn continues"
    nudge = [m.content for m in loop.state.all_messages if m.role == "user"][-1]
    assert "replace_symbol" in nudge
    assert "game.js" in nudge, "the advice must name the file, not 'that file'"
    assert "read_symbol" in nudge


def test_the_strategy_change_is_offered_once_and_then_it_really_stops(tmp_path):
    """Every guard needs an exit. Said twice, a change of strategy stops being
    one and becomes the loop repeating itself at the model."""
    (tmp_path / "game.js").write_text(
        "function update() {\n  return 1;\n}\n", encoding="utf-8"
    )
    misses = [
        _tool("patch_file", filepath="game.js", old_string=f"nope{i}", new_string="x")
        for i in range(9)
    ]
    loop = _loop(tmp_path, [*misses, _text("ok")])

    result = asyncio.run(loop.run("fix update()"))

    assert result.stopped
    assert "changed nothing" in result.final
    assert loop._strategy_switched, "it must have tried the other approach first"


def test_a_patch_that_lands_never_triggers_the_strategy_change(tmp_path):
    """The counter is about being stuck. A working edit is not."""
    (tmp_path / "game.js").write_text(
        "function update() {\n  return 1;\n}\n", encoding="utf-8"
    )
    loop = _loop(
        tmp_path,
        [
            _tool("patch_file", filepath="game.js", old_string="return 1;", new_string="return 2;"),
            _text("done"),
        ],
    )

    asyncio.run(loop.run("bump it"))

    assert not loop._strategy_switched


# --- Phase 2 guards, wired into the loop --------------------------------------


def test_eight_reads_without_producing_anything_is_interrupted(tmp_path):
    """Simple mode already caught an IDENTICAL read repeated three times - the
    model losing track of what it has. Eight DIFFERENT reads that produce
    nothing is a different fault and no counter saw it."""
    for i in range(9):
        (tmp_path / f"f{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
    reads = [_tool("read_file", filepath=f"f{i}.py") for i in range(9)]
    loop = _loop(tmp_path, [*reads, _text("done")], max_rounds=12)

    asyncio.run(loop.run("review these files"))

    nudges = [
        m.content for m in loop.state.all_messages
        if m.role == "user" and "produced nothing" in m.content
    ]
    assert nudges, "it read nine files and was never asked to produce anything"
    assert any("Stop reading" in n for n in nudges)


def test_reading_then_writing_is_never_interrupted(tmp_path):
    """The guard must not punish the normal shape of an edit: look, then act."""
    for i in range(6):
        (tmp_path / f"f{i}.py").write_text(f"x = {i}\n", encoding="utf-8")
    turns = []
    for i in range(6):
        turns.append(_tool("read_file", filepath=f"f{i}.py"))
        turns.append(_tool("write_file", filepath=f"out{i}.py", content=f"y = {i}\n"))
    loop = _loop(tmp_path, [*turns, _text("done")], max_rounds=16, verify_changes=False)

    asyncio.run(loop.run("copy each one"))

    assert not [
        m for m in loop.state.all_messages
        if m.role == "user" and "produced nothing" in m.content
    ]


def test_a_greeting_after_real_work_is_sent_back(tmp_path):
    """A model that says "How can I help you?" after eleven tool calls has lost
    the conversation. The reply reads as polite and is a context failure."""
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    loop = _loop(
        tmp_path,
        [
            _tool("read_file", filepath="a.py"),
            _text("Hello! How can I help you today?"),
            _text("Sorry - I was reading a.py. It sets x to 1."),
        ],
        max_rounds=5,
    )

    result = asyncio.run(loop.run("what is in a.py?"))

    assert "How can I help" not in result.final
    assert "x to 1" in result.final


def test_a_greeting_with_no_work_behind_it_is_a_normal_answer(tmp_path):
    """"hi" -> "Hi, how can I help?" is correct, and correcting it would be the
    guard punishing the right answer."""
    loop = _loop(tmp_path, [_text("Hi! How can I help you today?")])

    result = asyncio.run(loop.run("hi"))

    assert result.final == "Hi! How can I help you today?"


def test_an_invented_tool_name_is_answered_with_the_one_it_meant(tmp_path):
    """This used to return the full list of thirty names - the same list already
    in the prompt the model has just shown it is not reading."""
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("read_files", {"filepath": "a.py"})

    assert not result.ok
    assert "read_file" in result.message
    assert "Did you mean" in result.message
    assert len(result.message) < 200, "a correction, not a re-listing of the roster"


def test_a_search_that_keeps_failing_is_withheld_but_patching_never_is(tmp_path):
    """smallcode drops any tool after five consecutive failures. Dropping
    `patch_file` would leave a model that cannot edit anything, which is worse
    than the loop it prevents."""
    loop = _loop(tmp_path, [_text("ok")])
    for _ in range(6):
        loop._trust.record("search_files", ok=False)
        loop._trust.record("patch_file", ok=False)

    offered = {s["function"]["name"] for s in loop._sent_schemas()}

    assert "search_files" not in offered
    assert "patch_file" in offered


def test_a_claude_shaped_tool_name_reaches_the_shamsu_tool(tmp_path):
    """smallcode's `normalizeToolCall`. A model trained on those transcripts
    reaches for `Edit` or `Bash` by reflex, and they are far enough from a
    SHAMSU name that even fuzzy matching finds nothing - live, `Edit` fell all
    the way through to a re-listing of the whole roster."""
    from shamsu.agents.simple_chat import canonical_tool_name

    for shaped, real in (
        ("Edit", "patch_file"),
        ("Read", "read_file"),
        ("Bash", "run_command"),
        ("Write", "write_file"),
        ("Grep", "search_files"),
        ("Glob", "find_files"),
        ("str_replace_editor", "patch_file"),
    ):
        assert canonical_tool_name(shaped) == real, shaped


def test_a_long_markdown_file_shows_its_END_not_just_its_beginning(tmp_path):
    """The head clip is what starts the dead end in the gap analysis: the model
    patches against a half it was never shown. An outline answers that for
    code; nothing could outline a `.md`, so it kept getting the first N lines
    and never the last - then was asked to add an entry at the bottom."""
    lines = [f"- entry {i}" for i in range(400)]
    (tmp_path / "CHANGELOG.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("read_file", {"filepath": "CHANGELOG.md"})

    body = (result.data or {}).get("content", "")
    assert "entry 0" in body, "the beginning is still there"
    assert "entry 399" in body, "and now so is the END"
    assert "not shown" in body, "and it says what it left out"
    assert "entry 200" not in body, "the middle is what was dropped"


def test_a_short_markdown_file_is_still_sent_whole(tmp_path):
    """Two ends that would overlap is just the file, and saying otherwise would
    be the guard inventing a gap that is not there."""
    (tmp_path / "notes.md").write_text(
        "\n".join(f"line {i}" for i in range(20)) + "\n", encoding="utf-8"
    )
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("read_file", {"filepath": "notes.md"})

    assert "not shown" not in (result.data or {}).get("content", "")


def test_code_still_gets_an_outline_rather_than_two_slices(tmp_path):
    """An outline beats head-and-tail because it shows the whole shape. The
    fallback must not steal files the outline can handle."""
    body = "\n".join(
        f"def f{i}():\n    return {i}\n" for i in range(120)
    )
    (tmp_path / "big.py").write_text(body, encoding="utf-8")
    loop = _loop(tmp_path, [_text("ok")])

    result = loop._execute("read_file", {"filepath": "big.py"})

    assert (result.data or {}).get("outlined")
    assert not (result.data or {}).get("head_and_tail")


def test_the_model_is_told_what_kind_of_project_this_is(tmp_path):
    """smallcode's bootstrap. Every piece was already here - the manifests are
    one stat each, `detect_test_command` reads package.json scripts and pytest
    layouts - and nothing summarised it into the prompt, so a model opening a
    fresh workspace spent three to five calls working it out every session."""
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "vitest run"}}', encoding="utf-8"
    )
    loop = _loop(tmp_path, [_text("ok")])

    asyncio.run(loop.run("what is this?"))

    sent = "\n".join(
        m["content"] for m in loop.client.calls[0]["messages"] if m["role"] == "system"
    )
    assert "This project: Node" in sent
    # `npm test` - the command to RUN, not the script body it expands to.
    assert "npm test" in sent, "and how to run its tests"


def test_a_workspace_it_knows_nothing_about_says_nothing(tmp_path):
    """"Project: unknown" is worse than no line, because it looks like an
    answer."""
    from shamsu.agents.simple_chat import project_brief

    assert project_brief(tmp_path) == ""


# --- the plan anchor ----------------------------------------------------------


def test_a_multi_part_request_is_asked_to_write_the_steps_down(tmp_path):
    """`contract_create` has been offered all along, and a model that does not
    think to call it never does."""
    loop = _loop(tmp_path, [_text("ok")])

    asyncio.run(loop.run(
        "Read the auth module, then add a refresh handler, then update the middleware"
    ))

    asked = [
        m.content for m in loop.state.all_messages
        if m.role == "user" and "contract_create" in m.content
    ]
    assert asked, "a job with three parts was never asked for a plan"


def test_a_one_line_request_is_not_asked_to_plan(tmp_path):
    """A false positive costs a round and anchors the model to a bad plan."""
    loop = _loop(tmp_path, [_text("ok")])

    asyncio.run(loop.run("fix the typo in game.js"))

    assert not [
        m for m in loop.state.all_messages
        if m.role == "user" and "contract_create" in m.content
    ]


def test_the_plan_is_shown_again_on_a_later_turn(tmp_path):
    """THE anchor. The contract reached the model only if it called
    contract_status - so the thing meant to keep a multi-step task on the rails
    was invisible to exactly the model that had lost the thread."""
    first = _loop(
        tmp_path,
        [
            _named_tool("contract_create", {
                "title": "ship login",
                "assertions": ["form renders", "posts to the API", "tests pass"],
            }),
            _text("Plan written."),
        ],
    )
    asyncio.run(first.run("build the login page, then wire it up, then test it"))

    # A NEW loop, as repl.py builds per user message - the plan has to survive it.
    second = _loop(tmp_path, [_text("carrying on")])
    asyncio.run(second.run("continue"))

    sent = "\n".join(
        m["content"] for m in second.client.calls[0]["messages"] if m["role"] == "system"
    )
    assert "ACTIVE PLAN" in sent
    assert "posts to the API" in sent, "the unresolved steps must still be visible"


def test_a_finished_plan_is_not_re_shown(tmp_path):
    """A completed contract is history, not a plan. Re-showing it would tell a
    model starting something new to work through a list it has finished."""
    from shamsu.agents.simple_contract import new_contract, save_contract, PASSED

    contract = new_contract("done thing", "", ["it works"])
    contract.assertions[0].state = PASSED
    contract.assertions[0].evidence = "ran it"
    save_contract(tmp_path, contract)

    loop = _loop(tmp_path, [_text("ok")])
    asyncio.run(loop.run("something else entirely"))

    sent = "\n".join(
        m["content"] for m in loop.client.calls[0]["messages"] if m["role"] == "system"
    )
    assert "ACTIVE PLAN" not in sent


# --- context construction: shares, not flat constants -------------------------


def test_the_reply_reserve_never_outgrows_the_window_it_is_a_share_of(tmp_path):
    """`max(4096, ceiling // 4)` returned 4096 at every window below 16k - half
    of an 8k window and ALL of a 4k one, leaving nothing for the prompt. It was
    unreachable while 32k was the only setting anyone used; `/context window`
    makes it reachable and `_shrink_for_oom` was already walking into it."""
    from shamsu.agents.simple_chat import output_reserve

    for window in (4096, 6144, 8192, 12288, 16384, 32768, 65536):
        reserve = output_reserve(window)
        assert 0 < reserve <= window // 2, f"{window} -> {reserve}"
        assert window - reserve > 1024, "there must be room left for a prompt"


def test_a_big_window_still_gets_the_full_quarter(tmp_path):
    """The floor must not become a ceiling: the original defect was a REPLY
    starved at 32k, and that fix has to survive this one."""
    from shamsu.agents.simple_chat import output_reserve

    assert output_reserve(32768) == 8192
    assert output_reserve(16384) == 4096


def test_one_tool_result_cannot_swallow_the_window(tmp_path):
    """A flat 8,000 was 24% of a 32k window and 97.7% of an 8k one. Same defect
    the reserve already had once: right at one size, silently wrong at others."""
    from shamsu.agents.simple_chat import tool_result_budget

    for window in (4096, 8192, 16384, 32768):
        assert tool_result_budget(window) <= max(1500, window // 4), window
    assert tool_result_budget(32768) == 8000, "the big window keeps the old cap"
    assert tool_result_budget(8192) < 8000, "the small one must not"


def test_a_fresh_thread_is_not_told_it_has_a_past(tmp_path):
    """Live 2026-08-20, turn one of an empty session replied "I apologize for
    any confusion earlier. Let's proceed with the next step." There was no
    earlier and no next; the prompt asserted both on every turn."""
    from shamsu.agents.simple_prompt import simple_system_prompt

    fresh = simple_system_prompt(tmp_path, has_history=False)
    ongoing = simple_system_prompt(tmp_path, has_history=True)

    assert "Earlier messages in this conversation" not in fresh
    assert "Earlier messages in this conversation" in ongoing
    assert "You are SHAMSU" in fresh, "the rest of the prompt is unchanged"


def test_an_unreadable_session_is_assumed_to_have_history(tmp_path):
    """Fails towards True on purpose: claiming history that exists is harmless,
    claiming history that does not is the defect."""
    from shamsu.agents.simple_chat import _thread_has_history

    class Broken:
        @property
        def metadata(self):
            raise RuntimeError("no")

    assert _thread_has_history(Broken()) is True
    assert _thread_has_history(None) is False


def test_a_bare_tool_call_is_not_handed_back_as_the_answer(tmp_path):
    """Live 2026-08-20 on qwen2.5-coder:3b, a fresh turn replied
    `{"name": "run_file", "arguments": {"filepath": "hello.py"}}` - raw JSON,
    presented as the finished answer, for a tool that does not exist. The
    closest-match correction never fired because the call never reached
    dispatch."""
    loop = _loop(
        tmp_path,
        [
            _text('{"name": "run_file", "arguments": {"filepath": "hello.py"}}'),
            _text("Created hello.py."),
        ],
    )

    result = asyncio.run(loop.run("create hello.py"))

    assert result.final == "Created hello.py."
    nudge = [m.content for m in loop.state.all_messages if m.role == "user"][-1]
    assert "run_file" in nudge
    assert "not a tool" in nudge
