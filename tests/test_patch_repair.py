"""Why `old_string not found` kept being the answer to a correct edit.

Live 2026-08-22, session 20260822-090221-f144 on `js/main.js`: four
`patch_file` calls and two `replace_symbol` calls failed, the model then read
the file nine more times, announced the edit three times without making it, and
the turn ended having changed nothing.

Two causes, and they compounded:

1. The model sent `old_string` as a SINGLE LINE containing literal
   backslash-n. The harness detected that and even said so - but it only
   ADOPTED the decoded form when the decode matched exactly. It didn't, so
   every line-based repair below ran on the escaped one-liner, which has no
   lines to compare. The one usable version of the payload was computed and
   thrown away.

2. `read_file` numbers its output and the gutter ends in a space::

       74| function createStarField() {
       75|     const geometry = new THREE.BufferGeometry();

   `_strip_line_numbers` handles the model pasting `74| ` back verbatim. It
   cannot help when the model strips `74|` itself and keeps the separator - and
   that is what happened. Of the 22 lines of one `old_string`, **18 were
   exactly one space too deep and 4 were exact**, and the 4 exact ones were the
   JSDoc header above the read's first line, typed from memory rather than
   copied.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.tools.agent_tools import (
    MIN_INDENT_FUZZ_LINES,
    AgentToolRegistry,
    _indent_shifted_match_block,
)

JS = """\
/**
 * Create starfield background effect
 */
