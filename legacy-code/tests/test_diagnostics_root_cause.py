from __future__ import annotations

from shamsu.diagnostics.root_cause import dedupe_and_count, is_vendor_path, select_root_diagnostics
from shamsu.diagnostics.types import DiagnosticRecord


def test_syntax_error_prioritized_before_cascading_type_errors():
    records = [
        DiagnosticRecord(category="type_error", code="TS2322", file="a.ts", line=10, message="type mismatch"),
        DiagnosticRecord(category="syntax_error", code="TS1005", file="a.ts", line=5, message="')' expected"),
    ]

    root, secondary = select_root_diagnostics(records)

    assert len(root) == 1
    assert root[0].category == "syntax_error"
    assert secondary[0].category == "type_error"


def test_missing_export_grouped_as_root_cause_over_downstream_errors():
    records = [
        DiagnosticRecord(category="type_error", code="TS2304", file="session.ts", line=10, message="Cannot find name 'GameLoop'"),
        DiagnosticRecord(category="type_error", code="TS2304", file="session.ts", line=20, message="Cannot find name 'GameLoop'"),
        DiagnosticRecord(category="missing_export", file="session.ts", module="./loop", symbol="GameLoop", message="missing export"),
    ]

    root, secondary = select_root_diagnostics(records)

    assert len(root) == 1
    assert root[0].category == "missing_export"
    assert len(secondary) == 2


def test_repeated_errors_are_deduped_with_count():
    records = [
        DiagnosticRecord(category="type_error", code="TS2322", file="a.ts", line=10, message="type mismatch"),
        DiagnosticRecord(category="type_error", code="TS2322", file="a.ts", line=10, message="type mismatch"),
        DiagnosticRecord(category="type_error", code="TS2322", file="a.ts", line=10, message="type mismatch"),
    ]

    deduped = dedupe_and_count(records)

    assert len(deduped) == 1
    assert deduped[0].count == 3


def test_node_modules_and_vendor_frames_are_deprioritized():
    assert is_vendor_path("node_modules/some-lib/index.js") is True
    assert is_vendor_path(".venv/Lib/site-packages/pkg/mod.py") is True
    assert is_vendor_path("src/app.py") is False

    records = [
        DiagnosticRecord(category="type_error", code="E1", file="node_modules/lib/index.js", line=1, message="vendor error"),
        DiagnosticRecord(category="type_error", code="E2", file="src/app.py", line=1, message="user error"),
    ]

    root, _secondary = select_root_diagnostics(records)

    assert root[0].file == "src/app.py"


def test_root_cause_selection_preserves_exact_file_paths_symbols_and_codes():
    records = [
        DiagnosticRecord(category="syntax_error", code="TS1005", file="client/src/game/rules.ts", line=71, column=17, message="')' expected")
    ]

    root, _secondary = select_root_diagnostics(records)

    assert root[0].file == "client/src/game/rules.ts"
    assert root[0].line == 71
    assert root[0].column == 17
    assert root[0].code == "TS1005"


def test_many_errors_from_one_file_prioritize_that_file():
    records = [
        DiagnosticRecord(category="type_error", code="E1", file="b.ts", line=1, message="b error 1"),
        DiagnosticRecord(category="type_error", code="E2", file="a.ts", line=1, message="a error 1"),
        DiagnosticRecord(category="type_error", code="E3", file="a.ts", line=2, message="a error 2"),
        DiagnosticRecord(category="type_error", code="E4", file="a.ts", line=3, message="a error 3"),
    ]

    root, _secondary = select_root_diagnostics(records)

    assert root[0].file == "a.ts"


def test_select_root_diagnostics_handles_empty_input():
    assert select_root_diagnostics([]) == ([], [])
