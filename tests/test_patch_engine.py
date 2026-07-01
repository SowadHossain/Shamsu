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


def test_apply_and_rollback_do_not_mutate_files_in_this_slice(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    engine = PatchEngine(tmp_path)

    assert engine.apply(VALID_DIFF, tmp_path) is False
    assert engine.rollback(target) is False
    assert target.read_text(encoding="utf-8") == "value = 1\n"
