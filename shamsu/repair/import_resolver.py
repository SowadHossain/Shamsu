"""Deterministic import-path resolver.

For IMPORT_ERROR / MODULE_NOT_FOUND on a *relative* specifier, try to resolve
the real file on disk and, if the written specifier is wrong, compute the
corrected relative specifier BEFORE any model is asked to edit. This turns
'Failed to resolve import "./ui/Hud" from "src/ui/index.ts"' into a concrete,
verifiable suggestion ("./Hud") instead of a model guess.

This is deterministic filesystem logic only - it never calls a model.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from shamsu.diagnostics.compact import resolve_module_path
from shamsu.indexer.policy import walk_workspace_files

# Import specifier extensions Vite/tsc resolve, in the order we prefer to
# emit them (extension is stripped from the final specifier regardless).
_SUFFIXES = (".ts", ".tsx", ".js", ".jsx")
_INDEX_FILES = ("index.ts", "index.tsx", "index.js", "index.jsx")
_STRIP_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

@dataclass(frozen=True)
class ImportFix:
    importer: str          # workspace-relative importer file
    original_specifier: str  # what was written, e.g. "./ui/Hud"
    suggested_specifier: str  # corrected, e.g. "./Hud"
    resolved_file: str     # workspace-relative target the fix points at

    def describe(self) -> str:
        return (
            f"In {self.importer}, the import '{self.original_specifier}' does not resolve. "
            f"It should be '{self.suggested_specifier}' "
            f"(resolves to {self.resolved_file})."
        )


def _is_relative(specifier: str) -> bool:
    return specifier.startswith("./") or specifier.startswith("../")


def _strip_ext(path: Path) -> str:
    if path.name in _INDEX_FILES:
        # ./foo/index.ts -> ./foo
        return path.parent.as_posix()
    if path.suffix in _STRIP_EXTS:
        return path.with_suffix("").as_posix()
    return path.as_posix()


def _relative_specifier(importer_dir: Path, target: Path) -> str:
    """Compute the specifier `importer` should use to reach `target`, both
    given as workspace-relative POSIX paths."""
    import os

    rel = os.path.relpath(target.as_posix(), importer_dir.as_posix())
    rel_posix = Path(rel).as_posix()
    spec = _strip_ext(Path(rel_posix))
    if not (spec.startswith("./") or spec.startswith("../")):
        spec = f"./{spec}"
    return spec


def _target_basename(specifier: str) -> str:
    """The file stem the specifier is trying to reach ('./ui/Hud' -> 'Hud')."""
    name = Path(specifier).name
    for ext in _STRIP_EXTS:
        if name.endswith(ext):
            return name[: -len(ext)]
    return name


def _find_candidates(workspace_root: Path, basename: str) -> list[Path]:
    """All workspace source files whose stem matches `basename` (or a
    directory `basename/` with an index file), workspace-relative."""
    matches: list[Path] = []
    files = walk_workspace_files(
        workspace_root,
        suffixes=_SUFFIXES,
        indexable_only=True,
    )
    for path in files:
        if path.stem == basename:
            matches.append(path)
        elif path.name in _INDEX_FILES and path.parent.name == basename:
            matches.append(path)
    return matches


def _rank_candidate(importer_dir: Path, candidate_dir: Path) -> tuple[int, int]:
    """Prefer a match in the same directory as the importer, then the
    shallowest common-path distance."""
    if candidate_dir == importer_dir:
        return (0, 0)
    imp_parts = importer_dir.parts
    cand_parts = candidate_dir.parts
    common = 0
    for a, b in zip(imp_parts, cand_parts):
        if a != b:
            break
        common += 1
    distance = (len(imp_parts) - common) + (len(cand_parts) - common)
    return (1, distance)


def suggest_import_fix(
    workspace_root: Path, importer: str, specifier: str
) -> ImportFix | None:
    """Return a corrected relative specifier for a broken *relative* import,
    or None when it already resolves, is a bare/package import, or no unique
    on-disk target can be found."""
    workspace_root = Path(workspace_root).resolve()
    if not importer or not specifier:
        return None
    if not _is_relative(specifier):
        return None  # bare/package imports are out of scope for path correction

    # Already resolvable as written? Then there is nothing to correct.
    if resolve_module_path(workspace_root, importer, specifier):
        return None

    basename = _target_basename(specifier)
    if not basename:
        return None

    candidates = _find_candidates(workspace_root, basename)
    if not candidates:
        return None

    importer_dir = Path(importer).parent
    rel_candidates: list[Path] = []
    for candidate in candidates:
        try:
            rel_candidates.append(candidate.resolve().relative_to(workspace_root))
        except ValueError:
            continue
    if not rel_candidates:
        return None

    rel_candidates.sort(key=lambda c: _rank_candidate(importer_dir, c.parent))
    best = rel_candidates[0]

    suggested = _relative_specifier(importer_dir, best)
    if suggested == specifier:
        return None
    return ImportFix(
        importer=Path(importer).as_posix(),
        original_specifier=specifier,
        suggested_specifier=suggested,
        resolved_file=best.as_posix(),
    )
