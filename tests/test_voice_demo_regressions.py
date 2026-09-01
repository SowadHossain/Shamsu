"""Three defects one live session exposed, in `F:\\voice-demo` on 2026-08-31.

Asked to build an asteroid game, the harness ran five turns over two hours and
produced a game that showed the victory screen the moment you pressed Start.
Every guard in the harness fired correctly and the run still failed, which makes
it the most useful session this project has recorded.

* **The contract was void from the first minute.** `contract_create` was called
  with `assertions` as a printed Python list - one string - and the harness
  stored the blob as a single assertion. Eight requirements became one
  unsatisfiable claim that stayed `pending` for the whole session, so the only
  mechanism that answers "did it do what was asked" was inert and nothing could
  have noticed.
* **A correct diagnosis nobody could act on.** The wiring verifier found the
  duplicate `SoundManager` and reported it four times; the model answered with
  26 refused edits and four turns ending "I tried 4 edits in a row that changed
  nothing". The message named the fault and never the next call.
* **A file written and never loaded.** Asked to split the JS, the agent wrote
  `sounds.js` and left `index.html` loading `game.js` alone. Nothing 404s and
  nothing throws - the file simply never runs.

And one defect in the skill matcher the same session exposed: `ui` matched
inside "b**ui**ld", so a game build scored `ui-designer`.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from shamsu.agents.simple_chat import normalize_arguments
from shamsu.agents.simple_skills import _mentions, _score, best_skill
from shamsu.verify.wiring import verify_wiring

#: Exactly what the model sent on 2026-08-31 at 00:07:32.
LIVE_ASSERTIONS = (
    "['Game has a main menu with start button', 'Player ship can move with "
    "arrow keys and shoot with spacebar', 'Asteroids spawn and move across the "
    "screen', 'Player collects points by destroying asteroids', 'Game has at "
    "least 3 difficulty levels', 'Sound effects play on shoot, explosion, and "
    "level complete', 'Game ends when player loses all lives or completes all "
    "levels', 'High score is saved to local storage']"
)


# -- a printed list is still a list -----------------------------------------


def test_the_live_contract_payload_becomes_eight_assertions():
    """The regression, byte for byte. This produced ONE assertion."""
    out = normalize_arguments(
        "contract_create",
        {"title": "Asteroid Game Project", "brief": "b", "assertions": LIVE_ASSERTIONS},
    )
    assertions = out["assertions"]
    assert isinstance(assertions, list)
    assert len(assertions) == 8
    assert assertions[0] == "Game has a main menu with start button"
    assert assertions[-1] == "High score is saved to local storage"


def test_a_json_array_in_a_string_is_recovered_too():
    out = normalize_arguments("contract_create", {"assertions": '["a", "b"]'})
    assert out["assertions"] == ["a", "b"]


def test_a_real_list_is_left_exactly_as_it_is():
    out = normalize_arguments("contract_create", {"assertions": ["a", "b"]})
    assert out["assertions"] == ["a", "b"]


@pytest.mark.parametrize(
    "value",
    [
        "just one claim",  # a single assertion as a plain string is legal
        "[not a list at all",  # unbalanced
        "[",
        "",
        "the array [1,2] appears mid-sentence",
    ],
)
def test_anything_that_is_not_a_printed_list_is_untouched(value):
    assert normalize_arguments("contract_create", {"assertions": value})["assertions"] == value


def test_the_same_coercion_covers_the_other_array_arguments():
    """One fix in one place: `ask_user.options` and `memory_remember.tags` take
    arrays and get the same treatment from the same models."""
    assert normalize_arguments("ask_user", {"options": "['a','b']"})["options"] == ["a", "b"]
    assert normalize_arguments("memory_remember", {"tags": "['x']"})["tags"] == ["x"]


def test_a_tool_with_no_array_arguments_is_not_touched():
    weird = "['a','b']"
    assert normalize_arguments("read_file", {"filepath": weird})["filepath"] == weird


# -- the diagnostics name the next call --------------------------------------


def _project(files: dict[str, str]):
    root = Path(tempfile.mkdtemp())
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return verify_wiring(root)


def test_a_duplicate_class_says_which_copy_to_delete_and_how():
    result = _project(
        {
            "index.html": '<html><script src="game.js"></script></html>',
            "game.js": "class SoundManager {}\n",
            "sounds.js": "class SoundManager {}\n",
        }
    )
    duplicate = next(d for d in result.diagnostics if d.kind == "js_redeclaration")
    assert "patch_file" in duplicate.message
    # Names BOTH files, so "delete the class" cannot be read as the wrong copy -
    # deleting the only copy is what `replace_symbol` then refuses, which is the
    # loop this whole message exists to break.
    assert "game.js" in duplicate.message and "sounds.js" in duplicate.message
    assert "Do not edit both copies" in duplicate.message


def test_a_script_no_page_loads_is_reported_with_the_fix():
    result = _project(
        {
            "index.html": '<html><script src="game.js"></script></html>',
            "game.js": "const A = 1;\n",
            "sounds.js": "const B = 2;\n",
        }
    )
    orphan = next(d for d in result.diagnostics if d.kind == "unreferenced_script")
    assert orphan.file == "sounds.js"
    assert "never loaded" in orphan.message
    assert "index.html" in orphan.message and "patch_file" in orphan.message


@pytest.mark.parametrize(
    ("label", "files"),
    [
        (
            "a bundler project reaches modules without a script tag",
            {
                "index.html": '<html><script type="module" src="/src/main.js"></script></html>',
                "src/main.js": 'import {x} from "./player.js";\n',
                "src/player.js": "export const x = 1;\n",
            },
        ),
        ("a library has no page to be loaded by", {"lib.js": "const A = 1;\n"}),
        (
            "a page with no scripts owes nothing a tag",
            {"index.html": "<html><p>hi</p></html>", "a.js": "const A = 1;\n"},
        ),
        (
            "every script is loaded",
            {
                "index.html": '<html><script src="a.js"></script><script src="b.js"></script></html>',
                "a.js": "const A = 1;\n",
                "b.js": "const B = 2;\n",
            },
        ),
    ],
)
def test_the_orphan_check_stays_quiet_where_it_should(label, files):
    result = _project(files)
    assert not [d for d in result.diagnostics if d.kind == "unreferenced_script"], label


# -- a trigger is a word, not a substring ------------------------------------


class _Skill:
    def __init__(self, name, triggers=(), tags=(), instructions="body", budget=900):
        self.name = name
        self.triggers = triggers
        self.tags = tags
        self.instructions = instructions
        self.context_budget_tokens = budget


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("lets build an asteroid game", "ui"),  # b-UI-ld, the live one
        ("the latest version", "test"),
        ("take them apart", "part"),
        ("a quick guide", "ui"),
    ],
)
def test_a_trigger_does_not_match_inside_another_word(text, phrase):
    assert not _mentions(text, phrase)


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("fix the ui layout", "ui"),
        ("set up react-vite here", "react-vite"),
        ("work through it part by part", "part by part"),
        ("UI needs work", "ui"),
    ],
)
def test_a_real_mention_still_matches(text, phrase):
    assert _mentions(text.lower(), phrase)


def test_the_live_game_request_no_longer_pulls_in_a_page_layout_skill():
    """It scored ui-designer 3.0 purely on the "ui" inside "build"."""
    ui = _Skill("ui-designer", triggers=("ui", "design", "responsive"))
    assert _score(ui, "let's build an asteroid game with sound effects") == 0.0
    assert best_skill([ui], "let's build an asteroid game with sound effects") is None


def test_a_trigger_with_regex_characters_is_matched_literally():
    """Triggers are author text, not patterns - one must never raise."""
    skill = _Skill("dotnet", triggers=("c++", ".net"))
    assert _mentions("porting the c++ layer", "c++")
    assert _score(skill, "a plain sentence") == 0.0
