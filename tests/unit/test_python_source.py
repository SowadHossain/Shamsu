"""Deterministic Python structure extraction.

Plan section 16's rule is the thing being protected here: structural facts come
from the parser, or they do not appear. Every assertion below is about the
parser reporting what is actually in the source.
"""

from __future__ import annotations

import pytest

from shamsu.artifacts.python_source import extract_python, module_path_for


class TestModulePaths:
    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("src/shamsu/state/store.py", "shamsu.state.store"),
            ("src/shamsu/state/__init__.py", "shamsu.state"),
            ("shamsu/cli.py", "shamsu.cli"),
            ("main.py", "main"),
            ("lib/pkg/mod.py", "pkg.mod"),
            ("src/__init__.py", ""),
        ],
    )
    def test_derivation(self, path: str, expected: str) -> None:
        assert module_path_for(path) == expected

    def test_windows_separators_normalise(self) -> None:
        assert module_path_for("src\\shamsu\\state\\store.py") == "shamsu.state.store"


class TestSymbolExtraction:
    def test_functions(self) -> None:
        module = extract_python(
            "a.py", 'def login(user: str, pw: str) -> bool:\n    """Log in."""\n    return True\n'
        )
        assert len(module.symbols) == 1
        symbol = module.symbols[0]
        assert symbol.name == "login"
        assert symbol.kind == "function"
        assert symbol.signature == "login(user: str, pw: str) -> bool"
        assert symbol.summary == "Log in."
        assert symbol.line_start == 1

    def test_async_functions_are_distinguished(self) -> None:
        module = extract_python("a.py", "async def fetch() -> str:\n    return ''\n")
        assert module.symbols[0].kind == "async function"

    def test_classes_and_their_methods(self) -> None:
        module = extract_python(
            "a.py",
            'class Auth:\n    """Handles auth."""\n\n'
            "    def login(self) -> bool:\n        return True\n\n"
            "    def _secret(self) -> None: ...\n",
        )
        kinds = {symbol.qualified_name: symbol.kind for symbol in module.symbols}
        assert kinds == {"Auth": "class", "Auth.login": "method", "Auth._secret": "method"}

    def test_decorators_are_recorded(self) -> None:
        module = extract_python(
            "a.py", "class C:\n    @property\n    def x(self) -> int:\n        return 1\n"
        )
        method = next(s for s in module.symbols if s.qualified_name == "C.x")
        assert method.decorators == ("property",)

    def test_upper_case_module_constants(self) -> None:
        module = extract_python("a.py", "MAX_RETRIES = 3\nlowercase = 4\n")
        assert [s.name for s in module.symbols] == ["MAX_RETRIES"]

    def test_annotated_constants(self) -> None:
        module = extract_python("a.py", "TIMEOUT: float = 1.5\n")
        assert module.symbols[0].name == "TIMEOUT"
        assert module.symbols[0].kind == "constant"

    def test_nested_helpers_are_not_extracted(self) -> None:
        """Closures are implementation detail; including them bloats every card."""
        module = extract_python(
            "a.py", "def outer() -> None:\n    def inner() -> None: ...\n    inner()\n"
        )
        assert [s.qualified_name for s in module.symbols] == ["outer"]

    def test_symbols_are_ordered_by_position(self) -> None:
        """So a card diff reflects a real edit, not iteration order."""
        module = extract_python("a.py", "def b() -> None: ...\ndef a() -> None: ...\n")
        assert [s.name for s in module.symbols] == ["b", "a"]

    def test_only_the_first_docstring_line_is_kept(self) -> None:
        """A card carrying a full docstring stops being compact."""
        module = extract_python(
            "a.py", 'def f() -> None:\n    """First line.\n\n    Much more detail.\n    """\n'
        )
        assert module.symbols[0].summary == "First line."


class TestPublicity:
    @pytest.mark.parametrize(
        ("source", "name", "public"),
        [
            ("def login() -> None: ...", "login", True),
            ("def _helper() -> None: ...", "_helper", False),
            ("class _Internal:\n    def visible(self) -> None: ...", "_Internal.visible", False),
            ("class Public:\n    def method(self) -> None: ...", "Public.method", True),
            ("class Public:\n    def _hidden(self) -> None: ...", "Public._hidden", False),
        ],
    )
    def test_publicity(self, source: str, name: str, public: bool) -> None:
        """A public method on a private class is still internal."""
        module = extract_python("a.py", source)
        symbol = next(s for s in module.symbols if s.qualified_name == name)
        assert symbol.is_public is public


class TestImports:
    def test_plain_and_from_imports(self) -> None:
        module = extract_python("a.py", "import os\nimport asyncio\nfrom pathlib import Path\n")
        assert module.imports == ["asyncio", "os", "pathlib"]

    def test_relative_imports_keep_their_dots(self) -> None:
        module = extract_python("a.py", "from .sibling import thing\n")
        assert module.imports == [".sibling"]

    def test_duplicates_collapse(self) -> None:
        module = extract_python("a.py", "import os\nimport os\n")
        assert module.imports == ["os"]

    def test_internal_and_external_are_separable(self) -> None:
        """An edge to pydantic is a dependency fact; one to shamsu.state is architecture."""
        module = extract_python(
            "a.py", "import pydantic\nfrom shamsu.state import store\nimport json\n"
        )
        assert module.external_imports(["shamsu"]) == ["json", "pydantic"]
        assert module.internal_imports(["shamsu"]) == ["shamsu.state"]


class TestFailureHandling:
    def test_a_syntax_error_degrades_honestly(self) -> None:
        """A file mid-edit is ordinary; it must not fail a whole refresh pass."""
        module = extract_python("a.py", "def broken(:\n")
        assert module.parse_error is not None
        assert "SyntaxError" in module.parse_error
        assert module.symbols == []

    def test_an_empty_file_is_not_an_error(self) -> None:
        module = extract_python("a.py", "")
        assert module.parse_error is None
        assert module.symbols == []

    def test_module_docstring_is_captured(self) -> None:
        module = extract_python("a.py", '"""What this module does."""\n')
        assert module.summary == "What this module does."
