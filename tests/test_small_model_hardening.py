"""The three defects that let a change request be answered in prose.

All of them were invisible on qwen3.5:9b and fatal on a 7B, which is why they
shipped: the harness nets were measured on the larger model, where they are a
safety margin, and are load-bearing on the smaller ones this project targets.
"""
from __future__ import annotations

from shamsu.agents.simple_chat import (
    active_tool_schemas,
    answered_a_change_request_with_prose,
    describes_an_unmade_edit,
)
from shamsu.agents.tool_classifier import categories_for

WRITE_TOOLS = {"write_file", "append_file", "patch_file", "replace_symbol"}


def _roster(request: str) -> set[str]:
    return {
        schema["function"]["name"]
        for schema in active_tool_schemas(request=request, context_window=32768)
    }


# --- the roster could withhold the write tools outright ------------------------

def test_build_me_a_thing_can_still_write_it():
    """The exact shipped failure. "build me a snake game" scored `run` 2.5
    against `write` 0.0 - confidently, at 0.833 - because the write signal
    required "build" to be followed literally by "a"."""
    for request in (
        "build me a snake game",
        "build me a todo app",
        "can you build me a dashboard",
        "build the API",
        "build me a website with login",
        "build out the menu system",
    ):
        assert WRITE_TOOLS & _roster(request), request


def test_a_looking_request_can_still_act_on_what_it_finds():
    """A pasted traceback scores `read` on the `.js` alone and means "fix this".
    Live 2026-08-24 exactly that turn went on to patch eight files."""
    request = "menu.js:198 Uncaught ReferenceError: module is not defined"
    assert WRITE_TOOLS & _roster(request)


def test_planning_is_still_the_one_thing_that_cannot_write():
    """The exemption is the category's whole point: a planning turn that can
    call write_file is a normal turn with a different label."""
    for request in ("plan how to add authentication", "outline the approach first"):
        assert "write" not in categories_for(request), request
        assert not (WRITE_TOOLS & _roster(request)), request


def test_a_planning_request_too_short_to_score_still_gets_everything():
    """Pre-existing and deliberately left alone. "plan the refactor first" is 23
    characters, under SHORT_MESSAGE_CHARS, and `plan` is not one of the words
    that make a short message a task - so it scores nothing, and a no-idea
    verdict has always meant "send everything" rather than "send nothing"."""
    assert categories_for("plan the refactor first") == ()
    assert WRITE_TOOLS & _roster("plan the refactor first")


def test_it_still_narrows():
    """Not a licence to send everything - the token saving is the point."""
    everything = _roster("")
    assert len(_roster("run the tests")) < len(everything)


# --- the safety net needed the model to name a file ---------------------------

_REPLY_NAMING_NOTHING = """Sure, I can modify the file to print 200 prime numbers.
Here is the updated code:

```python
def show(count):
    n, seen = 2, 0
    while seen < count:
        print(n)
        n += 1

show(200)
```

Would you like me to write this updated code to the file?
"""


def test_the_old_detector_is_blind_when_no_file_is_named():
    """Not a bug being asserted as correct - the boundary being pinned. This is
    why the new detector exists, and the next test is the same reply passing."""
    assert describes_an_unmade_edit(_REPLY_NAMING_NOTHING, ["primes.py"]) == ""


def test_a_change_request_answered_in_prose_is_caught_without_a_filename():
    assert answered_a_change_request_with_prose(
        _REPLY_NAMING_NOTHING, "Can you modify the file and make it print 200 numbers?"
    )


def test_a_planning_request_is_never_nudged_into_writing():
    """`plan` and `write` both score 3.0 on this, and PRIORITY breaks the tie
    toward `write`, so the winner cannot be trusted and the scores are compared
    directly. Getting this wrong would turn a plan into an unasked-for edit."""
    for request in (
        "plan how you would change it",
        "plan how to add authentication",
        "how should we approach the refactor?",
    ):
        assert not answered_a_change_request_with_prose(_REPLY_NAMING_NOTHING, request), request


def test_a_question_is_not_a_change_request():
    for request in ("what does this file do?", "review the modules", "run the tests"):
        assert not answered_a_change_request_with_prose(_REPLY_NAMING_NOTHING, request), request


def test_prose_without_code_is_not_a_withheld_edit():
    assert not answered_a_change_request_with_prose("I would change the loop.", "modify the file")


# --- harness corrections must not outlive the turn that caused them -----------

def test_loop_nudges_are_dropped_at_the_start_of_the_next_turn():
    """Live 2026-08-28 they were not, and the model copied them: four snake-game
    requests answered with byte-identical replies, against a transcript that
    read as a user repeatedly complaining."""
    from shamsu.agents.chat_state import ChatState
    from shamsu.session.manager import ORIGIN_LOOP

    state = ChatState("sys", session_logger=None, hydrate=False)
    state.append_user("write me a snake game")
    state.append_assistant("")
    state.append_user("That reply was empty. Answer the question, or call one tool.",
                      origin=ORIGIN_LOOP)
    state.append_assistant("here is the game")

    dropped = state.drop_stale_loop_messages()

    assert dropped == 2, "the nudge and the empty assistant turn it was paired with"
    kept = [(m.role, m.content) for m in state._messages]
    assert ("user", "write me a snake game") in kept
    assert ("assistant", "here is the game") in kept
    assert not any("That reply was empty" in content for _role, content in kept)


