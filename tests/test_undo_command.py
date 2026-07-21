"""Gap G2: rollback existed but was undiscoverable.

Every model-driven write already opened a transaction with a backup, and
`/patch rollback <id>` could restore it - but using that meant knowing the
command existed AND digging the right id out of `.shamsu/mutations`. Nobody
does that in the moment the agent just mangled their code. `/undo` resolves
"the last change" so the safety net is actually reachable.
"""
from __future__ import annotations

import io
from pathlib import Path

from rich.console import Console

import shamsu.cli.repl as repl
from shamsu.patch.rollback import latest_undoable_transaction, rollback_transaction
from shamsu.patch.transactions import TransactionWorkspace


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


def _write_through_transaction(workspace: Path, name: str, before: str, after: str) -> str:
    """Mimic what agent_tools does for a model-driven edit: open a transaction,
    back the file up, then write the new content."""
    path = workspace / name
    path.write_text(before, encoding="utf-8")
    store = TransactionWorkspace(workspace)
    transaction_id = store.begin(
        reason=f"edit {name}",
        operations=[{"op": "edit_file", "path": name, "dest_path": "", "reason": ""}],
        destructive=False,
    )
    store.backup_file(transaction_id, name)
    path.write_text(after, encoding="utf-8")
    return transaction_id


def test_latest_undoable_is_none_on_a_clean_workspace(tmp_path: Path):
    assert latest_undoable_transaction(tmp_path) is None


def test_latest_undoable_picks_the_most_recent(tmp_path: Path):
    """Written back-to-back, so both ids almost always share the same
    second-resolution timestamp prefix and differ only by a RANDOM uuid suffix.
    Ordering by id sorts these arbitrarily - `/undo` would revert whichever
    uuid happened to sort higher. Agent writes are routinely this close
    together, so ordering must come from the manifest's microsecond
    `created_at` instead."""
    first = _write_through_transaction(tmp_path, "a.py", "old a", "new a")
    second = _write_through_transaction(tmp_path, "b.py", "old b", "new b")

    latest = latest_undoable_transaction(tmp_path)
    assert latest is not None
    assert latest[0] == second != first


def test_latest_undoable_is_stable_across_many_same_second_writes(tmp_path: Path):
    """Guards the ordering fix against the uuid lottery: 12 rapid writes in one
    workspace must always resolve to the last one, not a random one."""
    ids = [
        _write_through_transaction(tmp_path, f"f{index}.py", "old", "new")
        for index in range(12)
    ]
    latest = latest_undoable_transaction(tmp_path)
    assert latest is not None
    assert latest[0] == ids[-1]


def test_latest_undoable_skips_already_rolled_back(tmp_path: Path):
    """Undoing twice must step further back, not re-undo the same change."""
    first = _write_through_transaction(tmp_path, "a.py", "old a", "new a")
    second = _write_through_transaction(tmp_path, "b.py", "old b", "new b")

    ok, _ = rollback_transaction(tmp_path, second)
    assert ok
    latest = latest_undoable_transaction(tmp_path)
    assert latest is not None and latest[0] == first


def test_undo_restores_the_file(tmp_path: Path, monkeypatch):
    _write_through_transaction(tmp_path, "game.js", "original", "mangled")
    assert (tmp_path / "game.js").read_text(encoding="utf-8") == "mangled"

    monkeypatch.setattr(repl, "_make_approval_manager", lambda *a, **k: _AlwaysApprove())
    console = Console(record=True, width=100)
    repl._handle_undo(tmp_path, console, session_logger=None)

    assert (tmp_path / "game.js").read_text(encoding="utf-8") == "original"
    out = console.export_text()
    assert "game.js" in out


def test_undo_respects_a_denied_approval(tmp_path: Path, monkeypatch):
    """Undo overwrites current content - a 'no' must leave the file alone."""
    _write_through_transaction(tmp_path, "game.js", "original", "mangled")

    monkeypatch.setattr(repl, "_make_approval_manager", lambda *a, **k: _AlwaysDeny())
    console = Console(record=True, width=100)
    repl._handle_undo(tmp_path, console, session_logger=None)

    assert (tmp_path / "game.js").read_text(encoding="utf-8") == "mangled"
    assert "cancelled" in console.export_text().lower()


def test_undo_on_a_clean_workspace_says_so(tmp_path: Path):
    console = Console(record=True, width=100)
    repl._handle_undo(tmp_path, console, session_logger=None)
    assert "Nothing to undo" in console.export_text()


def test_undo_is_a_registered_command_and_in_help():
    from shamsu.cli.command_router import CommandRouter

    router = CommandRouter(repl.SYSTEM_COMMANDS)
    route = router.route("/undo")
    assert route.valid
    assert route.normalized == "undo"


class _AlwaysApprove:
    def ask(self, _request):  # noqa: ANN001
        return True


class _AlwaysDeny:
    def ask(self, _request):  # noqa: ANN001
        return False
