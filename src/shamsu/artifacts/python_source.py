"""Deterministic structure extraction from Python source.

Plan section 16 draws the line this module sits on: **structural facts must come
from deterministic analysis.** A model may later add a prose summary of what a
module is *for*, but it may never be the source of a symbol name, a file path,
a line number, or an import edge. Those come from the parser or they do not
appear.

Python uses stdlib `ast`, which costs nothing and is exactly correct for the
language SHAMSU is written in. Tree-sitter arrives in Milestone 8 for the
other languages; the extracted types here are deliberately language-agnostic so
that lands as a new extractor rather than a rewrite.
"""

from __future__ import annotations

import ast
import contextlib
from collections.abc import Sequence
from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field

#: Candidate packaging roots. In a src-layout project `src/` is a packaging
#: detail rather than part of the import path, so `src/pkg/mod.py` imports as
#: `pkg.mod`.
#:
#: This is only true when the directory is NOT itself a package. A project with
#: `src/__init__.py` really does import as `src.pkg.mod`, and stripping there
#: would silently break every import edge -- which is exactly the kind of wrong
#: structural claim artifacts must not make. `RepositoryContext` inspects the
#: tree and passes the roots that actually apply; this default is for callers
#: with only a path string to go on.
DEFAULT_SOURCE_ROOTS = ("src/", "lib/")


class ExtractedSymbol(BaseModel):
    """One named entity found in a source file."""

    model_config = ConfigDict(frozen=True)

    name: str
    qualified_name: str
    kind: str = Field(description="function | async function | class | method | constant")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    signature: str = ""
    summary: str = Field(default="", description="First docstring line, verbatim.")
    decorators: tuple[str, ...] = ()

    @property
    def is_public(self) -> bool:
        """Whether the symbol is part of the module's outward surface.

        Leading underscore anywhere in the qualified name means private, so a
        public method on a private class is correctly treated as internal.
        """
        return not any(part.startswith("_") for part in self.qualified_name.split("."))


class ExtractedModule:
    """Structure of one Python file. Not frozen: built incrementally by the visitor."""

    def __init__(self, path: str, module_path: str) -> None:
        self.path = path
        self.module_path = module_path
        self.summary: str = ""
        self.imports: list[str] = []
        self.symbols: list[ExtractedSymbol] = []
        self.parse_error: str | None = None

    @property
    def public_symbols(self) -> list[ExtractedSymbol]:
        return [symbol for symbol in self.symbols if symbol.is_public]

    @property
    def top_level_symbols(self) -> list[ExtractedSymbol]:
        return [symbol for symbol in self.symbols if "." not in symbol.qualified_name]

    def external_imports(self, internal_prefixes: Sequence[str]) -> list[str]:
        """Imports that leave the project.

        Distinguishing internal from external matters for the module card: an
        edge to `pydantic` is a dependency fact, an edge to `shamsu.state` is
        an architecture fact, and conflating them makes both less useful.
        """
        return [
            name
            for name in self.imports
            if not any(
                name == prefix or name.startswith(f"{prefix}.") for prefix in internal_prefixes
            )
        ]

    def internal_imports(self, internal_prefixes: Sequence[str]) -> list[str]:
        return [
            name
            for name in self.imports
            if any(name == prefix or name.startswith(f"{prefix}.") for prefix in internal_prefixes)
        ]


def module_path_for(path: str, source_roots: Sequence[str] = DEFAULT_SOURCE_ROOTS) -> str:
    """Dotted module path for a repository-relative file path.

    `src/shamsu/state/store.py` -> `shamsu.state.store`
    `src/shamsu/state/__init__.py` -> `shamsu.state`

    Pass `source_roots=()` when no directory should be stripped -- notably when
    `src/` is itself a package.
    """
    normalised = path.replace("\\", "/")
    for root in source_roots:
        if normalised.startswith(root):
            normalised = normalised[len(root) :]
            break

    pure = PurePosixPath(normalised)
    parts = list(pure.parts)
    if not parts:
        return ""

    stem = pure.stem
    if stem == "__init__":
        parts = parts[:-1]
    else:
        parts[-1] = stem

    return ".".join(parts)


def _summary_of(node: ast.AST) -> str:
    """First line of a docstring, or empty.

    Only the first line: a card carrying a full docstring stops being compact,
    and compactness is the entire purpose of an artifact.
    """
    if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        return ""
    doc = ast.get_docstring(node, clean=True)
    if not doc:
        return ""
    return doc.strip().splitlines()[0].strip()


