from __future__ import annotations

from io import StringIO

import pytest
from rich.console import Console

from shamsu.patch.engine import PatchEngine, parse_unified_diff
from shamsu.patch.preview import print_diff_preview
from shamsu.safety.sandbox import Sandbox


VALID_DIFF = """--- a/app/models.py
+++ b/app/models.py
@@ -1,3 +1,4 @@
 class Task:
     title = ""
-    done = False
+    status = "open"
+    priority = "medium"
"""


def test_valid_single_file_unified_diff_passes(tmp_path):
    engine = PatchEngine(tmp_path)

    ok, error = engine.validate_diff(VALID_DIFF)

    assert ok is True
    assert error is None


def test_valid_multi_file_unified_diff_passes(tmp_path):
    diff = VALID_DIFF + """--- a/app/views.py
+++ b/app/views.py
@@ -1 +1,2 @@
 def index():
+    return "ok"
"""
    engine = PatchEngine(tmp_path)

    ok, error = engine.validate_diff(diff)

    assert ok is True
    assert error is None


def test_missing_plus_header_fails(tmp_path):
    diff = """--- a/app/models.py
@@ -1 +1 @@
-old
+new
"""
    engine = PatchEngine(tmp_path)

    ok, error = engine.validate_diff(diff)

    assert ok is False
    assert "Missing +++ header" in error


def test_malformed_hunk_header_fails(tmp_path):
    diff = """--- a/app/models.py
+++ b/app/models.py
@@ bad header @@
-old
+new
"""
    engine = PatchEngine(tmp_path)

    ok, error = engine.validate_diff(diff)

    assert ok is False
    assert "Malformed hunk header" in error


def test_hunk_count_mismatch_is_recounted_not_rejected(tmp_path):
    """A wrong @@ line count (declared 1,2/1,2 for a one-line change) is a
    routine local-model mistake - it must be recounted from the body and
    applied, not rejected. Regression guard for the 'invalid diff' failures."""
    target = tmp_path / "app" / "models.py"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    diff = """--- a/app/models.py
+++ b/app/models.py
@@ -1,2 +1,2 @@
-old
+new
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    ok, error = engine.validate_diff(diff)
    assert ok is True, error
    assert engine.apply(diff, tmp_path) is True
    assert target.read_text(encoding="utf-8") == "new\n"


def test_path_escape_fails(tmp_path):
    diff = """--- a/../outside.py
+++ b/../outside.py
@@ -1 +1 @@
-old
+new
"""
    engine = PatchEngine(tmp_path)

    ok, error = engine.validate_diff(diff)

    assert ok is False
    assert "escapes workspace" in error


def test_dev_null_file_creation_path_parses_safely(tmp_path):
    diff = """--- /dev/null
