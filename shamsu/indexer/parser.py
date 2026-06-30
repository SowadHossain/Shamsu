"""
Lightweight source parsing for indexed files.

Day 2 starts with Python's stdlib AST. It gives enough structure for symbol
lookup without adding runtime memory pressure or depending on language servers.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SymbolRecord:
    name: str
    kind: str
    line_start: int
    line_end: int
    signature: str
    docstring: str = ""


@dataclass
class SnippetRecord:
    content: str
    line_start: int
    line_end: int
    chunk_index: int


class _PythonSymbolVisitor(ast.NodeVisitor):
    def __init__(self, source_lines: list[str]):
        self.source_lines = source_lines
        self.symbols: list[SymbolRecord] = []
        self._class_stack: list[str] = []

    def visit_Import(self, node: ast.Import) -> None:
        names = ", ".join(alias.name for alias in node.names)
        self._add(node, names, "import")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = "." * node.level + (node.module or "")
        names = ", ".join(alias.name for alias in node.names)
        self._add(node, f"{module}.{names}".strip("."), "import")

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add(node, node.name, "class")
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        kind = "method" if self._class_stack else "function"
        self._add(node, node.name, kind)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.visit_FunctionDef(node)

    def _add(self, node: ast.AST, name: str, kind: str) -> None:
        line_start = getattr(node, "lineno", 1)
        line_end = getattr(node, "end_lineno", line_start)
        signature = self.source_lines[line_start - 1].strip() if self.source_lines else name
        docstring = ast.get_docstring(node) if isinstance(node, (ast.ClassDef, ast.FunctionDef)) else ""
        self.symbols.append(
            SymbolRecord(
                name=name,
                kind=kind,
                line_start=line_start,
                line_end=line_end,
                signature=signature,
                docstring=docstring or "",
            )
        )


def parse_python_symbols(source: str) -> list[SymbolRecord]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    visitor = _PythonSymbolVisitor(source.splitlines())
    visitor.visit(tree)
    return visitor.symbols


def build_line_windows(source: str, window_size: int = 40) -> list[SnippetRecord]:
    lines = source.splitlines()
    snippets: list[SnippetRecord] = []
    for index, start in enumerate(range(0, len(lines), window_size)):
        window = lines[start : start + window_size]
        content = "\n".join(window).strip()
        if not content:
            continue
        snippets.append(
            SnippetRecord(
                content=content,
                line_start=start + 1,
                line_end=start + len(window),
                chunk_index=index,
            )
        )
    return snippets


def read_text_file(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
    except OSError:
        return None
