from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

from shamsu.cli.repl import _handle_patch


@pytest.fixture(autouse=True)
def _auto_approve_menu(monkeypatch):
    """/patch drives PatchEngine through the same interactive approval menu
    the real REPL uses. Tests exercise the CLI handler directly (not a live
    terminal), so auto-approve instead of blocking on stdin."""
    monkeypatch.setattr("shamsu.cli.repl.ask_approval_menu", lambda request, offer_remember=False, console=None: (True, ""))


def _write_change_request(workspace: Path, name: str, reason: str, operations: list[dict], patch: str = "", verification_command: str = "") -> str:
    payload = {
        "change_plan": {
            "reason": reason,
            "operations": operations,
            "verification_command": verification_command,
            "destructive": False,
        },
        "patch": patch,
    }
    path = workspace / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return name


def test_patch_status_reports_no_transactions_initially(tmp_path):
    console = Console(record=True)

    _handle_patch("patch status", tmp_path, console)

    output = console.export_text()
    assert "No transactions recorded yet" in output


def test_patch_apply_command_runs_change_request_json(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    request_name = _write_change_request(
        tmp_path, "change.json", "bump value", [{"op": "edit_file", "path": "app.py"}], patch=diff
    )
    console = Console(record=True)

    _handle_patch(f"patch apply {request_name}", tmp_path, console)

    output = console.export_text()
    assert "Applied" in output
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"


def test_patch_apply_command_reports_rejection_without_mutation(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    request_name = _write_change_request(
        tmp_path, "change.json", "bad", [{"op": "edit_file", "path": "app.py"}], patch="not a diff"
    )
    console = Console(record=True)

    _handle_patch(f"patch apply {request_name}", tmp_path, console)

    output = console.export_text()
    assert "Patch Rejected" in output
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_patch_preview_command_does_not_mutate(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    request_name = _write_change_request(
        tmp_path, "change.json", "bump value", [{"op": "edit_file", "path": "app.py"}], patch=diff
    )
    console = Console(record=True)

    _handle_patch(f"patch preview {request_name}", tmp_path, console)

    output = console.export_text()
    assert "bump value" in output
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"


def test_patch_journal_and_last_after_apply(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    request_name = _write_change_request(
        tmp_path, "change.json", "bump value", [{"op": "edit_file", "path": "app.py"}], patch=diff
    )
    apply_console = Console(record=True)
    _handle_patch(f"patch apply {request_name}", tmp_path, apply_console)

    journal_console = Console(record=True)
    _handle_patch("patch journal", tmp_path, journal_console)
    last_console = Console(record=True)
    _handle_patch("patch last", tmp_path, last_console)

    journal_output = journal_console.export_text()
    assert "bump value" in journal_output
    assert "applied" in journal_output
    assert "bump value" in last_console.export_text()


def test_patch_diff_command_shows_stored_patch(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    request_name = _write_change_request(
        tmp_path, "change.json", "bump value", [{"op": "edit_file", "path": "app.py"}], patch=diff
    )
    _handle_patch(f"patch apply {request_name}", tmp_path, Console(record=True))

    from shamsu.patch.journal import MutationJournal

    transaction_id = MutationJournal(tmp_path).last()["transaction_id"]
    diff_console = Console(record=True)

    _handle_patch(f"patch diff {transaction_id}", tmp_path, diff_console)

    assert "app.py" in diff_console.export_text()


def test_patch_rollback_command_restores_file(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    request_name = _write_change_request(
        tmp_path, "change.json", "bump value", [{"op": "edit_file", "path": "app.py"}], patch=diff
    )
    _handle_patch(f"patch apply {request_name}", tmp_path, Console(record=True))
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"

    from shamsu.patch.journal import MutationJournal

    transaction_id = MutationJournal(tmp_path).last()["transaction_id"]
    rollback_console = Console(record=True)

    _handle_patch(f"patch rollback {transaction_id}", tmp_path, rollback_console)

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    assert "Restored" in rollback_console.export_text()


def test_patch_trash_and_clean_trash_commands(tmp_path):
    (tmp_path / "old.py").write_text("value = 1\n", encoding="utf-8")
    request_name = _write_change_request(
        tmp_path, "change.json", "cleanup", [{"op": "delete_file", "path": "old.py"}]
    )
    _handle_patch(f"patch apply {request_name}", tmp_path, Console(record=True))
    assert not (tmp_path / "old.py").exists()

    trash_console = Console(record=True)
    _handle_patch("patch trash", tmp_path, trash_console)
    assert "old.py" in trash_console.export_text()

    clean_console = Console(record=True)
    _handle_patch("patch clean-trash", tmp_path, clean_console)
    assert "Permanently removed 1" in clean_console.export_text()

    empty_console = Console(record=True)
    _handle_patch("patch trash", tmp_path, empty_console)
    assert "Trash is empty" in empty_console.export_text()