+++ b/app/new_file.py
@@ -0,0 +1,2 @@
+def created():
+    return True
"""
    patches = parse_unified_diff(diff, Sandbox(tmp_path))

    assert patches[0].old_path == "/dev/null"
    assert patches[0].new_path == "app/new_file.py"
    assert patches[0].additions == 2


def test_rich_preview_includes_file_names_and_changed_lines(tmp_path):
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)

    print_diff_preview(VALID_DIFF, console=console, sandbox=Sandbox(tmp_path))

    rendered = output.getvalue()
    assert "Patch Preview" in rendered
    assert "app/models.py" in rendered
    assert '+    priority = "medium"' in rendered
    assert '-    done = False' in rendered


def test_apply_denies_without_mutating_when_approval_rejects(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: False)

    assert engine.apply(diff, tmp_path) is False
    assert target.read_text(encoding="utf-8") == "value = 1\n"
    assert not (tmp_path / "app.py.bak").exists()


def test_apply_shows_preview_before_file_edit_approval(monkeypatch, tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
    events: list[str] = []
    requests = []

    def preview(diff_text, console=None, sandbox=None):
        assert diff_text == diff
        events.append("preview")

    def approve(request):
        events.append("approval")
        requests.append(request)
        return True

    monkeypatch.setattr("shamsu.patch.preview.print_diff_preview", preview)
    engine = PatchEngine(tmp_path, approval_func=approve)

    assert engine.apply(diff, tmp_path) is True
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert events == ["preview", "approval"]
    assert requests[0].action_type == "file_edit"
    assert requests[0].preview == diff


def test_apply_modifies_file_and_creates_backup_after_approval(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    assert engine.apply(diff, tmp_path) is True
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert (tmp_path / "app.py.bak").read_text(encoding="utf-8") == "value = 1\n"


def test_apply_cancellation_restores_every_partial_change(tmp_path, monkeypatch):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-value = 1
+value = 2
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-value = 1
+value = 2
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)
    original_apply = engine._apply_file_patch
    calls = 0

    def cancel_after_first(patch, backups, created_files):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        original_apply(patch, backups, created_files)

    monkeypatch.setattr(engine, "_apply_file_patch", cancel_after_first)

    with pytest.raises(KeyboardInterrupt):
        engine.apply_result(diff, tmp_path)

    assert first.read_text(encoding="utf-8") == "value = 1\n"
    assert second.read_text(encoding="utf-8") == "value = 1\n"


def test_apply_tolerates_wrong_hunk_line_numbers(tmp_path):
    """Regression: a diff whose @@ header points at the wrong line (very common
    from local models) must still apply by locating the context, not reject."""
    target = tmp_path / "app.py"
    target.write_text("import os\n\n\ndef foo():\n    return 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -99,2 +99,2 @@
 def foo():
-    return 1
+    return 42
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    assert engine.apply(diff, tmp_path) is True
    assert target.read_text(encoding="utf-8") == "import os\n\n\ndef foo():\n    return 42\n"


def test_apply_tolerates_trailing_whitespace_in_context(tmp_path):
    """Regression: trailing-whitespace drift on a context line must not reject
    the patch, and the file's real bytes on untouched lines are preserved."""
    target = tmp_path / "app.py"
    target.write_text("def foo():\n    x = 1   \n    return x\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1,3 +1,3 @@
 def foo():
     x = 1
-    return x
+    return x + 1
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    assert engine.apply(diff, tmp_path) is True
    # The untouched 'x = 1' line keeps its original trailing spaces.
    assert target.read_text(encoding="utf-8") == "def foo():\n    x = 1   \n    return x + 1\n"


def test_rollback_restores_backup(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    backup = tmp_path / "app.py.bak"
    backup.write_text("value = 0\n", encoding="utf-8")
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    assert engine.rollback(target) is True
    assert target.read_text(encoding="utf-8") == "value = 0\n"
    assert not backup.exists()


def test_apply_invalid_diff_returns_false(tmp_path):
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    assert engine.apply("not a diff", tmp_path) is False


def test_apply_result_preserves_validation_failure_status(tmp_path):
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    result = engine.apply_result("not a diff", tmp_path)

    assert result.ok is False
    assert result.status == "validation_failed"
    assert result.error.startswith("DiffValidationError:")


def test_apply_result_preserves_denied_status(tmp_path):
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _request: False)

    result = engine.apply_result(diff, tmp_path)

    assert result.ok is False
    assert result.status == "denied"
    assert result.error == "Patch denied by user."


def test_apply_result_preserves_exact_context_mismatch_error(tmp_path):
    (tmp_path / "app.py").write_text("actual = 1\n", encoding="utf-8")
    diff = "--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-expected = 1\n+expected = 2\n"
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    result = engine.apply_result(diff, tmp_path)

    assert result.ok is False
    assert result.status == "apply_failed"
    assert result.error == (
        "Patch application failed after approval: Patch application modifies files inside "
        "the selected workspace. DiffValidationError: Patch context does not match target file."
    )


def test_model_context_label_is_removed_before_patch_application(tmp_path):
    target = tmp_path / "qa_probe.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    diff = """--- a/qa_probe.py
+++ b/qa_probe.py
@@ -1,3 +1,2 @@
 # File: qa_probe.py (lines 1-2)
 def add(a, b):
-    return a - b
+    return a + b
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    result = engine.apply_result(diff, tmp_path)

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"


def test_real_added_file_comment_is_not_sanitized(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1,2 @@
+# File: generated.py (lines 1-2)
 value = 1
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    assert engine.apply(diff, tmp_path) is True
    assert target.read_text(encoding="utf-8").startswith("# File: generated.py")


def test_apply_restores_backup_on_context_mismatch(tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("actual = 1\n", encoding="utf-8")
    diff = """--- a/first.py
+++ b/first.py
@@ -1 +1 @@
-value = 1
+value = 2
--- a/second.py
+++ b/second.py
@@ -1 +1 @@
-expected = 1
+expected = 2
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    assert engine.apply(diff, tmp_path) is False
    assert first.read_text(encoding="utf-8") == "value = 1\n"
    assert second.read_text(encoding="utf-8") == "actual = 1\n"


def test_apply_creates_new_file_inside_workspace(tmp_path):
    diff = """--- /dev/null
+++ b/pkg/new_file.py
@@ -0,0 +1,2 @@
+def created():
+    return True
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    assert engine.apply(diff, tmp_path) is True
    assert (tmp_path / "pkg" / "new_file.py").read_text(encoding="utf-8") == (
        "def created():\n    return True\n"
    )


def test_apply_deletes_file_with_backup_after_approval(tmp_path):
    target = tmp_path / "old.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-value = 1
"""
    requests = []

    def approve(request):
        requests.append(request)
        return True

    engine = PatchEngine(tmp_path, approval_func=approve)

    assert engine.apply(diff, tmp_path) is True
    assert not target.exists()
    assert (tmp_path / "old.py.bak").read_text(encoding="utf-8") == "value = 1\n"
    assert requests[0].action_type == "file_delete"


def test_apply_rejects_outside_workspace_path(tmp_path):
    diff = """--- a/../outside.py
+++ b/../outside.py
@@ -1 +1 @@
-old
+new
"""
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True)

    assert engine.apply(diff, tmp_path) is False


def test_apply_logs_patch_applied_event_when_session_logger_provided(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("def old_name():\n    return 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1,2 +1,2 @@
-def old_name():
+def new_name():
     return 1
"""

    class RecordingLogger:
        def __init__(self):
            self.events = []

        def log(self, event_type, payload, summary, workflow_id=None):
            self.events.append(event_type)

    logger = RecordingLogger()
    engine = PatchEngine(tmp_path, approval_func=lambda _request: True, session_logger=logger)

    assert engine.apply(diff, tmp_path) is True
    assert "patch.applied" in logger.events