function createStarField() {
    const geometry = new THREE.BufferGeometry();
    const positions = [];

    for (let i = 0; i < CONFIG.starCount; i++) {
        positions.push(1, 2, 3);
    }

    game.scene.add(new THREE.Points(geometry));
}
"""


def _tools(tmp_path: Path) -> AgentToolRegistry:
    return AgentToolRegistry(tmp_path, approval_func=lambda _r: True)


def _write(tmp_path: Path, body: str = JS) -> Path:
    path = tmp_path / "main.js"
    path.write_text(body, encoding="utf-8")
    return path


# -- the gutter -----------------------------------------------------------


def test_a_block_copied_out_of_the_read_gutter_still_applies(tmp_path: Path):
    """Every line one space too deep - the separator the model kept when it
    stripped `74|` itself."""
    path = _write(tmp_path)
    old = "\n".join(
        " " + line
        for line in ["function createStarField() {", "    const geometry = new THREE.BufferGeometry();", "    const positions = [];"]
    )
    result = _tools(tmp_path).execute(
        "edit_file",
        {"filepath": "main.js", "old_string": old, "new_string": " function createStarField() {\n     const geometry = new THREE.BufferGeometry();\n     const positions = [0];"},
    )

    assert result.ok, result.message
    assert "const positions = [0];" in path.read_text(encoding="utf-8")


def test_the_replacement_lands_at_the_files_indentation_not_the_models(tmp_path: Path):
    """Matching the block is half the job. Writing the model's own text back at
    its own wrong indentation fixes the match and breaks the file."""
    path = _write(tmp_path)
    old = " function createStarField() {\n     const geometry = new THREE.BufferGeometry();\n     const positions = [];"
    new = " function createStarField() {\n     const geometry = new THREE.BufferGeometry();\n     const positions = [9];"
    assert _tools(tmp_path).execute(
        "edit_file", {"filepath": "main.js", "old_string": old, "new_string": new}
    ).ok

    body = path.read_text(encoding="utf-8")
    assert "\nfunction createStarField() {\n" in body, "the function got indented"
    assert "\n    const positions = [9];\n" in body, "the body lost its real indent"


def test_lines_the_model_got_right_are_not_shifted_with_the_rest(tmp_path: Path):
    """The block is a MIXTURE - copied lines carry the gutter, hand-typed ones
    do not. Shifting all of them equally fixes the majority by breaking the
    minority, which is the same defect moved somewhere else."""
    path = _write(tmp_path)
    # ` * Create...` and ` */` are already correct; the code lines are +1.
    old = (
        "/**\n"
        " * Create starfield background effect\n"
        " */\n"
        " function createStarField() {\n"
        "     const geometry = new THREE.BufferGeometry();"
    )
    new = old.replace("const geometry", "const geom")
    assert _tools(tmp_path).execute(
        "edit_file", {"filepath": "main.js", "old_string": old, "new_string": new}
    ).ok

    body = path.read_text(encoding="utf-8")
    assert "\n * Create starfield background effect\n" in body, "the JSDoc star lost its space"
    assert "\n */\n" in body
    assert "\n    const geom = " in body


# -- the escaping ---------------------------------------------------------


def test_a_literal_backslash_n_payload_reaches_the_repairs(tmp_path: Path):
    """It used to be decoded, tested for an EXACT match, and discarded when
    that missed - so the repairs below ran on a one-line string with nothing to
    split. This is the exact shape from the reported session: escaped AND one
    space too deep."""
    path = _write(tmp_path)
    old = "\\n".join(
        [" function createStarField() {", "     const geometry = new THREE.BufferGeometry();", "     const positions = [];"]
    )
    new = "\\n".join(
        [" function createStarField() {", "     const geometry = new THREE.BufferGeometry();", "     const positions = [7];"]
    )
    result = _tools(tmp_path).execute(
        "edit_file", {"filepath": "main.js", "old_string": old, "new_string": new}
    )

    assert result.ok, result.message
    body = path.read_text(encoding="utf-8")
    assert "const positions = [7];" in body
    assert "\\n" not in body, "the literal escape was written INTO the file"
    assert "\n    const positions = [7];\n" in body


def test_the_note_says_the_escaping_was_repaired(tmp_path: Path):
    """A silent repair teaches the model nothing, and it will send the same
    shape next turn."""
    _write(tmp_path)
    old = "\\n".join([" function createStarField() {", "     const geometry = new THREE.BufferGeometry();", "     const positions = [];"])
    result = _tools(tmp_path).execute(
        "edit_file",
        {"filepath": "main.js", "old_string": old, "new_string": old.replace("[]", "[7]")},
    )

    assert "backslash-n" in result.message


# -- it must not get looser than that -------------------------------------


def test_text_that_is_simply_not_in_the_file_is_still_refused(tmp_path: Path):
    """Two of the four calls in the reported session were honest misses - the
    model mis-typed the text. Those must keep failing, or the repair has
    stopped being a repair."""
    _write(tmp_path)
    result = _tools(tmp_path).execute(
        "edit_file",
        {
            "filepath": "main.js",
            "old_string": " function createStarField() {\n     const nothing = like_this();\n     const positions = [];",
            "new_string": "x",
        },
    )

    assert not result.ok
    assert "not found" in result.message


def test_an_ambiguous_block_is_refused_rather_than_guessed(tmp_path: Path):
    body = "def a():\n    x = 1\n    y = 2\n\ndef b():\n    x = 1\n    y = 2\n"
    (tmp_path / "m.py").write_text(body, encoding="utf-8")

    result = _tools(tmp_path).execute(
        "edit_file",
        {"filepath": "m.py", "old_string": "  x = 1\n  y = 2\n", "new_string": "  x = 9\n"},
    )

    assert not result.ok
    assert (tmp_path / "m.py").read_text(encoding="utf-8") == body


def test_two_lines_are_not_enough_to_ignore_indentation_on(tmp_path: Path):
    """A short block matched on stripped text alone is loose enough to land in
    the wrong place, and indentation is the only thing separating two
    identically-worded lines in different scopes."""
    assert MIN_INDENT_FUZZ_LINES >= 3
    content = "if a:\n    x = 1\nelse:\n    x = 1\n"

    assert _indent_shifted_match_block(content, "x = 1\n") is None
    assert _indent_shifted_match_block(content, "  x = 1") is None


def test_what_comes_back_is_always_the_files_own_text(tmp_path: Path):
    """The matcher compares stripped lines, so a block whose inner nesting
    differs from the file's can still match. That is safe for exactly one
    reason and it has to hold: what it returns is the FILE's bytes, never the
    model's, so the file's own shape is what survives the replacement."""
    content = "def f():\n    if a:\n        x = 1\n    return x\n"

    # `x = 1` sent at the same depth as `if` - a different program, if it were
    # taken at face value.
    matched = _indent_shifted_match_block(content, "  if a:\n  x = 1\n  return x")

    assert matched == "    if a:\n        x = 1\n    return x"


def test_a_normal_exact_edit_is_untouched(tmp_path: Path):
    """None of this may cost an edit that was already right."""
    path = _write(tmp_path)
    result = _tools(tmp_path).execute(
        "edit_file",
        {
            "filepath": "main.js",
            "old_string": "    const positions = [];",
            "new_string": "    const positions = [1];",
        },
    )

    assert result.ok
    assert "backslash-n" not in result.message
    assert "\n    const positions = [1];\n" in path.read_text(encoding="utf-8")


# -- python, where this matters most --------------------------------------


def test_a_python_block_keeps_its_relative_indentation(tmp_path: Path):
    """In JS a stray space is cosmetic. In Python it is the program."""
    body = "class A:\n    def run(self):\n        if self.ok:\n            return 1\n        return 0\n"
    path = tmp_path / "m.py"
    path.write_text(body, encoding="utf-8")

    old = "\n".join(
        " " + line
        for line in ["    def run(self):", "        if self.ok:", "            return 1", "        return 0"]
    )
    new = old.replace("return 1", "return 2")
    assert _tools(tmp_path).execute(
        "edit_file", {"filepath": "m.py", "old_string": old, "new_string": new}
    ).ok

    after = path.read_text(encoding="utf-8")
    assert after == body.replace("return 1", "return 2")
    compile(after, "m.py", "exec")
