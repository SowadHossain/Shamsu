from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from shamsu.abstract.service import AbstractService
from shamsu.patch import git_apply
from shamsu.patch.engine import PatchEngine
from shamsu.patch.file_mutations import FileMutationOps
from shamsu.patch.safety import MutationSafetyError, is_secret_file, validate_mutation_path
from shamsu.patch.transactions import TransactionWorkspace
from shamsu.patch.trash import TrashWorkspace
from shamsu.safety.sandbox import Sandbox
from tests.test_abstract_service import FakeCodebaseMemoryAdapter

PASS_CMD = f'"{sys.executable}" -c "import sys; sys.exit(0)"'
FAIL_CMD = f'"{sys.executable}" -c "raise ValueError(\'boom\')"'


def _payload(reason, operations, patch="", verification_command="", destructive=False):
    return {
        "change_plan": {
            "reason": reason,
            "operations": operations,
            "verification_command": verification_command,
            "destructive": destructive,
        },
        "patch": patch,
    }


def _last_index_payload(workspace: Path) -> dict:
    path = workspace / ".shamsu" / "abstract" / "last-index.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)


# -- path traversal / symlink escape / .git internals ------------------------

def test_execute_change_request_blocks_path_traversal(tmp_path):
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("escape", [{"op": "create_directory", "path": "../outside"}])
    )

    assert result.ok is False
    assert "escapes workspace" in result.error


def test_execute_change_request_blocks_git_internal_edits(tmp_path):
    (tmp_path / ".git").mkdir()
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)
    diff = "--- /dev/null\n+++ b/.git/hooks/evil\n@@ -0,0 +1 @@\n+evil\n"

    result = engine.execute_change_request(
        _payload("touch git internals", [{"op": "create_file", "path": ".git/hooks/evil"}], patch=diff)
    )

    assert result.ok is False
    assert ".git" in result.error
    assert not (tmp_path / ".git" / "hooks" / "evil").exists()


