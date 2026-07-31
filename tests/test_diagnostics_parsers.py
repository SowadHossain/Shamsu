from __future__ import annotations

from shamsu.diagnostics.parsers import (
    generic_fallback,
    node_runtime_fallback,
    pytest_fallback,
    python_fallback,
    typescript_fallback,
)

# -- TypeScript / tsc -------------------------------------------------------

def test_parses_tsc_file_line_column_errors():
    text = "src/game/rules.ts(71,17): error TS1005: ')' expected."

    records = typescript_fallback.parse_tsc_errors(text)

    assert len(records) == 1
    record = records[0]
    assert record.file == "src/game/rules.ts"
    assert record.line == 71
    assert record.column == 17
    assert record.code == "TS1005"
    assert record.message == "')' expected."


def test_extracts_ts_error_code():
    records = typescript_fallback.parse_tsc_errors(
        "src/x.ts(1,1): error TS2322: Type 'string' is not assignable to type 'number'."
    )

    assert records[0].code == "TS2322"


def test_extracts_syntax_error_category_for_expected_token_messages():
    records = typescript_fallback.parse_tsc_errors("src/x.ts(1,1): error TS1005: ';' expected.")

    assert records[0].category == "syntax_error"


def test_non_syntax_tsc_error_is_categorized_as_type_error():
    records = typescript_fallback.parse_tsc_errors(
        "src/x.ts(1,1): error TS2322: Type 'string' is not assignable to type 'number'."
    )

    assert records[0].category == "type_error"


def test_extracts_missing_export_symbol_and_module():
    text = "Module '\"./rules\"' has no exported member 'World'."

    records = typescript_fallback.parse_missing_export(text)

    assert len(records) == 1
    assert records[0].symbol == "World"
    assert records[0].module == "./rules"
    assert records[0].category == "missing_export"


def test_extracts_did_you_mean_suggestion():
    text = "Module '\"./loop\"' has no exported member named 'GameLoop'. Did you mean 'gameLoop'?"

    records = typescript_fallback.parse_missing_export(text)

    assert records[0].related_locations == ["gameLoop"]


def test_parses_browser_missing_export_runtime_error():
    text = "Uncaught SyntaxError: The requested module '/src/game/loop.ts' does not provide an export named 'GameLoop'"

    records = typescript_fallback.parse_browser_missing_export(text)

    assert len(records) == 1
    assert records[0].category == "runtime_missing_export"
    assert records[0].module == "/src/game/loop.ts"
    assert records[0].symbol == "GameLoop"


# -- Python tracebacks -------------------------------------------------------

def test_parses_final_exception_type_and_message():
    text = (
        "Traceback (most recent call last):\n"
        '  File "app/views.py", line 12, in task_list\n'
        "    raise ValueError(\"bad input\")\n"
        "ValueError: bad input\n"
    )

    records = python_fallback.parse_python_traceback(text)

    assert len(records) == 1
    assert records[0].code == "ValueError"
    assert records[0].message == "bad input"


def test_extracts_user_code_traceback_frame_over_vendor_frame():
    text = (
        "Traceback (most recent call last):\n"
        '  File "/project/.venv/Lib/site-packages/somelib/core.py", line 99, in run\n'
        '  File "app/views.py", line 12, in task_list\n'
        "ValueError: bad input\n"
    )

    records = python_fallback.parse_python_traceback(text)

    assert records[0].file == "app/views.py"
    assert records[0].line == 12


def test_python_traceback_ignores_error_like_lines_outside_traceback_context():
    text = "some log line mentioning Error: not a traceback\n"

    assert python_fallback.parse_python_traceback(text) == []


def test_parses_py_compile_syntax_error_block():
    text = (
        '  File "ledgerlite.py", line 94\n'
        "    f.write('id,category,amount,note\n"
        "            ^\n"
        "SyntaxError: unterminated string literal (detected at line 94)\n"
    )

    records = python_fallback.parse_python_traceback(text)

    assert len(records) == 1
    assert records[0].category == "syntax_error"
    assert records[0].code == "SyntaxError"
    assert records[0].file == "ledgerlite.py"
    assert records[0].line == 94
    assert "unterminated string literal" in records[0].message


# -- pytest ------------------------------------------------------------------

def test_parses_pytest_failed_summary_line():
    text = (
        "FAILED tests/test_foo.py::test_bar - AssertionError: expected 1, got 2\n"
    )

    records = pytest_fallback.parse_pytest_failures(text)

    assert len(records) == 1
    assert records[0].file == "tests/test_foo.py"
    assert records[0].symbol == "test_bar"
    assert "AssertionError" in records[0].message


# -- Generic fallback ---------------------------------------------------------

def test_generic_fallback_parses_file_line_column_format():
    records = generic_fallback.parse_generic_locations("path/to/file.py:10:4: something went wrong")

    assert len(records) == 1
    assert records[0].file == "path/to/file.py"
    assert records[0].line == 10
    assert records[0].column == 4


def test_generic_fallback_parses_file_line_format_without_column():
    records = generic_fallback.parse_generic_locations("path/to/file.py:10: something went wrong")

    assert len(records) == 1
    assert records[0].line == 10
    assert records[0].column is None


def test_generic_fallback_ignores_npm_boilerplate():
    text = (
        "npm notice New minor version of npm available\n"
        "> myapp@1.0.0 build\n"
        "> tsc\n"
        "path/to/file.py:10: real error here\n"
        "npm ERR! code ELIFECYCLE\n"
    )

    records = generic_fallback.parse_generic_locations(text)

    assert len(records) == 1
    assert "real error" in records[0].message


def test_strip_npm_boilerplate_removes_lifecycle_noise():
    lines = [
        "npm notice New minor version of npm available",
        "> myapp@1.0.0 build",
        "> tsc",
        "real output line",
        "npm ERR! code ELIFECYCLE",
    ]

    cleaned = generic_fallback.strip_npm_boilerplate(lines)

    assert cleaned == ["real output line"]


# -- Node/browser runtime -----------------------------------------------------

def test_node_runtime_parses_module_not_found():
    records = node_runtime_fallback.parse_node_runtime_errors("Cannot find module './missing'")

    assert records[0].category == "module_not_found"
    assert records[0].module == "./missing"