def _signature_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render a function signature from the AST.

    Reconstructed rather than sliced from source so it stays a single line
    regardless of how the definition was formatted.
    """
    # `unparse` is best-effort: an exotic annotation is not worth failing a
    # whole card over, and a signature rendered as `...` is honestly incomplete
    # rather than wrong.
    try:
        args = ast.unparse(node.args)
    except (ValueError, RecursionError, AttributeError):
        args = "..."

    rendered = f"{node.name}({args})"
    if node.returns is not None:
        with contextlib.suppress(ValueError, RecursionError, AttributeError):
            rendered += f" -> {ast.unparse(node.returns)}"
    return rendered


def _decorators_of(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[str, ...]:
    rendered: list[str] = []
    for decorator in node.decorator_list:
        with contextlib.suppress(ValueError, RecursionError, AttributeError):
            rendered.append(ast.unparse(decorator))
    return tuple(rendered)


class _Visitor(ast.NodeVisitor):
    """Walks a module, recording top-level symbols and one level of nesting.

    One level is deliberate. Methods matter -- they are the callable surface of
    a class. Closures and locally-defined helpers do not: they are
    implementation detail, and including them would bloat every card with names
    nothing outside the function can reach.
    """

    def __init__(self, module: ExtractedModule) -> None:
        self.module = module

    # -- imports -----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.module.imports.append(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # Relative imports are recorded as written; resolving them needs the
        # package context, which belongs to the graph builder, not here.
        if node.module:
            prefix = "." * node.level
            self.module.imports.append(f"{prefix}{node.module}")
        elif node.level:
            self.module.imports.append("." * node.level)

    # -- definitions -------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.module.symbols.append(
            ExtractedSymbol(
                name=node.name,
                qualified_name=node.name,
                kind="class",
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                signature=node.name,
                summary=_summary_of(node),
                decorators=_decorators_of(node),
            )
        )
        for child in node.body:
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                self.module.symbols.append(
                    ExtractedSymbol(
                        name=child.name,
                        qualified_name=f"{node.name}.{child.name}",
                        kind="method",
                        line_start=child.lineno,
                        line_end=child.end_lineno or child.lineno,
                        signature=_signature_of(child),
                        summary=_summary_of(child),
                        decorators=_decorators_of(child),
                    )
                )
        # Not calling generic_visit: nested classes and closures are
        # implementation detail, per the class docstring.

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._function(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._function(node, "async function")

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, kind: str) -> None:
        self.module.symbols.append(
            ExtractedSymbol(
                name=node.name,
                qualified_name=node.name,
                kind=kind,
                line_start=node.lineno,
                line_end=node.end_lineno or node.lineno,
                signature=_signature_of(node),
                summary=_summary_of(node),
                decorators=_decorators_of(node),
            )
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        """Record module-level UPPER_CASE names as constants.

        Screaming case is the only signal Python gives that an assignment is
        meant as configuration rather than a working variable, and those are
        the module-level names worth putting on a card.
        """
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id.isupper():
                self.module.symbols.append(
                    ExtractedSymbol(
                        name=target.id,
                        qualified_name=target.id,
                        kind="constant",
                        line_start=node.lineno,
                        line_end=node.end_lineno or node.lineno,
                        signature=target.id,
                    )
                )

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        target = node.target
        if isinstance(target, ast.Name) and target.id.isupper():
            self.module.symbols.append(
                ExtractedSymbol(
                    name=target.id,
                    qualified_name=target.id,
                    kind="constant",
                    line_start=node.lineno,
                    line_end=node.end_lineno or node.lineno,
                    signature=target.id,
                )
            )


def extract_python(path: str, source: str) -> ExtractedModule:
    """Parse one Python file into its structure.

    A syntax error is recorded on the result rather than raised. A file being
    mid-edit is ordinary, and it should degrade to "no structure extracted" --
    an honest empty result -- rather than failing a whole refresh pass.
    """
    module = ExtractedModule(path=path, module_path=module_path_for(path))

    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError) as exc:
        module.parse_error = f"{type(exc).__name__}: {exc}"
        return module

    module.summary = _summary_of(tree)

    visitor = _Visitor(module)
    for node in tree.body:
        visitor.visit(node)

    # Stable order: by position, so a card diff reflects a real edit rather
    # than dictionary iteration order.
    module.symbols.sort(key=lambda symbol: (symbol.line_start, symbol.qualified_name))
    module.imports = sorted(set(module.imports))
    return module


__all__ = [
    "DEFAULT_SOURCE_ROOTS",
    "ExtractedModule",
    "ExtractedSymbol",
    "extract_python",
    "module_path_for",
]
