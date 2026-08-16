"""Did the code that was just written actually get wired in?

The deepest failure this runtime has shipped, and the one every other gate
misses: SHAMSU verified that code *parsed*, never that it was *reachable*.
Asked to add a route, v1's model appended it below `urlpatterns` instead of
inside it —

    urlpatterns = [
        path('', views.book_list, name='book_list'),
    ]
    path('members/', views.member_list, name='member_list')   # dead

— valid Python, `compile` passes, the write probe passes, ruff passes,
`manage.py check` passes, and the page does not exist. The run reported
*"[verified] Verification passed 1 required stage(s): syntax."*

The shape recurs wherever a definition has to be *registered* somewhere to take
effect: a handler added to a file but not to the dispatch table, a command not
added to the parser, a fixture not added to the list. The model writes correct
code in the wrong place and every syntactic check agrees with it.

**This reports; it does not gate.** A newly added function with no caller is
suspicious, not wrong: a public API is added before its users, and pytest
collects `test_*` by name rather than by reference. Failing a step on this
would trade a false success for a false failure, which is not an improvement.
So the finding goes back to the model as an observation, while it still has
actions left to fix it — which is the one thing that reliably worked in every
live session where a human fed an error back by hand.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from shamsu.security.paths import workspace_key

#: Names that are reached by machinery rather than by a reference, so having no
#: caller says nothing about them.
#:
#: `test_*` is pytest's collection rule. `main` is an entry point. Dunders are
#: called by the interpreter. A leading underscore is *not* here on purpose —
#: a private helper nothing calls is exactly the dead code worth reporting.
_COLLECTED = ("test_", "main", "setup_", "teardown_")


@dataclass(frozen=True)
class Unreferenced:
    """A definition this change added that nothing appears to use."""

    path: str
    name: str
    line: int
    kind: str

    def render(self) -> str:
        return f"{self.path}:{self.line} defines {self.kind} {self.name}, and nothing calls it"


def added_but_unreferenced(
    path: str, before: str, after: str, *, others: dict[str, str] | None = None
) -> tuple[Unreferenced, ...]:
    """Top-level names `after` adds that no file appears to reference.

    Compares *definitions*, not text: a function that moved is not new, and a
    rename is a removal plus an addition, which is the right reading.

    `others` is the rest of the workspace, so a symbol registered from a
    different module counts as reached. Without it, every function a package
    re-exports would be reported.

    Deliberately name-based and over-permissive. Python binding is not
    statically decidable, and this decides whether to *tell the model
    something* — the cost of a miss is the status quo, and the cost of a false
    positive is a wasted turn. Erring toward silence is the right direction.
    """
    try:
        old = _definitions(before)
        new = _definitions(after)
    except SyntaxError:
        # The write probe refuses unparseable content before it lands, so this
        # is a file that was already broken. Nothing useful to say about it.
        return ()

    added = {name: node for name, node in new.items() if name not in old}
    if not added:
        return ()

    referenced = _names_used(after, skip=set(added))
    for source in (others or {}).values():
        referenced |= _names_used(source, skip=set())

    findings = [
        Unreferenced(
            path=path,
            name=name,
            line=getattr(node, "lineno", 0),
            kind="class" if isinstance(node, ast.ClassDef) else "function",
        )
        for name, node in sorted(added.items())
        if name not in referenced and not name.startswith(_COLLECTED) and not name.startswith("__")
    ]
    return tuple(findings)


def _definitions(source: str) -> dict[str, ast.AST]:
    """Top-level functions and classes, by name.

    Top-level only. A method is reached through its class, and reporting every
    uncalled method would bury the one finding that matters in noise.
    """
    tree = ast.parse(source)
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }


def _names_used(source: str, *, skip: set[str]) -> set[str]:
    """Every name mentioned, other than at its own definition site.

    `skip` holds the names being judged, so a function's own `def` line does
    not count as a use of it — while a *recursive* call still would, which is
    a limitation worth accepting: the alternative is walking scopes, and this
    is a hint generator.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()

    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.alias):
            used.add(node.asname or node.name.split(".")[-1])
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # A dispatch table keyed by string — `{"restart": _restart}` uses
            # the name, but `HANDLERS["restart"]` reaches it by literal. Both
            # spellings count, because the question is "is this wired in?" and
            # a string key is one of the ways it can be.
            if node.value.isidentifier():
                used.add(node.value)
        elif (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.name not in skip
        ):
            used.add(node.name)

    return used


def render_findings(findings: tuple[Unreferenced, ...]) -> str:
    """The observation handed back to the model, phrased as work to do."""
    if not findings:
        return ""

    lines = [
        "The code you wrote is not reachable. Adding a definition is not the same as wiring it in:",
        *[f"  - {finding.render()}" for finding in findings],
        "",
        "If it needs registering somewhere — a dispatch table, a route list, a "
        "parser, an __init__ export — do that now. If it is genuinely meant to "
        "be called from outside this project, say so and conclude.",
    ]
    return "\n".join(lines)


def workspace_sources(workspace: Path, *, exclude: str = "", limit: int = 400) -> dict[str, str]:
    """Python sources in the workspace, for the cross-file reference check."""
    sources: dict[str, str] = {}
    skip = workspace_key(exclude)

    for candidate in sorted(workspace.rglob("*.py")):
        if len(sources) >= limit:
            break
        relative = candidate.relative_to(workspace).as_posix()
        if relative == skip or any(part.startswith(".") for part in candidate.parts):
            continue
        try:
            sources[relative] = candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return sources


__all__ = [
    "Unreferenced",
    "added_but_unreferenced",
    "render_findings",
    "workspace_sources",
]
