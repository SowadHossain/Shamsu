"""Deterministic root-cause selection over normalized diagnostics.

Implements section 8 of the diagnostics prompt: prefer real compiler errors
over boilerplate, prefer user workspace files over vendor files, prefer
syntax errors before downstream type errors, prefer missing export/import
errors before the cascade of symbol errors they cause, group/dedupe
identical diagnostics, and prioritize the file most errors come from.
"""
from __future__ import annotations

from collections import Counter

from shamsu.diagnostics.types import DiagnosticRecord

VENDOR_MARKERS = (
    "node_modules/", "node_modules\\",
    "site-packages/", "site-packages\\",
    "/.venv/", "\\.venv\\",
    "/dist/", "\\dist\\",
    "vendor/", "vendor\\",
)

# Lower number = higher priority root cause.
CATEGORY_PRIORITY = {
    "missing_export": 0,
    "import_export_mismatch": 0,
    "runtime_missing_export": 0,
    "module_not_found": 0,
    "syntax_error": 1,
    "type_error": 2,
    "compiler_error": 2,
    "exception": 2,
    "test_failure": 2,
    "runtime_exception": 3,
    "lint": 3,
    "generic": 4,
}

# Categories that, when present, are the root cause of everything else -
# an import/export mismatch typically cascades into many downstream
# type/symbol errors that are just noise once the real cause is known.
ROOT_CAUSE_CATEGORIES = {"missing_export", "import_export_mismatch", "runtime_missing_export", "module_not_found"}


def is_vendor_path(path: str) -> bool:
    if not path:
        return False
    normalized = path.replace("\\", "/")
    return any(marker.replace("\\", "/") in normalized for marker in VENDOR_MARKERS)


def dedupe_and_count(records: list[DiagnosticRecord]) -> list[DiagnosticRecord]:
    """Group identical diagnostics (same code/file/line/message) and count
    repeats, preserving first-seen order."""
    grouped: dict[tuple, DiagnosticRecord] = {}
    order: list[tuple] = []
    for record in records:
        key = record.identity()
        if key in grouped:
            grouped[key].count += record.count
        else:
            grouped[key] = record
            order.append(key)
    return [grouped[key] for key in order]


def select_root_diagnostics(
    records: list[DiagnosticRecord],
) -> tuple[list[DiagnosticRecord], list[DiagnosticRecord]]:
    deduped = dedupe_and_count(records)
    if not deduped:
        return [], []

    user_records = [r for r in deduped if not is_vendor_path(r.file)]
    pool = user_records or deduped

    file_counts = Counter(r.file for r in pool if r.file)
    dominant_file = file_counts.most_common(1)[0][0] if file_counts else None

    def sort_key(record: DiagnosticRecord) -> tuple:
        same_file = 0 if (dominant_file and record.file == dominant_file) else 1
        return (CATEGORY_PRIORITY.get(record.category, 5), same_file, -record.count)

    ordered = sorted(pool, key=sort_key)

    root_cause_matches = [r for r in ordered if r.category in ROOT_CAUSE_CATEGORIES]
    if root_cause_matches:
        root = root_cause_matches
        secondary = [r for r in ordered if r not in root]
        return root, secondary

    top_priority = CATEGORY_PRIORITY.get(ordered[0].category, 5)
    root = [r for r in ordered if CATEGORY_PRIORITY.get(r.category, 5) == top_priority]
    secondary = [r for r in ordered if r not in root]
    return root, secondary