def test_dropping_nudges_leaves_a_clean_transcript_alone():
    from shamsu.agents.chat_state import ChatState

    state = ChatState("sys", session_logger=None, hydrate=False)
    state.append_user("write me a snake game")
    state.append_assistant("done")
    assert state.drop_stale_loop_messages() == 0


# --- a write that lands in another file's namespace ---------------------------

def test_a_write_reports_a_symbol_another_file_already_declares(tmp_path):
    """The openbazar bug, asked as a question instead of debugged as an error:
    `Identifier 'GameState' has already been declared` cost four turns."""
    from shamsu.agents.simple_chat import SimpleChatLoop

    (tmp_path / "collision.js").write_text("class GameState {}\n", encoding="utf-8")
    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.workspace = tmp_path

    assert loop._symbols_now_declared_twice("game.js", "class GameState {}\n") == [
        ("GameState", "collision.js")
    ]


def test_a_write_of_its_own_symbols_is_not_a_conflict(tmp_path):
    """Rewriting a file must not report the file against itself."""
    from shamsu.agents.simple_chat import SimpleChatLoop

    (tmp_path / "game.js").write_text("class GameState {}\n", encoding="utf-8")
    loop = SimpleChatLoop.__new__(SimpleChatLoop)
    loop.workspace = tmp_path

    assert loop._symbols_now_declared_twice("game.js", "class GameState {}\n") == []


# --- the skill index is rent unless the workspace could use it ----------------

def test_the_skill_index_drops_stack_skills_a_workspace_cannot_use(tmp_path):
    """`use_skill` was called zero times across every session logged to
    2026-08-28, while its index shipped on every turn."""
    from shamsu.agents.simple_chat import skill_index

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    index = skill_index(tmp_path)
    assert "react-vite" not in index
    assert "sql-databases" not in index


def test_the_skill_index_keeps_stack_skills_a_workspace_can_use(tmp_path):
    from shamsu.agents.simple_chat import skill_index

    (tmp_path / "App.tsx").write_text("export default function App(){}\n", encoding="utf-8")
    assert "react-vite" in skill_index(tmp_path)


def test_the_skill_index_is_keyed_on_the_workspace_not_the_request(tmp_path):
    """Keyed on the request it would move the head of the KV prefix every turn,
    and re-prefilling the whole system prompt to save 147 tokens is a loss."""
    from shamsu.agents.simple_chat import skill_index

    (tmp_path / "main.py").write_text("x = 1\n", encoding="utf-8")
    assert skill_index(tmp_path) == skill_index(tmp_path)


# --- what the window says about files the request did not name ----------------

def test_the_skeleton_names_what_other_files_declare(tmp_path):
    """`codebase_brief` covers files the request NAMED, at most three. The
    collisions were always in the ones it did not name."""
    from shamsu.agents.simple_chat import workspace_skeletons

    (tmp_path / "collision.js").write_text("class GameState {}\n", encoding="utf-8")
    (tmp_path / "menu.js").write_text("function draw() {}\n", encoding="utf-8")

    brief = workspace_skeletons(tmp_path, ["collision.js", "menu.js"], 400)

    assert "GameState" in brief and "collision.js" in brief
    assert "draw" in brief and "menu.js" in brief


def test_the_skeleton_is_empty_for_a_greenfield_workspace(tmp_path):
    """Nothing to compile, so nothing is paid for."""
    from shamsu.agents.simple_chat import workspace_skeletons

    assert workspace_skeletons(tmp_path, [], 400) == ""


def test_the_skeleton_respects_its_budget(tmp_path):
    """On a small window this must degrade to a few files, not crowd out the
    conversation it exists to inform."""
    from shamsu.agents.simple_chat import count_tokens, workspace_skeletons

    for n in range(20):
        (tmp_path / f"mod{n}.py").write_text(
            f"def alpha_{n}():\n    pass\n\n\ndef beta_{n}():\n    pass\n", encoding="utf-8"
        )
    files = [f"mod{n}.py" for n in range(20)]

    generous = workspace_skeletons(tmp_path, files, 4000)
    tight = workspace_skeletons(tmp_path, files, 40)

    assert count_tokens(tight) <= count_tokens(generous)
    assert count_tokens(tight) < 120, "a tight budget must actually bind"


def test_the_skeleton_skips_files_with_nothing_to_declare(tmp_path):
    from shamsu.agents.simple_chat import workspace_skeletons

    (tmp_path / "notes.txt").write_text("just prose\n", encoding="utf-8")
    assert workspace_skeletons(tmp_path, ["notes.txt"], 400) == ""
