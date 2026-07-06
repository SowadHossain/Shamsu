from __future__ import annotations

from io import StringIO

from rich.console import Console

from shamsu.ui.progress import ProgressReporter


def test_progress_reporter_redacts_secrets_in_output():
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=100)

    reporter = ProgressReporter(console=console)
    reporter.step("Loaded password = \"abc123\" from config")

    rendered = output.getvalue()
    assert "[REDACTED]" in rendered
    assert "abc123" not in rendered


def test_progress_reporter_logs_session_events():
    class FakeLogger:
        def __init__(self):
            self.events = []

        def log(self, event_type, payload, summary, workflow_id=None):
            self.events.append((event_type, payload, summary, workflow_id))

    logger = FakeLogger()
    reporter = ProgressReporter(session_logger=logger)

    reporter.tool_start("read_file", "file=app.py")

    assert logger.events
    assert logger.events[0][0] == "progress.event"
    assert logger.events[0][1]["kind"] == "progress.tool_start"
