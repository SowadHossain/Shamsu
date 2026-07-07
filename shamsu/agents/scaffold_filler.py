"""Fill a scaffold's marked holes from the PRD contract.

Templates ship a working placeholder; this turns the placeholder into the PRD's
real project by replacing ONLY the body of each `// HOLE:<id>` ... `// END:<id>`
region with model-generated code. It never rewrites whole files and never edits
outside a hole, so exports the rest of the app imports are preserved. Every
write is transaction-backed (rollback-safe), and any hole whose marker/region is
missing or whose model output is empty is skipped rather than guessed.

The model call is injected as a synchronous callable (system, user, schema) ->
raw JSON string, so this module has no Ollama/asyncio dependency and is unit
testable; the pipeline bridges async->sync at the wiring boundary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from json_repair import repair_json

from shamsu.patch.transactions import TransactionWorkspace
from shamsu.prd.contract import PRDContract
from shamsu.registry.schema import Hole, RegistryEntry
from shamsu.session.manager import SessionLogger

HOLE_FILL_SCHEMA: dict = {
    "type": "object",
    "properties": {"code": {"type": "string"}},
    "required": ["code"],
}

HOLE_FILL_SYSTEM = """You are SHAMSU filling ONE marked hole in a working code scaffold.
Hard rules:
- Output ONLY JSON: {"code": "<the replacement body for this hole>"}.
- Implement exactly what the PRD describes for THIS hole, nothing else.
- Your code replaces the lines BETWEEN the // HOLE and // END markers. Do not
  emit the marker lines themselves.
- Keep every existing export. Do not rename functions the rest of the app imports.
- GameState is a numeric bag (add fields freely; use 0/1 for booleans). Keep it
  compiling. No em dashes in comments.
"""


class GenerateJSON(Protocol):
    def __call__(self, system: str, user: str, schema: dict) -> str: ...


@dataclass
class FillResult:
    filled: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)


class ScaffoldFiller:
    def __init__(
        self,
        workspace_root: Path,
        generate: GenerateJSON,
        *,
        session_logger: SessionLogger | None = None,
        schema: dict | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._generate = generate
        self.session_logger = session_logger
        self._schema = schema or HOLE_FILL_SCHEMA
        self.transactions = TransactionWorkspace(self.workspace_root)

    def fill(
        self,
        entry: RegistryEntry,
        target_dir: Path | str,
        contract: PRDContract,
    ) -> FillResult:
        target = Path(target_dir).resolve()
        result = FillResult()
        for hole in _ordered_holes(entry.manifest.holes):
            file_path = (target / hole.target_file).resolve()
            if not file_path.is_file():
                result.skipped.append(hole.id)
                continue
            text = file_path.read_text(encoding="utf-8", errors="replace")
            region = find_hole_region(text, hole.marker)
            if region is None:
                result.skipped.append(hole.id)
                continue

            body = text[region[0]:region[1]]
            code = self._fill_one(entry, contract, hole, body, text)
            if not code:
                result.skipped.append(hole.id)
                continue

            new_text = text[:region[0]] + _as_block(code) + text[region[1]:]
            if new_text == text:
                result.skipped.append(hole.id)
                continue

            rel = self._rel(file_path)
            transaction_id = self.transactions.begin(
                reason=f"ScaffoldFiller: fill hole {hole.id} ({hole.target_file})",
                operations=[{"op": "edit_file", "path": rel, "dest_path": "", "reason": hole.id}],
                destructive=False,
            )
            self.transactions.backup_file(transaction_id, rel)
            file_path.write_text(new_text, encoding="utf-8")
            self.transactions.record_after(transaction_id, rel)
            self.transactions.finalize(transaction_id, "applied")

            result.filled.append(hole.id)
            if rel not in result.changed_files:
                result.changed_files.append(rel)
            self._log("scaffold.hole_filled", {"hole": hole.id, "file": rel})
        return result

    # -- helpers ---------------------------------------------------------------

    def _fill_one(
        self,
        entry: RegistryEntry,
        contract: PRDContract,
        hole: Hole,
        body: str,
        file_text: str,
    ) -> str:
        prompt = build_hole_prompt(entry, contract, hole, body, file_text)
        try:
            raw = self._generate(HOLE_FILL_SYSTEM, prompt, self._schema)
        except Exception:
            return ""
        return _parse_code(raw or "")

    def _rel(self, file_path: Path) -> str:
        return file_path.resolve().relative_to(self.workspace_root).as_posix()

    def _log(self, event_type: str, payload: dict) -> None:
        if self.session_logger:
            self.session_logger.log(
                event_type, payload, f"ScaffoldFiller: {event_type}", workflow_id="scaffold-fill"
            )


def build_hole_prompt(
    entry: RegistryEntry,
    contract: PRDContract,
    hole: Hole,
    body: str,
    file_text: str,
) -> str:
    parts = [contract.render_brief(), ""]
    parts.append(f"## Hole to fill: {hole.id} (in {hole.target_file})")
    parts.append(f"- what: {hole.description}")
    if hole.signature:
        parts.append(f"- signature/intent: {hole.signature}")
    parts.append("")
    parts.append("## Current placeholder body (replace this with PRD logic)")
    parts.append(body.strip("\n") or "(empty)")
    parts.append("")
    parts.append("## Full file for context (do NOT rewrite it; only return the hole body)")
    parts.append(file_text)
    parts.append("")
    parts.append('## Task\nReturn JSON {"code": "..."} with ONLY the replacement body for this hole.')
    return "\n".join(parts)


# --- marker region handling ---------------------------------------------------

def find_hole_region(text: str, marker: str) -> tuple[int, int] | None:
    """Return (start, end) char offsets of the body BETWEEN `// HOLE:<id>` and
    the matching `// END:<id>` line (markers excluded). None if not found."""
    end_marker = _end_marker(marker)
    if end_marker is None:
        return None
    lines = text.splitlines(keepends=True)
    hole_line_idx = _find_marker_line(lines, marker)
    if hole_line_idx is None:
        return None
    end_line_idx = _find_marker_line(lines, end_marker, start=hole_line_idx + 1)
    if end_line_idx is None:
        return None
    start = sum(len(line) for line in lines[: hole_line_idx + 1])
    end = sum(len(line) for line in lines[:end_line_idx])
    return (start, end)


def _end_marker(marker: str) -> str | None:
    stripped = marker.strip()
    if "HOLE:" not in stripped:
        return None
    return stripped.replace("HOLE:", "END:")


def _find_marker_line(lines: list[str], marker: str, start: int = 0) -> int | None:
    needle = marker.strip()
    for idx in range(start, len(lines)):
        if needle in lines[idx]:
            return idx
    return None


def _as_block(code: str) -> str:
    return code.strip("\n") + "\n"


def _ordered_holes(holes: list[Hole]) -> list[Hole]:
    """Topological-ish order: a hole is emitted once its declared deps are.
    Falls back to manifest order for cycles/unknown deps."""
    remaining = list(holes)
    emitted: set[str] = set()
    ordered: list[Hole] = []
    while remaining:
        progressed = False
        for hole in list(remaining):
            if all(dep in emitted for dep in hole.depends_on):
                ordered.append(hole)
                emitted.add(hole.id)
                remaining.remove(hole)
                progressed = True
        if not progressed:  # unresolved deps -> keep manifest order
            ordered.extend(remaining)
            break
    return ordered


def _parse_code(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    data = _loads(text)
    if isinstance(data, dict):
        code = data.get("code")
        if isinstance(code, str):
            return code.strip("\n")
    return ""


def _loads(text: str) -> object | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(repair_json(text))
    except Exception:
        return None
