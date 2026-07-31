from __future__ import annotations

import json

from shamsu.diagnostics.adapters import (
    drain3_compactor,
    llmlingua_optional,
    native_json,
    reviewdog_errorformat,
    sarif,
)

# -- native structured output -------------------------------------------------

def test_native_json_parses_eslint_style_array():
    payload = json.dumps(
        [
            {
                "filePath": "src/app.ts",
                "messages": [
                    {"ruleId": "no-unused-vars", "severity": 2, "message": "'x' is unused", "line": 3, "column": 5}
                ],
            }
        ]
    )

    records = native_json.try_parse("eslint", payload)

    assert records is not None
    assert records[0].code == "no-unused-vars"
    assert records[0].file == "src/app.ts"
    assert records[0].line == 3


def test_native_json_parses_go_test_json_lines():
    lines = [
        json.dumps({"Action": "run", "Test": "TestFoo"}),
        json.dumps({"Action": "output", "Test": "TestFoo", "Output": "foo_test.go:10: assertion failed\n"}),
        json.dumps({"Action": "fail", "Test": "TestFoo", "Package": "pkg"}),
    ]

    records = native_json.try_parse("go test", "\n".join(lines))

    assert records is not None
    assert records[0].symbol == "TestFoo"
    assert "assertion failed" in records[0].message


def test_native_json_returns_none_for_plain_text():
    assert native_json.try_parse("tsc", "src/x.ts(1,1): error TS1005: ')' expected.") is None


def test_native_json_preferred_over_fallback_parser_in_digest():
    """External tool policy #1: native structured output wins over any
    fallback parser when the tool already emitted it."""
    from pathlib import Path

    from shamsu.diagnostics.digest import DiagnosticDigest

    payload = json.dumps(
        [{"filePath": "src/app.ts", "messages": [{"ruleId": "no-unused-vars", "severity": 2, "message": "unused", "line": 1, "column": 1}]}]
    )
    digest = DiagnosticDigest(Path("."))

    packet = digest.run("eslint --format json", ".", 1, payload, "")

    assert packet.parser_chain == ["native_json"]
    assert packet.root_diagnostics[0].parser_name == "native_json"


# -- SARIF --------------------------------------------------------------------

def test_sarif_try_parse_reads_genuine_sarif_output():
    payload = json.dumps(
        {
            "$schema": "https://schemastore.azurewebsites.net/schemas/json/sarif-2.1.0.json",
            "runs": [
                {
                    "tool": {"driver": {"name": "eslint"}},
                    "results": [
                        {
                            "ruleId": "no-unused-vars",
                            "level": "error",
                            "message": {"text": "'x' is unused"},
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": "src/app.ts"},
                                        "region": {"startLine": 3, "startColumn": 5},
                                    }
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )

    records = sarif.try_parse(payload)

    assert records is not None
    assert records[0].file == "src/app.ts"
    assert records[0].line == 3


def test_sarif_try_parse_returns_none_for_non_sarif_json():
    assert sarif.try_parse(json.dumps({"foo": "bar"})) is None


def test_to_sarif_like_round_trips_internal_records():
    from shamsu.diagnostics.types import DiagnosticRecord

    record = DiagnosticRecord(code="TS1005", severity="error", message="oops", file="a.ts", line=5, column=1)

    rendered = sarif.to_sarif_like("tsc", [record])

    assert rendered["runs"][0]["results"][0]["ruleId"] == "TS1005"
    assert rendered["runs"][0]["results"][0]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "a.ts"


# -- errorformat-style adapter -------------------------------------------------

def test_reviewdog_errorformat_parses_file_line_column_message():
    records = reviewdog_errorformat.parse("path/to/file.py:10:4: something broke")

    assert len(records) == 1
    assert records[0].file == "path/to/file.py"
    assert records[0].line == 10
    assert records[0].column == 4
    assert records[0].parser_name == "reviewdog_errorformat"


def test_reviewdog_errorformat_used_when_no_tool_specific_parser_matches():
    """External tool policy #2/#3: the errorformat-style adapter is used to
    fill gaps a tool-specific fallback parser doesn't cover, and only when a
    native/SARIF parse did not already succeed."""
    from pathlib import Path

    from shamsu.diagnostics.digest import DiagnosticDigest

    digest = DiagnosticDigest(Path("."))
    packet = digest.run("make build", ".", 1, "", "path/to/file.py:10:4: something broke")

    assert "reviewdog_errorformat" in packet.parser_chain
    assert packet.root_diagnostics[0].file == "path/to/file.py"


def test_reviewdog_external_binary_reports_none_when_not_configured(monkeypatch):
    monkeypatch.delenv("SHAMSU_REVIEWDOG_BIN", raising=False)
    monkeypatch.setattr("shutil.which", lambda name: None)

    assert reviewdog_errorformat.external_binary() is None


# -- Drain3 compaction ----------------------------------------------------------

def test_drain3_noisy_log_detection_requires_dev_server_tool_and_no_diagnostics():
    # "npm dev" is the canonical tool name shamsu.diagnostics.normalize
    # .detect_tool() returns for an `npm run dev` command.
    assert drain3_compactor.is_noisy_runtime_log("npm dev", 0, 100) is True
    assert drain3_compactor.is_noisy_runtime_log("tsc", 0, 100) is False
    assert drain3_compactor.is_noisy_runtime_log("npm dev", 3, 100) is False
    assert drain3_compactor.is_noisy_runtime_log("npm dev", 0, 10) is False


def test_drain3_fallback_compaction_groups_repeated_templated_lines():
    lines = [f"[info] request {i} handled in 12ms" for i in range(5)]

    templates, removed = drain3_compactor._compact_fallback(lines)

    assert len(templates) == 1
    assert templates[0][1] == 5
    assert removed == 4


def test_drain3_compaction_never_used_for_exact_compiler_diagnostics():
    """Drain3-style compaction must only apply to noisy runtime logs, not
    exact compiler diagnostics where line numbers/codes matter."""
    from shamsu.diagnostics.compact import build_compact_log

    text = "src/x.ts(1,1): error TS1005: ')' expected."

    compact_log, _removed = build_compact_log(text, "tsc", structured_diagnostic_count=1)

    assert "TS1005" in compact_log
    assert "')' expected." in compact_log


# -- LLMLingua (disabled by default) --------------------------------------------

def test_llmlingua_disabled_by_default():
    assert llmlingua_optional.is_enabled(None) is False
    assert llmlingua_optional.is_enabled({}) is False


def test_llmlingua_maybe_compress_prose_is_noop_when_disabled():
    text = "some long prose that would otherwise be compressed"

    compressed, used = llmlingua_optional.maybe_compress_prose(text, {"enable_llmlingua": False})

    assert compressed == text
    assert used is False


def test_llmlingua_env_var_enables_but_still_requires_package_installed(monkeypatch):
    monkeypatch.setenv("SHAMSU_DIAGNOSTICS_LLMLINGUA", "1")
    assert llmlingua_optional.is_enabled(None) is True
    # Package is not installed in this environment - must not fake compression.
    text = "prose"
    compressed, used = llmlingua_optional.maybe_compress_prose(text, None)
    assert compressed == text
    assert used is False
