"""Gap D2: the agent had no move/delete tools.

Any refactor that relocated a file forced the model into `run_command` shell
hacks (mv/move - allowlist-dependent, POSIX/Windows-divergent) or into
write-new-and-leave-the-old, littering dead files that then pollute
search_index results and future context packs.

Both new tools go through the same transaction machinery as every other
model-driven write, so `/undo` covers them: a model deleting the wrong file
must never be unrecoverable.
"""
from __future__ import annotations

from pathlib import Path

from shamsu.patch.rollback import latest_undoable_transaction, rollback_transaction
from shamsu.tools.agent_tools import AgentToolRegistry


def _registry(workspace: Path, approve: bool = True) -> AgentToolRegistry:
    return AgentToolRegistry(workspace, approval_func=lambda _request: approve)


# --- move ---------------------------------------------------------------------


def test_move_file_relocates_and_is_undoable(tmp_path: Path):
    (tmp_path / "old.py").write_text("x = 1", encoding="utf-8")

    result = _registry(tmp_path).move_file("old.py", "src/new.py")

    assert result.ok, result.message
    assert not (tmp_path / "old.py").exists()
    assert (tmp_path / "src" / "new.py").read_text(encoding="utf-8") == "x = 1"

    # The move is a real transaction, so /undo can reach it.
    latest = latest_undoable_transaction(tmp_path)
    assert latest is not None
    ok, _ = rollback_transaction(tmp_path, latest[0])
    assert ok
    assert (tmp_path / "old.py").read_text(encoding="utf-8") == "x = 1"


def test_move_file_refuses_to_clobber_the_destination(tmp_path: Path):
    (tmp_path / "a.py").write_text("keep me", encoding="utf-8")
    (tmp_path / "b.py").write_text("do not lose me", encoding="utf-8")

    result = _registry(tmp_path).move_file("a.py", "b.py")

    assert not result.ok
    assert "already exists" in result.message
    assert (tmp_path / "b.py").read_text(encoding="utf-8") == "do not lose me"
    assert (tmp_path / "a.py").exists()


def test_move_file_rejects_a_missing_source(tmp_path: Path):
    result = _registry(tmp_path).move_file("ghost.py", "new.py")
    assert not result.ok
    assert "not a file" in result.message


def test_move_file_rejects_escaping_the_workspace(tmp_path: Path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    result = _registry(tmp_path).move_file("a.py", "../escaped.py")
    assert not result.ok
    assert (tmp_path / "a.py").exists()


def test_move_file_respects_a_denied_approval(tmp_path: Path):
    (tmp_path / "a.py").write_text("x", encoding="utf-8")

    result = _registry(tmp_path, approve=False).move_file("a.py", "b.py")

    assert not result.ok
    assert (tmp_path / "a.py").exists()
    assert not (tmp_path / "b.py").exists()


# --- delete -------------------------------------------------------------------


def test_delete_file_removes_and_is_undoable(tmp_path: Path):
    (tmp_path / "dead.py").write_text("obsolete", encoding="utf-8")

    result = _registry(tmp_path).delete_file("dead.py")

    assert result.ok, result.message
    assert not (tmp_path / "dead.py").exists()
    assert "/undo" in result.message

    latest = latest_undoable_transaction(tmp_path)
    assert latest is not None
    ok, _ = rollback_transaction(tmp_path, latest[0])
    assert ok
    assert (tmp_path / "dead.py").read_text(encoding="utf-8") == "obsolete"


def test_delete_file_respects_a_denied_approval(tmp_path: Path):
    (tmp_path / "keep.py").write_text("important", encoding="utf-8")

    result = _registry(tmp_path, approve=False).delete_file("keep.py")

    assert not result.ok
    assert (tmp_path / "keep.py").read_text(encoding="utf-8") == "important"


def test_delete_file_rejects_a_missing_target(tmp_path: Path):
    result = _registry(tmp_path).delete_file("ghost.py")
    assert not result.ok


# --- wiring -------------------------------------------------------------------


def test_tools_are_exposed_to_the_model_and_dispatch(tmp_path: Path):
    registry = _registry(tmp_path)
    names = {
        (schema.get("function") or {}).get("name") for schema in registry.tool_schemas()
    }
    assert {"move_file", "delete_file"} <= names

    (tmp_path / "a.py").write_text("x", encoding="utf-8")
    assert registry.execute("move_file", {"source": "a.py", "destination": "b.py"}).ok
    assert registry.execute("delete_file", {"filepath": "b.py"}).ok
