from __future__ import annotations

from shamsu.diagnostics.types import DiagnosticRecord, ErrorPacket


def test_error_packet_signature_uses_root_diagnostics():
    packet = ErrorPacket(
        command="tsc",
        exit_code=1,
        root_diagnostics=[
            DiagnosticRecord(category="syntax_error", code="TS1005", file="a.ts", line=5, message="')' expected")
        ],
    )
    other = ErrorPacket(
        command="tsc",
        exit_code=1,
        root_diagnostics=[
            DiagnosticRecord(category="syntax_error", code="TS1005", file="a.ts", line=5, message="')' expected")
        ],
    )
    different = ErrorPacket(
        command="tsc",
        exit_code=1,
        root_diagnostics=[
            DiagnosticRecord(category="syntax_error", code="TS1006", file="a.ts", line=5, message="something else")
        ],
    )

    assert packet.signature() == other.signature()
    assert packet.signature() != different.signature()


def test_error_packet_signature_falls_back_to_exit_code_when_no_diagnostics():
    packet = ErrorPacket(command="npm test", exit_code=0)

    assert packet.signature() == "exit=0"


def test_error_packet_ok_true_only_when_clean_exit_and_no_root_diagnostics():
    assert ErrorPacket(exit_code=0).ok is True
    assert ErrorPacket(exit_code=1).ok is False
    assert ErrorPacket(exit_code=0, root_diagnostics=[DiagnosticRecord()]).ok is False
    assert ErrorPacket(exit_code=0).classification == "success"
    assert ErrorPacket(exit_code=1).classification == "command_failure"
    assert ErrorPacket(exit_code=1).actionable is True


def test_error_packet_to_model_context_includes_root_diagnostics_and_snippets():
    from shamsu.diagnostics.types import RecommendedSnippet

    packet = ErrorPacket(
        command="tsc",
        exit_code=1,
        summary="tsc failed",
        root_diagnostics=[DiagnosticRecord(category="syntax_error", code="TS1005", file="a.ts", line=5, message="oops")],
        recommended_snippets=[RecommendedSnippet(file="a.ts", line_start=1, line_end=10, reason="syntax_error")],
    )

    context = packet.to_model_context()

    assert "tsc failed" in context
    assert "TS1005" in context
    assert "a.ts:5" in context
    assert "a.ts lines 1-10" in context


def test_diagnostic_record_identity_groups_by_code_file_line_message():
    a = DiagnosticRecord(code="TS1005", file="a.ts", line=5, message="oops")
    b = DiagnosticRecord(code="TS1005", file="a.ts", line=5, message="oops")
    c = DiagnosticRecord(code="TS1005", file="a.ts", line=6, message="oops")

    assert a.identity() == b.identity()
    assert a.identity() != c.identity()
