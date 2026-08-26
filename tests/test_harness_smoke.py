"""The checks that answer "does the harness still start and finish a turn".

These exist because of a live failure on 2026-08-26. A cleanup pass deleted
`shamsu/agents/tool_classifier.py`, and nothing caught it: the module was
imported inside a function, so the package still compiled, every import-time
check passed, and the whole test suite was green. The first thing that ran the
line was a person typing `hi` into the real TUI, who got

    Turn failed: No module named 'shamsu.agents.tool_classifier'

Two lessons, one test file:

* a turn has to be *run*, not just imported. `test_a_plain_turn_completes`
  drives the loop end to end against a scripted model;
* and the cheap structural invariants - no import pointing at a module that
  was deleted, no module in the tree that nothing reaches - are worth asserting
  directly, because they are what a cleanup breaks.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from shamsu.agents.simple_chat import SimpleChatLoop, build_simple_tools

PACKAGE = Path(__file__).resolve().parent.parent / "shamsu"


# --------------------------------------------------------------------------
# A turn, end to end
# --------------------------------------------------------------------------


class ScriptedClient:
    """Replays model turns and records what the loop sent."""

    def __init__(self, *turns: dict) -> None:
        self.turns = list(turns)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if not self.turns:
            return {"message": {"content": "done", "tool_calls": []}}
        return self.turns.pop(0)


def _say(text: str) -> dict:
    return {"message": {"content": text, "tool_calls": []}}


def _call(name: str, **arguments) -> dict:
    return {
        "message": {
            "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}],
        }
    }


def _loop(workspace: Path, client: ScriptedClient) -> SimpleChatLoop:
    return SimpleChatLoop(
        workspace,
        client=client,
        tools=build_simple_tools(workspace, console_approval=lambda _request: True),
        verify_changes=False,
    )


def test_a_plain_turn_completes(tmp_path):
    """`hi` - the smallest possible turn, and the one that broke live."""
    client = ScriptedClient(_say("Hello."))
    result = asyncio.run(_loop(tmp_path, client).run("hi"))

    assert result.final.strip() == "Hello."
    assert not result.error
    assert client.calls, "the loop never reached the model"


def test_a_turn_that_reads_a_file_completes(tmp_path):
    """A turn that actually routes tools, so the router runs on a real request."""
    (tmp_path / "notes.txt").write_text("kept\n", encoding="utf-8")
    client = ScriptedClient(
        _call("read_file", filepath="notes.txt"),
        _say("The file says kept."),
    )
    result = asyncio.run(_loop(tmp_path, client).run("read notes.txt and tell me what it says"))

    assert "kept" in result.final
    assert result.tool_calls >= 1


def test_a_turn_that_writes_a_file_completes(tmp_path):
    client = ScriptedClient(
        _call("write_file", filepath="hello.py", content="print('hi')\n"),
        _say("Written."),
    )
    result = asyncio.run(_loop(tmp_path, client).run("create hello.py that prints hi"))

    assert (tmp_path / "hello.py").read_text(encoding="utf-8") == "print('hi')\n"
    assert not result.error


def test_a_prompt_that_forbids_writing_disarms_the_write_tools(tmp_path):
    """The read-only instruction is enforced, not just detected.

    The detector and the enforcement point both survived the cleanup; the call
    joining them did not, so a prompt that said "do not modify files" was
    answered by a loop that could still write.
    """
    (tmp_path / "keep.txt").write_text("original\n", encoding="utf-8")
    client = ScriptedClient(
        _call("write_file", filepath="keep.txt", content="clobbered\n"),
        _say("I was not able to change it."),
    )
    asyncio.run(_loop(tmp_path, client).run("Summarise keep.txt. Do not modify files."))

    assert (tmp_path / "keep.txt").read_text(encoding="utf-8") == "original\n"


def test_a_carve_out_still_allows_the_one_change_it_asks_for(tmp_path):
    """"do not modify any OTHER files" asks for one change - not for none."""
    client = ScriptedClient(
        _call("write_file", filepath="new.txt", content="made\n"),
        _say("Done."),
    )
    asyncio.run(
        _loop(tmp_path, client).run("Create new.txt. Do not modify any other files.")
    )

    assert (tmp_path / "new.txt").exists()


# --------------------------------------------------------------------------
# Structural invariants a cleanup breaks
# --------------------------------------------------------------------------


def _module_names() -> set[str]:
    names: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        names.add(".".join(parts))
    return names


def _shamsu_imports(tree: ast.AST, module: str) -> list[tuple[int, str]]:
    """Every `shamsu.*` module this file imports, including inside functions.

    `from shamsu.cli.commands import models` names a MODULE, not an attribute,
    so each imported name is reported alongside its package - otherwise a
    package that is only ever imported that way looks like it has no importer.
    """
    package = module.rsplit(".", 1)[0] if "." in module else module
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                (node.lineno, alias.name)
                for alias in node.names
                if alias.name.startswith("shamsu")
            ]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                target = ".".join(package.split(".") + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if not target.startswith("shamsu"):
                continue
            found.append((node.lineno, target))
            found += [
                (node.lineno, f"{target}.{alias.name}")
                for alias in node.names
                if alias.name != "*"
            ]
    return found


@pytest.mark.parametrize(
    "path", sorted(p for p in PACKAGE.rglob("*.py") if "__pycache__" not in p.parts),
    ids=lambda p: str(p.relative_to(PACKAGE)).replace("\\", "/"),
)
def test_every_shamsu_import_points_at_a_module_that_exists(path: Path):
    """No import - top level or inside a function - names a deleted module.

    An import inside a function is invisible to the compiler and to every test
    that does not run that exact branch, which is why this is asserted over the
    source rather than left to coverage.
    """
    known = _module_names()
    module = ".".join(path.relative_to(PACKAGE.parent).with_suffix("").parts)
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))

    # `from x import y` yields both `x` and `x.y`; only `x` has to resolve,
    # since `y` may legitimately be a class or a function.
    dangling = [
        f"line {line}: {target}"
        for line, target in _shamsu_imports(tree, module)
        if target not in known and target.rsplit(".", 1)[0] not in known
    ]
    assert not dangling, f"{path.name} imports modules that do not exist: {dangling}"


#: Every way into the package. A module reachable from none of these is dead.
ENTRYPOINTS = (
    "shamsu.cli.app",
    "shamsu.cli.noninteractive",
    "shamsu.webui.cli",
    "shamsu.integrations.telegram.service",
    "shamsu.integrations.telegram.local",
    "shamsu.integrations.telegram.install",
    "shamsu.runtime.ollama",
)


def test_no_module_is_orphaned():
    """Nothing in the tree is unreachable from an entrypoint.

    This is the cleanup's own acceptance test: legacy code is exactly the code
    no entrypoint can reach, so a module that reappears here is either newly
    dead or newly unwired.
    """
    known = _module_names()
    graph: dict[str, set[str]] = {}
    for path in PACKAGE.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        parts = list(path.relative_to(PACKAGE.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        module = ".".join(parts)
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        deps = set()
        for _, target in _shamsu_imports(tree, module):
            if target in known:
                deps.add(target)
            elif target.rsplit(".", 1)[0] in known:
                deps.add(target.rsplit(".", 1)[0])
        graph[module] = deps

    seen: set[str] = set()
    stack = [name for name in ENTRYPOINTS if name in graph]
    while stack:
        module = stack.pop()
        if module in seen:
            continue
        seen.add(module)
        # Importing `a.b.c` imports `a` and `a.b` too.
        parts = module.split(".")
        stack += [
            ".".join(parts[:i]) for i in range(1, len(parts)) if ".".join(parts[:i]) in graph
        ]
        stack += list(graph.get(module, ()))

    orphans = sorted(set(graph) - seen)
    assert not orphans, f"unreachable from every entrypoint: {orphans}"
