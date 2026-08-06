from __future__ import annotations

from pathlib import Path

from rich.console import Console

from shamsu.cli.repl import _handle_diagnostics
from shamsu.diagnostics import doctor as diagnostics_doctor
from shamsu.diagnostics.setup import DiagnosticsWorkspace


# -- 32. /diagnostics status shows helper availability ------------------------

def test_diagnostics_doctor_status_reports_helper_availability(tmp_path: Path):
    payload = diagnostics_doctor.check(tmp_path)

    assert payload["native_structured_output"] is True
    assert payload["errorformat_style_parser"] is True
    assert "drain3_available" in payload
    assert "llmlingua_enabled" in payload
    assert payload["llmlingua_enabled"] is False


def test_diagnostics_doctor_format_report_mentions_fallback_when_drain3_missing(tmp_path: Path):
    payload = diagnostics_doctor.check(tmp_path)
    payload["drain3_available"] = False

    report = diagnostics_doctor.format_report(payload)

    assert "fallback template-miner" in report or "drain3 (runtime log compaction): unavailable" in report


def test_repl_diagnostics_status_command_prints_report(tmp_path: Path):
    console = Console(record=True)

    _handle_diagnostics("diagnostics status", tmp_path, console)

    output = console.export_text()
    assert "Diagnostics doctor" in output


# -- 33. /diagnostics last shows the latest packet ----------------------------

def test_repl_diagnostics_last_shows_latest_packet_summary(tmp_path: Path):
    ws = DiagnosticsWorkspace(tmp_path)
    ws.save_packet(
        {
            "command": "npm run build",
            "exit_code": 1,
            "summary": "npm build failed: TS1005 syntax_error",
            "root_diagnostics": [{"category": "syntax_error", "code": "TS1005", "file": "a.ts", "line": 5, "message": "')' expected"}],
            "target_files": ["a.ts"],
            "recommended_snippets": [{"file": "a.ts", "line_start": 1, "line_end": 10, "reason": "syntax_error"}],
            "parser_chain": ["typescript_fallback"],
        }
    )
    console = Console(record=True)

    _handle_diagnostics("diagnostics last", tmp_path, console)

    output = console.export_text()
    assert "npm build failed" in output
    assert "TS1005" in output
    assert "a.ts" in output


def test_repl_diagnostics_last_reports_when_no_packet_recorded(tmp_path: Path):
    console = Console(record=True)

    _handle_diagnostics("diagnostics last", tmp_path, console)

    assert "No ErrorPacket recorded yet" in console.export_text()


# -- 34. /diagnostics sources shows which parser/helper handled the output ----

def test_repl_diagnostics_sources_shows_parser_chain(tmp_path: Path):
    ws = DiagnosticsWorkspace(tmp_path)
    ws.save_packet({"command": "tsc", "exit_code": 1, "parser_chain": ["typescript_fallback"]})
    console = Console(record=True)

    _handle_diagnostics("diagnostics sources", tmp_path, console)

    assert "typescript_fallback" in console.export_text()


def test_repl_diagnostics_explain_describes_root_cause_rule(tmp_path: Path):
    ws = DiagnosticsWorkspace(tmp_path)
    ws.save_packet(
        {
            "command": "npm run build",
            "exit_code": 1,
            "root_diagnostics": [{"category": "missing_export", "code": "", "message": "missing export GameLoop"}],
        }
    )
    console = Console(record=True)

    _handle_diagnostics("diagnostics explain", tmp_path, console)

    output = console.export_text()
    assert "root cause" in output.lower()
    assert "missing export GameLoop" in output


# -- 35. secrets are redacted in diagnostics doctor/status too ----------------

def test_diagnostics_workspace_files_never_contain_persisted_secrets(tmp_path: Path):
    from shamsu.diagnostics.digest import DiagnosticDigest

    digest = DiagnosticDigest(tmp_path)
    packet = digest.run(
        "python app.py",
        tmp_path,
        1,
        "",
        "app.py:1: error: SECRET_KEY = \"django-insecure-secret\"\n",
    )
    ws = DiagnosticsWorkspace(tmp_path)
    ws.save_packet(packet.to_dict())

    packet_path = tmp_path / ".shamsu" / "diagnostics" / "last-error-packet.json"
    assert "django-insecure-secret" not in packet_path.read_text(encoding="utf-8")
