"""Code that was written but never wired in.

The failure every syntactic gate agrees with. v1 appended a route below
`urlpatterns` instead of inside it and reported *"Verification passed 1
required stage(s): syntax"* for a page that did not exist.

The balance these tests pin down: **catch the dead handler, stay quiet about
everything a project legitimately defines without calling.** A false positive
costs the model a turn out of eight, so silence is the safe direction and every
"stays quiet" case below is as load-bearing as the one finding.
"""

from __future__ import annotations

from pathlib import Path

from shamsu.verification.reachability import (
    added_but_unreferenced,
    render_findings,
    workspace_sources,
)

DISPATCH_BEFORE = """\
def _start() -> str:
    return "starting"


HANDLERS = {"start": _start}


def dispatch(command: str) -> str:
    return HANDLERS[command]()
"""

DEAD = (
    DISPATCH_BEFORE
    + """

def _restart() -> str:
    return "restarting"
"""
)

WIRED = (
    DISPATCH_BEFORE.replace(
        'HANDLERS = {"start": _start}',
        'HANDLERS = {"start": _start, "restart": _restart}',
    )
    + """

def _restart() -> str:
    return "restarting"
"""
)


class TestItCatchesTheRealThing:
    def test_an_unregistered_handler_is_reported(self) -> None:
        findings = added_but_unreferenced("dispatch.py", DISPATCH_BEFORE, DEAD)
        assert [finding.name for finding in findings] == ["_restart"]
        assert findings[0].kind == "function"

    def test_a_registered_handler_is_not(self) -> None:
        assert added_but_unreferenced("dispatch.py", DISPATCH_BEFORE, WIRED) == ()

    def test_the_message_says_what_to_do(self) -> None:
        rendered = render_findings(added_but_unreferenced("dispatch.py", DISPATCH_BEFORE, DEAD))
        assert "_restart" in rendered
        assert "dispatch table" in rendered

    def test_nothing_added_is_nothing_reported(self) -> None:
        changed = DISPATCH_BEFORE.replace('"starting"', '"started"')
        assert added_but_unreferenced("dispatch.py", DISPATCH_BEFORE, changed) == ()


class TestItStaysQuietWhenItShould:
    """Every case here would be a wasted turn if it fired."""

    def test_a_test_function_is_collected_not_called(self) -> None:
        after = "def test_adds() -> None:\n    assert True\n"
        assert added_but_unreferenced("test_x.py", "", after) == ()

    def test_an_entry_point_is_not_dead(self) -> None:
        after = "def main() -> None:\n    print('hi')\n"
        assert added_but_unreferenced("app.py", "", after) == ()

    def test_a_symbol_used_from_another_file_is_reached(self) -> None:
        """The re-export case. Without this, every package API would be reported."""
        after = "def subtract(a: int, b: int) -> int:\n    return a - b\n"
        others = {"pkg/__init__.py": "from pkg.calc import subtract\n"}
        assert added_but_unreferenced("pkg/calc.py", "", after, others=others) == ()

    def test_a_symbol_reached_by_string_key_is_not_dead(self) -> None:
        """A registry keyed by literal still wires the name in."""
        after = 'def handler() -> None:\n    pass\n\nROUTES = {"handler": None}\n'
        assert added_but_unreferenced("routes.py", "", after) == ()

    def test_an_unparseable_file_says_nothing(self) -> None:
        assert added_but_unreferenced("broken.py", "", "def oops(:\n") == ()

    def test_a_method_is_not_judged(self) -> None:
        """Only top-level definitions. Methods are reached through their class."""
        after = "class Thing:\n    def helper(self) -> None:\n        pass\n"
        findings = added_but_unreferenced("thing.py", "", after)
        assert [finding.name for finding in findings] == ["Thing"], "the class, not the method"


class TestWorkspaceSources:
    def test_it_skips_the_file_under_test_and_dot_directories(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\n")
        (tmp_path / ".shamsu").mkdir()
        (tmp_path / ".shamsu" / "c.py").write_text("z = 3\n")

        sources = workspace_sources(tmp_path, exclude="a.py")
        assert set(sources) == {"b.py"}
