"""Which command runs this project's tests.

The model was already told to check its work - "run it, run its tests, or run
the build" has been in the system prompt throughout - and then left to guess
what that command is. So it guessed: `pytest` in a project with no pytest,
`npm test` in a project whose package.json has no test script, `python test.py`
against a file that does not exist. Each guess costs a round and returns an
error that says nothing about the project.

smallcode gives the model `run_tests` and detects the command itself
(`src/tools/run_tests`). This is that, over detection SHAMSU already had buried
in `verify/gate.py` where only the verification pipeline could reach it.

The honest failure matters as much as the detection: a project with no test
command gets told so, plainly, rather than being handed a plausible command
that will fail. "There is no test command here" is a fact the model can act on;
`pytest: command not found` is a puzzle.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

__all__ = ["TestCommand", "detect_test_command"]


@dataclass(frozen=True)
class TestCommand:
    """What to run, and why this project is thought to need it."""

    command: str
    reason: str

    def __bool__(self) -> bool:
        return bool(self.command)


def detect_test_command(workspace: Path, test_filter: str = "") -> TestCommand:
    """The test command for *workspace*, or an empty one with the reason why.

    Ordered by how strong the evidence is, not by language popularity: a
    declared script in `package.json` is a statement by whoever wrote the
    project, while "there are files named test_*.py" is an inference. The
    declaration wins.
    """
    for detect in (_node, _python, _rust, _go, _make):
        found = detect(workspace)
        if found:
            return _with_filter(found, test_filter)
    return TestCommand(
        "",
        "no test runner found - no test script in package.json, no pytest "
        "layout, no Cargo.toml, go.mod or Makefile test target",
    )


def _with_filter(found: TestCommand, test_filter: str) -> TestCommand:
    """Narrow the run, when the runner has a way to be narrowed."""
    narrowing = (test_filter or "").strip()
    if not narrowing:
        return found
    command = found.command
    if "pytest" in command:
        return TestCommand(f"{command} -k {narrowing!r}", found.reason)
    if command.startswith("npm test"):
        joiner = " " if "--" in command else " -- "
        return TestCommand(f"{command}{joiner}{narrowing}", found.reason)
    if command.startswith(("cargo test", "go test")):
        return TestCommand(f"{command} {narrowing}", found.reason)
    return found


def _node(workspace: Path) -> TestCommand | None:
    manifest = workspace / "package.json"
    try:
        scripts = (json.loads(manifest.read_text(encoding="utf-8")) or {}).get("scripts") or {}
    except (OSError, ValueError):
        return None
    script = str(scripts.get("test") or "").strip()
    if not script or not _real_script(script):
        return None
    return TestCommand(_node_command(script), "package.json declares a test script")


# npm writes this stub into every `npm init` project. Running it proves nothing
# and exits 1, which reads as a failing test suite.
_PLACEHOLDER = re.compile(r"no test specified|exit\s+1", re.IGNORECASE)


def _real_script(script: str) -> bool:
    return not _PLACEHOLDER.search(script)


def _node_command(script: str) -> str:
    """`npm test`, plus whatever keeps the runner from waiting forever.

    Both defaults are interactive watchers: vitest and jest will sit holding the
    terminal open, and a tool call that never returns is worse than one that
    fails. Same reasoning as `verify/gate._node_test_command`.
    """
    lowered = script.lower()
    if re.search(r"\bvitest\b", lowered) and not re.search(r"\bvitest\s+run\b", lowered):
        return "npm test -- --run"
    if re.search(r"\bjest\b", lowered) and "--runinband" not in lowered:
        return "npm test -- --runInBand"
    return "npm test"


def _python(workspace: Path) -> TestCommand | None:
    if any((workspace / name).is_file() for name in ("pytest.ini", "conftest.py", "tox.ini")):
        return TestCommand("python -m pytest -q", "this project is laid out for pytest")
    for name in ("pyproject.toml", "setup.cfg", "requirements.txt", "requirements-dev.txt"):
        path = workspace / name
        try:
            if "pytest" in path.read_text(encoding="utf-8", errors="replace").lower():
                return TestCommand("python -m pytest -q", f"{name} names pytest")
        except OSError:
            continue
    tests = workspace / "tests"
    if tests.is_dir() and any(tests.rglob("test_*.py")):
        return TestCommand("python -m pytest -q", "there are tests/test_*.py files")
    if any(workspace.glob("test_*.py")):
        return TestCommand("python -m pytest -q", "there are test_*.py files")
    return None


def _rust(workspace: Path) -> TestCommand | None:
    if (workspace / "Cargo.toml").is_file():
        return TestCommand("cargo test", "Cargo.toml is present")
    return None


def _go(workspace: Path) -> TestCommand | None:
    if (workspace / "go.mod").is_file():
        return TestCommand("go test ./...", "go.mod is present")
    return None


_MAKE_TEST = re.compile(r"^test\s*:", re.MULTILINE)


def _make(workspace: Path) -> TestCommand | None:
    for name in ("Makefile", "makefile", "GNUmakefile"):
        path = workspace / name
        try:
            if _MAKE_TEST.search(path.read_text(encoding="utf-8", errors="replace")):
                return TestCommand("make test", f"{name} has a test target")
        except OSError:
            continue
    return None
