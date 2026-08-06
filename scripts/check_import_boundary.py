#!/usr/bin/env python3
"""Fail if v2 production code reaches into the archived v1 tree.

Plan section 8.1 states the rule; this makes it enforceable. Without a
mechanical check, "legacy code is a donor, not a dependency" degrades into a
convention, and conventions lose to deadlines.

Detects, by AST rather than grep so comments and strings do not trip it:

  * ``import legacy_code`` / ``from legacy_code... import ...``
  * ``importlib.import_module("legacy_code...")``
  * ``sys.path`` manipulation mentioning the legacy directory
  * literal ``legacy-code`` / ``legacy_code`` path strings in source

Two things are deliberately NOT violations:

  * Docstrings. Documentation saying "never import legacy-code/" is the
    opposite of a violation.
  * A line marked ``# boundary-ok: <reason>``. Some code must *name* the
    archive in order to *exclude* it -- the artifact scanner's ignore list is
    the motivating case. A narrow, greppable, reason-carrying pragma is better
    than either a blanket exemption or code that cannot say what it means.

Usage:
    python scripts/check_import_boundary.py [--root .]

Exit status 0 when clean, 1 when a violation is found.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

LEGACY_DIR = "legacy-code"

# Module-path spellings that would mean "the archived tree".
BANNED_MODULE_PREFIXES = ("legacy_code", "legacy-code")

# Substrings that indicate a path reference to the archived tree. Checked
# against string literals so that a hard-coded path is caught even when it
# never becomes an import.
BANNED_PATH_FRAGMENTS = ("legacy-code", "legacy_code")


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    detail: str

    def render(self, root: Path) -> str:
        try:
            shown = self.path.relative_to(root)
        except ValueError:
            shown = self.path
        return f"{shown}:{self.line}: {self.detail}"


def _is_banned_module(name: str | None) -> bool:
    if not name:
        return False
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in BANNED_MODULE_PREFIXES)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Identify docstring constants so prose may discuss the archive freely.

    Documentation that says "do not import legacy-code/" is the opposite of a
    violation, and a checker that punishes it would just teach people to stop
    writing it down.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            ids.add(id(body[0].value))
    return ids


#: Marks a line that may name the archive in order to exclude it.
PRAGMA = "boundary-ok"


class BoundaryVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, docstrings: set[int], lines: list[str]) -> None:
        self.path = path
        self.docstrings = docstrings
        self.lines = lines
        self.violations: list[Violation] = []

    def _exempt(self, lineno: int) -> bool:
        """Whether the source line carries the opt-out pragma."""
        if not 1 <= lineno <= len(self.lines):
            return False
        return PRAGMA in self.lines[lineno - 1]

    def _flag(self, node: ast.AST, detail: str) -> None:
        lineno = getattr(node, "lineno", 0)
        # An import is never exemptible -- the pragma exists for exclusion
        # lists, not for smuggling a dependency past the check.
        self.violations.append(Violation(self.path, lineno, detail))

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if _is_banned_module(alias.name):
                self._flag(node, f"imports archived module '{alias.name}'")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # node.module is None for relative imports, which cannot escape src/.
        if _is_banned_module(node.module):
            self._flag(node, f"imports from archived module '{node.module}'")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if (
            isinstance(node.value, str)
            and id(node) not in self.docstrings
            and not self._exempt(getattr(node, "lineno", 0))
        ):
            for fragment in BANNED_PATH_FRAGMENTS:
                if fragment in node.value:
                    self._flag(
                        node,
                        f"string literal references the archived tree: {node.value!r}",
                    )
                    break
        self.generic_visit(node)


def iter_python_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def check_file(path: Path) -> list[Violation]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return [Violation(path, 0, f"unreadable: {exc}")]

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [Violation(path, exc.lineno or 0, f"syntax error: {exc.msg}")]

    visitor = BoundaryVisitor(path, _docstring_nodes(tree), source.splitlines())
    visitor.visit(tree)
    return visitor.violations


def check_packaging(root: Path) -> list[Violation]:
    """The root project must not depend on, or ship, the archive.

    Parsed rather than grepped, because the interesting fields are few and the
    file legitimately mentions ``legacy-code`` elsewhere -- the ruff ban rule
    and pytest's ``norecursedirs`` are what *enforces* the boundary, and
    flagging them would be exactly backwards.
    """
    pyproject = root / "pyproject.toml"
    if not pyproject.exists():
        return []

    try:
        config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        return [Violation(pyproject, exc.lineno, f"unparseable pyproject.toml: {exc.msg}")]

    violations: list[Violation] = []

    def flag(detail: str) -> None:
        violations.append(Violation(pyproject, 0, detail))

    project = config.get("project", {})

    # Runtime and optional dependencies must not name the archive.
    dependency_groups: list[tuple[str, object]] = [
        ("project.dependencies", project.get("dependencies", []))
    ]
    for extra, deps in (project.get("optional-dependencies") or {}).items():
        dependency_groups.append((f"project.optional-dependencies.{extra}", deps))

    for field, deps in dependency_groups:
        if not isinstance(deps, list):
            continue
        for dep in deps:
            if isinstance(dep, str) and any(f in dep for f in BANNED_PATH_FRAGMENTS):
                flag(f"{field} depends on the archive: {dep!r}")

    # The wheel must not ship the archive.
    wheel = config.get("tool", {}).get("hatch", {}).get("build", {}).get("targets", {})
    packages = (wheel.get("wheel") or {}).get("packages", [])
    if isinstance(packages, list):
        for package in packages:
            if isinstance(package, str) and any(f in package for f in BANNED_PATH_FRAGMENTS):
                flag(f"wheel packages the archive: {package!r}")

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)

    root = args.root.resolve()
    src = root / "src"

    if not src.is_dir():
        print(f"error: no src/ directory under {root}", file=sys.stderr)
        return 1

    violations: list[Violation] = []
    checked = 0
    for path in iter_python_files(src):
        checked += 1
        violations.extend(check_file(path))

    violations.extend(check_packaging(root))

    if violations:
        print(f"Import boundary VIOLATED ({len(violations)} finding(s)):\n", file=sys.stderr)
        for violation in violations:
            print(f"  {violation.render(root)}", file=sys.stderr)
        print(
            "\nv2 production code must not import or reference legacy-code/."
            "\nTo reuse legacy logic, migrate it: see LEGACY_COMPONENTS.md.",
            file=sys.stderr,
        )
        return 1

    print(f"Import boundary clean: {checked} file(s) under src/ carry no legacy reference.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