def test_execute_change_request_blocks_symlink_escape(tmp_path):
    outside = tmp_path.parent / f"outside-{tmp_path.name}"
    outside.mkdir(exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks are not permitted in this environment.")

    engine = PatchEngine(workspace, approval_func=lambda _r: True)
    diff = "--- /dev/null\n+++ b/escape/evil.py\n@@ -0,0 +1 @@\n+evil\n"

    result = engine.execute_change_request(
        _payload("write through symlink", [{"op": "create_file", "path": "escape/evil.py"}], patch=diff)
    )

    assert result.ok is False
    assert not (outside / "evil.py").exists()


def test_validate_mutation_path_rejects_git_internals_directly(tmp_path):
    sandbox = Sandbox(tmp_path)

    with pytest.raises(MutationSafetyError):
        validate_mutation_path(sandbox, ".git/config")


# -- secret files -------------------------------------------------------------

def test_is_secret_file_detects_common_patterns():
    assert is_secret_file(".env")
    assert is_secret_file(".env.production")
    assert is_secret_file("config/id_rsa")
    assert is_secret_file("keys/server.pem")
    assert not is_secret_file("app/models.py")


def test_secret_file_edit_is_flagged_and_requires_approval(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    seen_requests = []

    def approve(request):
        seen_requests.append(request)
        return False

    engine = PatchEngine(tmp_path, approval_func=approve)
    diff = "--- a/.env\n+++ b/.env\n@@ -1 +1 @@\n-SECRET=1\n+SECRET=2\n"

    result = engine.execute_change_request(
        _payload("rotate secret", [{"op": "edit_file", "path": ".env"}], patch=diff)
    )

    assert result.ok is False
    assert seen_requests, "approval must be requested before touching a secret-like file"
    assert "secret" in seen_requests[0].description.lower()
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "SECRET=1\n"


# -- git apply --check -------------------------------------------------------

@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_apply_check_validates_before_apply(tmp_path):
    _git_init(tmp_path)
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")

    ok, _ = git_apply.check("--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n", tmp_path)
    assert ok is True

    bad_ok, message = git_apply.check("not a diff", tmp_path)
    assert bad_ok is False
    assert message


def test_engine_rejects_patch_when_git_apply_check_fails(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    monkeypatch.setattr("shamsu.patch.engine.git_apply.available", lambda _root: True)
    monkeypatch.setattr("shamsu.patch.engine.git_apply.check", lambda *_a, **_k: (False, "simulated rejection"))
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("edit", [{"op": "edit_file", "path": "app.py"}], patch=diff)
    )

    assert result.ok is False
    assert "git apply --check" in result.error
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"


# -- valid / invalid diff application -----------------------------------------

def test_valid_patch_applies_and_verification_passes(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("bump value", [{"op": "edit_file", "path": "app.py"}], patch=diff, verification_command=PASS_CMD)
    )

    assert result.ok is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert result.verification.passed is True
    assert result.transaction_id


def test_invalid_diff_rejected_without_mutation(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("bad", [{"op": "edit_file", "path": "app.py"}], patch="not a diff")
    )

    assert result.ok is False
    assert "Invalid diff" in result.error
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"


# -- verification / stall guard -----------------------------------------------

def test_verification_failure_creates_error_packet_and_reports_failure(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("edit", [{"op": "edit_file", "path": "app.py"}], patch=diff, verification_command=FAIL_CMD)
    )

    assert result.ok is False
    assert result.verification.ran is True
    assert result.verification.passed is False
    assert result.verification.error_packet is not None
    assert result.verification.error_packet["root_diagnostics"]


def test_success_only_reported_when_verification_exits_zero(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("edit", [{"op": "edit_file", "path": "app.py"}], patch=diff, verification_command=FAIL_CMD)
    )

    # The file was written, but SHAMSU must never claim success without exit 0.
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"
    assert result.ok is False


def test_repeated_failing_verification_sets_stalled_flag(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    diff1 = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    result1 = engine.execute_change_request(
        _payload("attempt 1", [{"op": "edit_file", "path": "app.py"}], patch=diff1, verification_command=FAIL_CMD)
    )
    assert result1.ok is False
    assert result1.verification.stalled is False

    diff2 = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 2\n+value = 3\n"
    result2 = engine.execute_change_request(
        _payload("attempt 2", [{"op": "edit_file", "path": "app.py"}], patch=diff2, verification_command=FAIL_CMD)
    )
    assert result2.ok is False
    assert result2.verification.stalled is True


# -- create_file / delete / rename / move --------------------------------------

def test_create_file_via_inline_content(tmp_path):
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("new file", [{"op": "create_file", "path": "pkg/new.py", "content": "x = 1\n"}])
    )

    assert result.ok is True
    assert (tmp_path / "pkg" / "new.py").read_text(encoding="utf-8") == "x = 1\n"


def test_create_file_refuses_overwrite_via_diff(tmp_path):
    (tmp_path / "app.py").write_text("existing = 1\n", encoding="utf-8")
    diff = "--- /dev/null\n+++ b/app.py\n@@ -0,0 +1 @@\n+new = 1\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("create over existing", [{"op": "create_file", "path": "app.py"}], patch=diff)
    )

    assert result.ok is False
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "existing = 1\n"


def test_create_file_refuses_overwrite_direct_api(tmp_path):
    (tmp_path / "app.py").write_text("existing = 1\n", encoding="utf-8")
    transactions = TransactionWorkspace(tmp_path)
    txn = transactions.begin("test", [], False)
    ops = FileMutationOps(tmp_path, transactions)

    outcome = ops.create_file(txn, "app.py", "new = 1\n")

    assert outcome.ok is False
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "existing = 1\n"


def test_delete_requires_approval(tmp_path):
    (tmp_path / "old.py").write_text("value = 1\n", encoding="utf-8")
    engine = PatchEngine(tmp_path, approval_func=lambda _r: False)

    result = engine.execute_change_request(
        _payload("cleanup", [{"op": "delete_file", "path": "old.py"}], destructive=True)
    )

    assert result.ok is False
    assert (tmp_path / "old.py").exists()


def test_delete_moves_file_to_trash_not_permanent(tmp_path):
    (tmp_path / "old.py").write_text("value = 1\n", encoding="utf-8")
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("cleanup", [{"op": "delete_file", "path": "old.py"}])
    )

    assert result.ok is True
    assert not (tmp_path / "old.py").exists()
    entries = TrashWorkspace(tmp_path).list_entries()
    assert len(entries) == 1
    assert entries[0].relative_path == "old.py"
    assert (tmp_path / ".shamsu" / "trash" / entries[0].transaction_id / "old.py").read_text(
        encoding="utf-8"
    ) == "value = 1\n"


def test_rename_checks_codebase_memory_references(tmp_path):
    (tmp_path / "old_name.py").write_text("value = 1\n", encoding="utf-8")

    class RecordingAdapter:
        def __init__(self):
            self.calls = []

        def healthcheck(self, workspace):
            from shamsu.abstract.types import CodebaseMemoryHealth

            return CodebaseMemoryHealth(available=True, message="ready")

        def get_references(self, workspace, path):
            self.calls.append(path)
            return {"ok": True, "results": [{"name": "caller_module"}]}

    adapter = RecordingAdapter()
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)
    engine.memory_adapter = adapter

    result = engine.execute_change_request(
        _payload(
            "rename module",
            [{"op": "rename_file", "path": "old_name.py", "dest_path": "new_name.py"}],
            destructive=True,
        )
    )

    assert result.ok is True
    assert adapter.calls == ["old_name.py"]
    assert (tmp_path / "new_name.py").exists()
    assert not (tmp_path / "old_name.py").exists()


# -- transaction backups/hashes + rollback -------------------------------------

