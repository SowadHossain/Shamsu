from __future__ import annotations

from shamsu.indexer.walker import FileWalker
from shamsu.prd.extractor import extract_entities
from shamsu.prd.parser import MarkdownPRDParser


def test_file_walker_discover_skips_inaccessible_reparse_points(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    broken = tmp_path / "broken_symlink.bin"
    broken.write_bytes(b"")

    from pathlib import Path as PathClass

    real_is_file = PathClass.is_file

    def flaky_is_file(self, *args, **kwargs):
        if self.name == "broken_symlink.bin":
            raise OSError(1920, "The file cannot be accessed by the system")
        return real_is_file(self, *args, **kwargs)

    monkeypatch.setattr(PathClass, "is_file", flaky_is_file)

    discovered = FileWalker(tmp_path).discover()

    names = {path.name for path in discovered}
    assert "app.py" in names
    assert "broken_symlink.bin" not in names


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
