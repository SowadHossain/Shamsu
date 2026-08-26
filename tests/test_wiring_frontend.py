"""The three cross-file defects that shipped a game which never loaded.

2026-08-24, `demo-3/openbazar`. Eight JS modules, ~1,800 lines, every file
green under `node --check`, and the contract reported seven of nine assertions
passed. The page never ran a single line, and none of the three reasons is
visible from inside any one file:

  * `index.html` said `<script src="game.js">`; the file was at `js/game.js`.
    The HTML was written seventeen minutes before the script existed and
    nothing re-checked the path.
  * `const GameState` was declared in `config.js` AND `game.js`. Loaded
    together as plain scripts they share one scope, so the second file dies on
    `SyntaxError: Identifier 'GameState' has already been declared`.
  * `playSound()` was called eleven times across five modules and defined
    nowhere - the sound manager exports `SoundManager.play`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from shamsu.verify.wiring import verify_wiring


def _kinds(root: Path) -> list[str]:
    return [d.kind for d in verify_wiring(root).diagnostics]


def _messages(root: Path) -> str:
    return "\n".join(d.render() for d in verify_wiring(root).diagnostics)


# -- an asset the browser will 404 on --------------------------------------


def test_a_script_tag_pointing_at_nothing_is_an_error(tmp_path: Path):
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "game.js").write_text("function go() {}\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<script src="game.js"></script>', encoding="utf-8"
    )

    assert "missing_asset" in _kinds(tmp_path)
    assert "game.js" in _messages(tmp_path)


def test_the_same_tag_with_the_right_path_is_fine(tmp_path: Path):
    (tmp_path / "js").mkdir()
    (tmp_path / "js" / "game.js").write_text("function go() {}\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<script src="js/game.js"></script>', encoding="utf-8"
    )

    assert "missing_asset" not in _kinds(tmp_path)


@pytest.mark.parametrize(
    "target",
    [
        "https://cdn.example.com/three.min.js",
        "//cdn.example.com/three.js",
        "data:text/javascript,void 0",
        "#main",
        "{{ static_url }}",
        "${base}/app.js",
    ],
)
def test_targets_the_filesystem_cannot_settle_are_left_alone(tmp_path: Path, target):
    """A CDN, a data URI, a fragment, a template placeholder. Flagging these
    would make the check fire on every real project that uses one."""
    (tmp_path / "index.html").write_text(
        '<script src="' + target + '"></script>', encoding="utf-8"
    )

    assert "missing_asset" not in _kinds(tmp_path)


def test_a_root_relative_path_resolves_from_the_project_root(tmp_path: Path):
    (tmp_path / "static").mkdir()
    (tmp_path / "static" / "app.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "pages").mkdir()
    (tmp_path / "pages" / "one.html").write_text(
        '<link href="/static/app.css">', encoding="utf-8"
    )

    assert "missing_asset" not in _kinds(tmp_path)


# -- one name, two files, one scope ----------------------------------------


def test_a_const_declared_in_two_scripts_is_an_error(tmp_path: Path):
    (tmp_path / "config.js").write_text("const GameState = {};\n", encoding="utf-8")
    (tmp_path / "game.js").write_text("const GameState = {};\n", encoding="utf-8")

    assert "js_redeclaration" in _kinds(tmp_path)
    assert "GameState" in _messages(tmp_path)


def test_two_functions_of_one_name_are_not_an_error(tmp_path: Path):
    """Legal - the second silently wins. Flagging it would fire on every
    project with a `main()` in two files."""
    (tmp_path / "a.js").write_text("function main() {}\n", encoding="utf-8")
    (tmp_path / "b.js").write_text("function main() {}\n", encoding="utf-8")

    assert "js_redeclaration" not in _kinds(tmp_path)


def test_es_modules_have_their_own_scope_and_are_exempt(tmp_path: Path):
    (tmp_path / "a.js").write_text("export const State = {};\n", encoding="utf-8")
    (tmp_path / "b.js").write_text(
        "import { State } from './a.js';\nconst other = State;\n", encoding="utf-8"
    )

    assert "js_redeclaration" not in _kinds(tmp_path)


# -- a helper nobody wrote --------------------------------------------------


def test_a_helper_called_across_files_and_defined_nowhere_is_an_error(tmp_path: Path):
    for name in ("snake.js", "collision.js", "input.js"):
        (tmp_path / name).write_text("playSound(1);\n", encoding="utf-8")
    (tmp_path / "sound.js").write_text(
        "const SoundManager = { play: function () {} };\n", encoding="utf-8"
    )

    assert "undefined_helper" in _kinds(tmp_path)
    message = _messages(tmp_path)
    assert "playSound()" in message
    assert "3 files" in message


def test_a_helper_that_exists_somewhere_is_fine(tmp_path: Path):
    (tmp_path / "sound.js").write_text("function playSound(n) {}\n", encoding="utf-8")
    for name in ("snake.js", "collision.js"):
        (tmp_path / name).write_text("playSound(1);\n", encoding="utf-8")

    assert "undefined_helper" not in _kinds(tmp_path)


def test_a_name_used_in_one_file_only_is_never_reported(tmp_path: Path):
    """The rule that keeps this quiet. A callback parameter, a local closure,
    a binding no regex here models - all of them live in one file."""
    (tmp_path / "a.js").write_text(
        "function run(cb) { cb(); each(x => x()); }\n", encoding="utf-8"
    )

    assert "undefined_helper" not in _kinds(tmp_path)


def test_a_call_inside_a_comment_is_not_a_call(tmp_path: Path):
    """Live false positive: `// Check collision with body (skip ...)` was
    reported as a call to `body()`. A gate that cries wolf on prose is one
    people learn to ignore."""
    for index, name in enumerate(("a.js", "b.js")):
        (tmp_path / name).write_text(
            "// Check collision with body (skip a few)\n"
            "/* also mentions ghost (here) */\n"
            # A distinct name per file: two files declaring one `const` is a
            # real redeclaration, and catching it here would be the check
            # working, not the fixture.
            "const note" + str(index) + " = 'and phantom (in a string)';\n",
            encoding="utf-8",
        )

    assert _kinds(tmp_path) == []


def test_a_constructor_is_not_a_missing_helper(tmp_path: Path):
    """`new Float32Array(...)` was reported on a real project. The globals list
    can never be complete, so `new` is not guessed at."""
    for name in ("a.js", "b.js"):
        (tmp_path / name).write_text(
            "const buf = new Float32Array(3);\nconst w = new Wobble(1);\n",
            encoding="utf-8",
        )

    assert "undefined_helper" not in _kinds(tmp_path)


def test_browser_globals_are_not_missing_helpers(tmp_path: Path):
    for name in ("a.js", "b.js"):
        (tmp_path / name).write_text(
            "requestAnimationFrame(tick);\nparseInt('3', 10);\nsetTimeout(go, 1);\n",
            encoding="utf-8",
        )

    assert "undefined_helper" not in _kinds(tmp_path)


# -- all three at once, as they actually shipped ---------------------------


def test_the_whole_snake_game_failure_is_caught(tmp_path: Path):
    (tmp_path / "js").mkdir()
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="styles.css">\n'
        '<script src="game.js"></script>\n',
        encoding="utf-8",
    )
    (tmp_path / "styles.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "js" / "config.js").write_text(
        "const GameState = { score: 0 };\n", encoding="utf-8"
    )
    (tmp_path / "js" / "game.js").write_text(
        "const GameState = { score: 0 };\nplaySound(1);\n", encoding="utf-8"
    )
    (tmp_path / "js" / "snake.js").write_text("playSound(2);\n", encoding="utf-8")

    kinds = set(_kinds(tmp_path))
    assert kinds == {"missing_asset", "js_redeclaration", "undefined_helper"}, kinds


def test_a_clean_project_stays_clean(tmp_path: Path):
    (tmp_path / "js").mkdir()
    (tmp_path / "index.html").write_text(
        '<script src="js/app.js"></script>', encoding="utf-8"
    )
    (tmp_path / "js" / "app.js").write_text(
        "function start() { console.log('go'); }\nstart();\n", encoding="utf-8"
    )

    assert verify_wiring(tmp_path).ok
