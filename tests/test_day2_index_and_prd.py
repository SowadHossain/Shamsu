from __future__ import annotations

import sqlite3

from shamsu.indexer.parser import parse_python_symbols
from shamsu.indexer.walker import FileWalker
from shamsu.prd.extractor import extract_entities
from shamsu.prd.parser import MarkdownPRDParser
from shamsu.retriever.search import SearchAgent


def test_parse_python_symbols_extracts_classes_functions_methods_and_imports():
    source = (
        "import os\n\n"
        "class Sandbox:\n"
        "    \"\"\"Path safety.\"\"\"\n"
        "    def validate(self, path):\n"
        "        return path\n\n"
        "def helper(value):\n"
        "    return value\n"
    )

    symbols = parse_python_symbols(source)
    names = {(symbol.name, symbol.kind) for symbol in symbols}

    assert ("os", "import") in names
    assert ("Sandbox", "class") in names
    assert ("validate", "method") in names
    assert ("helper", "function") in names
    sandbox = next(symbol for symbol in symbols if symbol.name == "Sandbox")
    assert sandbox.docstring == "Path safety."


def test_file_walker_writes_symbols_and_searchable_snippets(tmp_path):
    (tmp_path / "auth.py").write_text(
        "class LoginView:\n"
        "    def authenticate_user(self):\n"
        "        return 'session auth flow'\n",
        encoding="utf-8",
    )

    db_path = tmp_path / ".shamsu" / "index.db"
    entries = FileWalker(tmp_path, db_path=db_path).index()

    assert entries[0].symbol_count == 2

    conn = sqlite3.connect(db_path)
    symbols = conn.execute("SELECT name, kind FROM symbols ORDER BY name").fetchall()
    snippets = conn.execute("SELECT content FROM snippets").fetchall()
    conn.close()
    assert ("LoginView", "class") in symbols
    assert ("authenticate_user", "method") in symbols
    assert any("session auth flow" in row[0] for row in snippets)

    search_results = SearchAgent(db_path).search("session authentication", top_k=3)
    assert search_results
    assert search_results[0].file_path == "auth.py"


def test_file_walker_removes_stale_index_rows(tmp_path):
    old_file = tmp_path / "old.py"
    old_file.write_text("def old_symbol():\n    return 1\n", encoding="utf-8")
    db_path = tmp_path / ".shamsu" / "index.db"
    FileWalker(tmp_path, db_path=db_path).index()

    old_file.unlink()
    (tmp_path / "new.py").write_text("def new_symbol():\n    return 2\n", encoding="utf-8")
    FileWalker(tmp_path, db_path=db_path).index()

    conn = sqlite3.connect(db_path)
    paths = conn.execute("SELECT path FROM files ORDER BY path").fetchall()
    symbols = conn.execute("SELECT name FROM symbols ORDER BY name").fetchall()
    snippets = conn.execute("SELECT content FROM snippets").fetchall()
    conn.close()

    assert paths == [("new.py",)]
    assert symbols == [("new_symbol",)]
    assert all("old_symbol" not in row[0] for row in snippets)


def test_extract_entities_from_prd_sections(tmp_path):
    prd_path = tmp_path / "todo.md"
    prd_path.write_text(
        "# Todo App\n\n"
        "## Entities / Data Models\n"
        "- **Task**: title (text), description (long text), "
        "status (choices: todo/in_progress/done), due_date (date optional), "
        "user (FK to User)\n"
        "- **Category**: name (string), user (belongs to User)\n",
        encoding="utf-8",
    )

    parsed = MarkdownPRDParser().parse(prd_path)
    entities = extract_entities(parsed)

    task = entities[0]
    assert task.name == "Task"
    assert [field.name for field in task.fields] == [
        "title",
        "description",
        "status",
        "due_date",
        "user",
    ]
    assert task.fields[0].django_type == "CharField"
    assert task.fields[1].django_type == "TextField"
    assert task.fields[2].kwargs["choices"] == ["todo", "in_progress", "done"]
    assert task.fields[3].kwargs["null"] is True
    assert task.fields[4].django_type == "ForeignKey"
    assert task.fields[4].kwargs["to"] == "User"
    assert task.relationships == ["belongs_to:User"]
