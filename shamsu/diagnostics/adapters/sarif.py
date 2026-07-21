"""SARIF-in / SARIF-like-out normalization.

`try_parse` reads genuine SARIF output from a tool that already emits it
(e.g. an ESLint SARIF formatter). `to_sarif_like` renders SHAMSU's internal
`DiagnosticRecord`/`ErrorPacket` model back out in a SARIF-compatible shape
for inspection - a full SARIF implementation is not required, but the
internal model stays translatable.
"""
from __future__ import annotations

import json
from typing import Any

from shamsu.diagnostics.types import DiagnosticRecord

SEVERITY_TO_SARIF_LEVEL = {"error": "error", "warning": "warning", "info": "note"}


def looks_like_sarif(text: str) -> bool:
    stripped = text.strip()
    if not stripped.startswith("{"):
        return False
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, dict) and (
        "sarif" in str(payload.get("$schema", "")).lower() or "runs" in payload
    )


def try_parse(text: str) -> list[DiagnosticRecord] | None:
    if not looks_like_sarif(text):
        return None
    payload = json.loads(text.strip())
    records: list[DiagnosticRecord] = []
    for run in payload.get("runs", []):
        tool_name = ((run.get("tool") or {}).get("driver") or {}).get("name", "")
        for result in run.get("results", []):
            records.append(_result_to_record(tool_name, result))
    return records or None


def _result_to_record(tool_name: str, result: dict) -> DiagnosticRecord:
    locations = result.get("locations") or []
    physical = (locations[0].get("physicalLocation") if locations else {}) or {}
    artifact = physical.get("artifactLocation") or {}
    region = physical.get("region") or {}
    message = (result.get("message") or {}).get("text", "")
    return DiagnosticRecord(
        tool=tool_name,
        severity=result.get("level", "error"),
        code=str(result.get("ruleId") or ""),
        category="lint",
        message=message,
        file=artifact.get("uri", ""),
        line=region.get("startLine"),
        column=region.get("startColumn"),
        raw_excerpt=json.dumps(result)[:500],
        parser_name="sarif",
        confidence=1.0,
    )


def to_sarif_like(tool: str, records: list[DiagnosticRecord]) -> dict[str, Any]:
    return {
        "$schema": "sarif-like-internal",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {"driver": {"name": tool or "shamsu-diagnostics"}},
                "results": [
                    {
                        "ruleId": record.code or record.category,
                        "level": SEVERITY_TO_SARIF_LEVEL.get(record.severity, "error"),
                        "message": {"text": record.message},
                        "locations": [
                            {
                                "physicalLocation": {
                                    "artifactLocation": {"uri": record.file},
                                    "region": {"startLine": record.line, "startColumn": record.column},
                                }
                            }
                        ]
                        if record.file
                        else [],
                    }
                    for record in records
                ],
            }
        ],
    }
