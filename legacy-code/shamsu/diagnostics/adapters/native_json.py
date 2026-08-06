"""Parse native structured output tools already emit on request.

This is the first thing DiagnosticDigest tries - if the tool already told
us exactly what's wrong in a machine-readable shape, there is nothing to
guess. Supports:

- eslint / ruff `--format json` (JSON array of file->messages objects)
- `go test -json` (JSON-Lines of {Action, Package, Test, Output})
- `cargo ... --message-format=json` (JSON-Lines of {reason, message})
- JUnit-style XML (pytest/django `--junitxml`, cargo2junit, etc.)

Returns None (not an empty list) when the output doesn't look like any of
these shapes, so the caller knows to fall through to the next parser.
"""
from __future__ import annotations

import json
import xml.etree.ElementTree as ET

from shamsu.diagnostics.types import DiagnosticRecord


def try_parse(tool: str, stdout: str) -> list[DiagnosticRecord] | None:
    text = stdout.strip()
    if not text:
        return None

    records = _try_json_array(text)
    if records is not None:
        return records

    records = _try_jsonl(text)
    if records is not None:
        return records

    records = _try_junit_xml(text)
    if records is not None:
        return records

    return None


def _try_json_array(text: str) -> list[DiagnosticRecord] | None:
    if not text.lstrip().startswith("["):
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or not payload:
        return None

    records: list[DiagnosticRecord] = []
    for entry in payload:
        if not isinstance(entry, dict):
            return None
        if "messages" in entry and "filePath" in entry:
            records.extend(_eslint_file_entry(entry))
        elif "location" in entry and ("filename" in entry or "file" in entry):
            records.append(_ruff_entry(entry))
        else:
            return None
    return records or None


def _eslint_file_entry(entry: dict) -> list[DiagnosticRecord]:
    file_path = entry.get("filePath", "")
    records = []
    for message in entry.get("messages", []):
        severity_num = message.get("severity", 2)
        records.append(
            DiagnosticRecord(
                tool="eslint",
                language="javascript",
                severity="error" if severity_num == 2 else "warning",
                code=str(message.get("ruleId") or ""),
                category="lint",
                message=str(message.get("message", "")),
                file=file_path,
                line=message.get("line"),
                column=message.get("column"),
                raw_excerpt=json.dumps(message),
                parser_name="native_json",
                confidence=1.0,
            )
        )
    return records


def _ruff_entry(entry: dict) -> DiagnosticRecord:
    location = entry.get("location") or {}
    return DiagnosticRecord(
        tool="ruff",
        language="python",
        severity="error",
        code=str(entry.get("code") or ""),
        category="lint",
        message=str(entry.get("message", "")),
        file=entry.get("filename") or entry.get("file") or "",
        line=location.get("row"),
        column=location.get("column"),
        raw_excerpt=json.dumps(entry),
        parser_name="native_json",
        confidence=1.0,
    )


def _try_jsonl(text: str) -> list[DiagnosticRecord] | None:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) < 1:
        return None
    parsed_lines: list[dict] = []
    for line in lines:
        stripped = line.strip()
        if not stripped.startswith("{"):
            return None
        try:
            parsed_lines.append(json.loads(stripped))
        except json.JSONDecodeError:
            return None

    if all("Action" in entry for entry in parsed_lines):
        return _go_test_json(parsed_lines)
    if all("reason" in entry for entry in parsed_lines):
        return _cargo_json(parsed_lines)
    return None


def _go_test_json(entries: list[dict]) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    failed_tests: dict[str, list[str]] = {}
    for entry in entries:
        if entry.get("Action") == "output" and entry.get("Test"):
            failed_tests.setdefault(entry["Test"], []).append(str(entry.get("Output", "")))
    for entry in entries:
        if entry.get("Action") != "fail" or not entry.get("Test"):
            continue
        test_name = entry["Test"]
        output = "".join(failed_tests.get(test_name, []))
        records.append(
            DiagnosticRecord(
                tool="go test",
                language="go",
                severity="error",
                category="test_failure",
                message=output.strip() or f"{test_name} failed",
                symbol=test_name,
                module=str(entry.get("Package", "")),
                raw_excerpt=output.strip(),
                parser_name="native_json",
                confidence=1.0,
            )
        )
    return records


def _cargo_json(entries: list[dict]) -> list[DiagnosticRecord]:
    records: list[DiagnosticRecord] = []
    for entry in entries:
        if entry.get("reason") != "compiler-message":
            continue
        message = entry.get("message") or {}
        if message.get("level") not in {"error", "warning"}:
            continue
        spans = message.get("spans") or []
        primary = next((span for span in spans if span.get("is_primary")), spans[0] if spans else {})
        code = (message.get("code") or {}).get("code") or ""
        records.append(
            DiagnosticRecord(
                tool="cargo",
                language="rust",
                severity=message.get("level", "error"),
                code=str(code),
                category="compiler_error",
                message=str(message.get("message", "")),
                file=primary.get("file_name", ""),
                line=primary.get("line_start"),
                column=primary.get("column_start"),
                raw_excerpt=json.dumps(message)[:500],
                parser_name="native_json",
                confidence=1.0,
            )
        )
    return records or None


def _try_junit_xml(text: str) -> list[DiagnosticRecord] | None:
    stripped = text.lstrip()
    if not stripped.startswith("<?xml") and not stripped.startswith("<testsuite"):
        return None
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError:
        return None

    testcases = root.iter("testcase")
    records: list[DiagnosticRecord] = []
    for testcase in testcases:
        failure = testcase.find("failure")
        error = testcase.find("error")
        node = failure if failure is not None else error
        if node is None:
            continue
        body = (node.text or "").strip()
        message = node.get("message") or (body.splitlines()[0] if body else "")
        records.append(
            DiagnosticRecord(
                tool="junit",
                language="",
                severity="error",
                category="test_failure",
                message=message,
                file=testcase.get("classname", ""),
                symbol=testcase.get("name", ""),
                raw_excerpt=body[:1000],
                parser_name="native_json",
                confidence=0.95,
            )
        )
    if not records:
        return None
    return records