def test_transaction_saves_backups_and_hashes(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("edit", [{"op": "edit_file", "path": "app.py"}], patch=diff)
    )

    manifest = engine.transactions.load_manifest(result.transaction_id)
    assert manifest["before_hashes"]["app.py"] is not None
    assert manifest["after_hashes"]["app.py"] is not None
    assert manifest["before_hashes"]["app.py"] != manifest["after_hashes"]["app.py"]
    assert "app.py" in manifest["backups"]
    backup_path = tmp_path / ".shamsu" / "mutations" / result.transaction_id / manifest["backups"]["app.py"]
    assert backup_path.read_text(encoding="utf-8") == "value = 1\n"
    assert engine.transactions.load_patch(result.transaction_id) == diff


def test_rollback_restores_files(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("edit", [{"op": "edit_file", "path": "app.py"}], patch=diff)
    )
    assert result.ok is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 2\n"

    ok, message = engine.rollback_transaction(result.transaction_id)

    assert ok is True
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "value = 1\n"
    manifest = engine.transactions.load_manifest(result.transaction_id)
    assert manifest["status"] == "rolled_back"


def test_rollback_of_create_removes_the_created_file(tmp_path):
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)
    result = engine.execute_change_request(
        _payload("new file", [{"op": "create_file", "path": "pkg/new.py", "content": "x = 1\n"}])
    )
    assert result.ok is True
    assert (tmp_path / "pkg" / "new.py").exists()

    ok, _ = engine.rollback_transaction(result.transaction_id)

    assert ok is True
    assert not (tmp_path / "pkg" / "new.py").exists()


def test_rollback_unknown_transaction_reports_failure(tmp_path):
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    ok, message = engine.rollback_transaction("does-not-exist")

    assert ok is False
    assert "Unknown transaction" in message


# -- formatter only touches changed files --------------------------------------

def test_formatter_only_runs_on_touched_files(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n", encoding="utf-8")
    (tmp_path / "touched.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "untouched.py").write_text("other = 1\n", encoding="utf-8")
    diff = "--- a/touched.py\n+++ b/touched.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"

    seen_commands = []

    class RecordingCommandRunner:
        last_error_packet = None

        def run(self, command, cwd):
            seen_commands.append(command)
            return 0, "", ""

        def run_tests(self, cwd):
            raise NotImplementedError

    engine = PatchEngine(tmp_path, approval_func=lambda _r: True, command_runner=RecordingCommandRunner())

    result = engine.execute_change_request(
        _payload("edit", [{"op": "edit_file", "path": "touched.py"}], patch=diff)
    )

    assert result.ok is True
    assert len(seen_commands) == 1
    assert "touched.py" in seen_commands[0]
    assert "untouched.py" not in seen_commands[0]


def test_formatter_does_not_run_when_unconfigured(tmp_path):
    (tmp_path / "touched.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/touched.py\n+++ b/touched.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"

    seen_commands = []

    class RecordingCommandRunner:
        last_error_packet = None

        def run(self, command, cwd):
            seen_commands.append(command)
            return 0, "", ""

        def run_tests(self, cwd):
            raise NotImplementedError

    engine = PatchEngine(tmp_path, approval_func=lambda _r: True, command_runner=RecordingCommandRunner())
    engine.execute_change_request(_payload("edit", [{"op": "edit_file", "path": "touched.py"}], patch=diff))

    assert seen_commands == []


# -- Codebase-Memory MCP refresh on success/failure -----------------------------

def test_successful_mutation_marks_code_memory_stale(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    adapter = FakeCodebaseMemoryAdapter(available=True)
    AbstractService(tmp_path, adapter=adapter).ensure_ready()
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("edit", [{"op": "edit_file", "path": "app.py"}], patch=diff)
    )

    assert result.ok is True
    assert _last_index_payload(tmp_path).get("forced_stale") is True


def test_failed_mutation_does_not_mark_code_memory_stale(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    adapter = FakeCodebaseMemoryAdapter(available=True)
    AbstractService(tmp_path, adapter=adapter).ensure_ready()
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("bad", [{"op": "edit_file", "path": "app.py"}], patch="not a diff")
    )

    assert result.ok is False
    assert _last_index_payload(tmp_path).get("forced_stale") is not True


def test_verification_failed_mutation_does_not_mark_code_memory_stale(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    adapter = FakeCodebaseMemoryAdapter(available=True)
    AbstractService(tmp_path, adapter=adapter).ensure_ready()
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("edit", [{"op": "edit_file", "path": "app.py"}], patch=diff, verification_command=FAIL_CMD)
    )

    assert result.ok is False
    assert _last_index_payload(tmp_path).get("forced_stale") is not True


# -- malformed model output is never trusted blindly ----------------------------

def test_execute_change_request_rejects_missing_change_plan(tmp_path):
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request({"patch": "whatever"})

    assert result.ok is False
    assert "change_plan" in result.error


def test_execute_change_request_rejects_unknown_operation(tmp_path):
    engine = PatchEngine(tmp_path, approval_func=lambda _r: True)

    result = engine.execute_change_request(
        _payload("bad op", [{"op": "format_disk", "path": "app.py"}])
    )

    assert result.ok is False
    assert "Unknown operation" in result.error
