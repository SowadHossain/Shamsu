from __future__ import annotations

from io import StringIO

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


def test_hunk_line_count_mismatch_fails(tmp_path):
    diff = """--- a/app/models.py
+++ b/app/models.py
@@ -1,2 +1,2 @@
-old
+new
"""
    engine = PatchEngine(tmp_path)

    ok, error = engine.validate_diff(diff)

    assert ok is False
    assert "line count mismatch" in error


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
