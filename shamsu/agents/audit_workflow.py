"""
Code audit workflow.

The audit path is read-only: it searches indexed snippets, builds a compact
reviewer context pack, asks the reviewer specialist for structured findings,
and returns those findings without applying patches.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from json_repair import repair_json

from shamsu.context.builder import ContextBuilder
from shamsu.interfaces import IContextBuilder, ILLMManager, ISearchAgent
from shamsu.llm.manager import LLMManager
from shamsu.types import ContextPack, SearchResult

AUDIT_SYSTEM_INSTRUCTIONS = """Audit the indexed code snippets for bugs, security issues,
missing tests, fragile error handling, and maintainability risks.
Return ONLY JSON with this shape:
{"findings":[{"severity":"low|medium|high","file_path":"path","line_start":1,
"category":"security|bug|test|maintainability","reason":"why it matters",
"recommendation":"what to change"}]}"""


@dataclass(frozen=True)
class AuditFinding:
    severity: Literal["low", "medium", "high"]
    file_path: str
    line_start: int | None
    category: str
    reason: str
    recommendation: str = ""


@dataclass(frozen=True)
class AuditReport:
    request: str
    pack: ContextPack
    findings: list[AuditFinding]
    raw_response: str


class AuditWorkflow:
    def __init__(
        self,
        search: ISearchAgent,
        llm: ILLMManager | None = None,
        context_builder: IContextBuilder | None = None,
    ) -> None:
        self.search = search
        self.llm = llm or LLMManager()
        self.context_builder = context_builder or ContextBuilder()

    async def run(self, request: str = "Audit this project") -> AuditReport:
        results = self._search_for_audit_context(request)
        pack = self.context_builder.pack(
            results=results,
            request=f"{AUDIT_SYSTEM_INSTRUCTIONS}\n\nUser audit request: {request}",
            task_id="audit",
            step_id=1,
            specialist="reviewer",
        )
        response = await self.llm.run_specialist("reviewer", pack)
        findings = parse_audit_findings(response.raw)
        return AuditReport(
            request=request,
            pack=pack,
            findings=findings,
            raw_response=response.raw,
        )

    def _search_for_audit_context(self, request: str) -> list[SearchResult]:
        seen: set[tuple[str, int, int]] = set()
        results: list[SearchResult] = []
        for query in _audit_queries(request):
            for result in self.search.search(query, top_k=5):
                key = (result.file_path, result.line_start, result.line_end)
                if key in seen:
                    continue
                seen.add(key)
                results.append(result)
        return results[:12]


def parse_audit_findings(raw: str) -> list[AuditFinding]:
    data = _parse_jsonish(raw)
    if isinstance(data, dict):
        items = data.get("findings", [])
    elif isinstance(data, list):
        items = data
    else:
        return []
    findings: list[AuditFinding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        severity = _normalize_severity(str(item.get("severity", "medium")))
        reason = str(item.get("reason", "")).strip()
        file_path = str(item.get("file_path", "")).strip()
        if not reason or not file_path:
            continue
        findings.append(
            AuditFinding(
                severity=severity,
                file_path=file_path,
                line_start=_optional_int(item.get("line_start")),
                category=str(item.get("category", "general")).strip() or "general",
                reason=reason,
                recommendation=str(item.get("recommendation", "")).strip(),
            )
        )
    return findings


def _audit_queries(request: str) -> list[str]:
    return [
        request,
        "security password secret token permission authentication authorization",
        "TODO FIXME error exception validation input",
        "test tests coverage assert",
    ]


def _parse_jsonish(raw: str) -> object:
    text = _strip_code_fence(raw.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        try:
            return repair_json(text, return_objects=True)
        except Exception:
            return None


def _strip_code_fence(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def _normalize_severity(value: str) -> Literal["low", "medium", "high"]:
    lowered = value.lower().strip()
    if lowered in {"low", "medium", "high"}:
        return lowered  # type: ignore[return-value]
    return "medium"


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
