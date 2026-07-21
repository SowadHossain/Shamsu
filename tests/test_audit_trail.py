from __future__ import annotations

import json
from pathlib import Path

from shamsu.patch.engine import PatchEngine
from shamsu.safety.audit import AuditLogger, log_generated_files
from shamsu.tools.executor import DENIED_EXIT_CODE, CommandRunner


def test_audit_logger_writes_redacted_event_shape_inside_workspace(tmp_path: Path):
    log_path = AuditLogger(tmp_path).log(
        "file_write",
        "success",
        affected_paths=["app.py"],
        details={"message": "password = \"abc123\""},
    )

    assert log_path == tmp_path / ".shamsu" / "audit.jsonl"
    event = _events(log_path)[0]
    assert set(event) == {"timestamp", "action_type", "result", "affected_paths", "details"}
    assert event["action_type"] == "file_write"
    assert event["result"] == "success"
    assert event["affected_paths"] == ["app.py"]
    assert "[REDACTED]" in event["details"]["message"]
    assert "abc123" not in log_path.read_text(encoding="utf-8")


def test_generated_files_are_logged_with_paths(tmp_path: Path):
    (tmp_path / "generated.py").write_text("value = 1\n", encoding="utf-8")

    log_generated_files(tmp_path, ["generated.py"])

    event = _events(tmp_path / ".shamsu" / "audit.jsonl")[0]
    assert event["action_type"] == "generated_files"
    assert event["result"] == "success"
    assert event["affected_paths"] == ["generated.py"]


def test_command_runner_logs_denied_approval_and_command(tmp_path: Path):
    runner = CommandRunner(tmp_path, approval_func=lambda _request: False)

    code, _stdout, _stderr = runner.run("python -c \"print('secret')\"", tmp_path)

    assert code == DENIED_EXIT_CODE
    events = _events(tmp_path / ".shamsu" / "audit.jsonl")
    assert [event["action_type"] for event in events] == ["approval", "command_run"]
    assert [event["result"] for event in events] == ["denied", "denied"]


def test_patch_apply_logs_approval_and_affected_path(tmp_path: Path):
    target = tmp_path / "app.py"
    target.write_text("value = 1\n", encoding="utf-8")
    diff = """--- a/app.py
+++ b/app.py
@@ -1 +1 @@
-value = 1
+value = 2
"""

    assert PatchEngine(tmp_path, approval_func=lambda _request: True).apply(diff, tmp_path)

    events = _events(tmp_path / ".shamsu" / "audit.jsonl")
    assert [event["action_type"] for event in events[-2:]] == ["approval", "patch_apply"]
    assert events[-1]["result"] == "success"
    assert events[-1]["affected_paths"] == ["app.py"]


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
