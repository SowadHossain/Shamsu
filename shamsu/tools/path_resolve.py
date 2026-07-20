"""Deterministic workspace path resolution.

Single source of truth for turning a *reported* path - one that came out of a
compile error, traceback, or model diff (e.g. ``src/App.tsx``) - into the real
workspace-relative path (``client/src/App.tsx``). The file-discovery tools
(``read_file`` / ``find_file`` / ``grep_files``) and the diff-based bug-fix
workflow both build on these helpers so path handling behaves identically no
matter which loop is driving.

Dependency-light on purpose (stdlib only): the repair/bug-fix stack imports it
without pulling in the whole tool registry.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from shamsu.indexer.policy import DEFAULT_EXCLUDED_DIRS

# Directories the file-discovery tools never descend into: version-control
# metadata, virtualenvs, dependency trees, and build output. Kept broad so
# find_file/grep_files stay fast on JS projects.
_HEAVY_DIRS = DEFAULT_EXCLUDED_DIRS

# Common web-app source roots. When a model asks for `src/App.tsx` but the file
# actually lives under `client/src/App.tsx`, these prefixes let the resolver
# reconstruct the real path instead of failing blindly.
_FRONTEND_ROOTS = ("client/", "frontend/", "app/", "web/", "packages/", "src/")

# Upper bound on how many workspace files the resolver/discovery tools scan.
_MAX_WORKSPACE_SCAN = 8000


def _normalize_workspace_path(path: str) -> str:
    """Normalize a model-supplied path: strip quotes/backticks, unify slashes,
    and drop a leading ``./``. Never resolves outside the workspace - that stays
    the Sandbox's job."""
    text = str(path or "").strip()
    # Models often wrap paths in quotes or backticks: `src/App.tsx`, "src/App.tsx".
    text = text.strip("`\"'").strip()
    text = text.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.strip()


def _walk_workspace_files(root: Path, limit: int = _MAX_WORKSPACE_SCAN) -> list[str]:
    """Relative POSIX paths of every file under ``root``, skipping heavy dirs.

    Uses ``os.walk`` with in-place pruning so it never descends into
    ``node_modules`` / ``.venv`` etc. - important for JS projects where a naive
    ``rglob`` would scan tens of thousands of files."""
    results: list[str] = []
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _HEAVY_DIRS]
        for name in filenames:
            rel = (Path(dirpath) / name).relative_to(root).as_posix()
            results.append(rel)
            if len(results) >= limit:
                return results
    return results


def _path_exists_case_insensitive(root: Path, rel: str) -> Path | None:
    """Resolve ``rel`` under ``root`` matching each path segment case-insensitively.
    Returns the real path (with on-disk casing) or None. Case-only mismatches are
    the single most common cross-platform path bug, so this is checked first."""
    rel = _normalize_workspace_path(rel)
    if not rel:
        return None
    current = root
    for part in PurePosixPath(rel).parts:
        if part in ("", "."):
            continue
        if not current.is_dir():
            return None
        match: Path | None = None
        try:
            for child in current.iterdir():
                if child.name.lower() == part.lower():
                    match = child
                    break
        except OSError:
            return None
        if match is None:
            return None
        current = match
    return current if current != root else None


def _find_path_candidates(root: Path, requested: str, limit: int = 10) -> list[str]:
    """Ranked recovery candidates for a missing path, best match first.

    Ranking (lower = better): case-insensitive exact path, suffix match
    (``src/App.tsx`` -> ``client/src/App.tsx``), a known frontend root prefix,
    exact basename, same stem/different extension, then a basename substring."""
    requested = _normalize_workspace_path(requested)
    if not requested:
        return []
    req_lower = requested.lower()
    req_name = PurePosixPath(requested).name
    req_name_lower = req_name.lower()
    req_stem = PurePosixPath(req_name).stem.lower()

    all_files = _walk_workspace_files(root)
    lower_map = {rel.lower(): rel for rel in all_files}

    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()

    def add(rel: str, rank: int) -> None:
        if rel in seen:
            return
        seen.add(rel)
        ranked.append((rank, rel))

    # 1. Case-insensitive exact relative path.
    if req_lower in lower_map:
        add(lower_map[req_lower], 0)

    # 2. Suffix match: requested is a trailing sub-path of a real file.
    for rel in all_files:
        rl = rel.lower()
        if rl == req_lower:
            add(rel, 0)
        elif rl.endswith("/" + req_lower):
            add(rel, 1)

    # 3. Known frontend source roots prepended to the request.
    for prefix in _FRONTEND_ROOTS:
        cand = (prefix + requested).lower()
        if cand in lower_map:
            add(lower_map[cand], 2)

    # 4. Exact basename anywhere in the tree.
    for rel in all_files:
        if PurePosixPath(rel).name.lower() == req_name_lower:
            add(rel, 3)

    # 5. Same stem, different extension (App.tsx <-> App.jsx).
    if len(req_stem) >= 2:
        for rel in all_files:
            p = PurePosixPath(rel)
            if p.stem.lower() == req_stem and p.name.lower() != req_name_lower:
                add(rel, 4)

    # 6. Basename substring - weakest signal, ranked last.
    if len(req_name_lower) >= 3:
        for rel in all_files:
            if req_name_lower in PurePosixPath(rel).name.lower():
                add(rel, 5)

    ranked.sort(key=lambda item: (item[0], len(item[1]), item[1]))
    return [rel for _, rel in ranked[:limit]]


def _strong_path_candidates(root: Path, requested: str, limit: int = 5) -> list[str]:
    """The subset of candidates strong enough to block a wrong-path *write*:
    case-insensitive exact, suffix, or frontend-root matches - i.e. "the same
    relative file at a different root". Basename/substring matches are excluded
    so genuinely new files (a fresh top-level README) are not falsely blocked."""
    requested = _normalize_workspace_path(requested)
    if not requested:
        return []
    req_lower = requested.lower()
    all_files = _walk_workspace_files(root)
    lower_map = {rel.lower(): rel for rel in all_files}

    out: list[str] = []
    seen: set[str] = set()

    def add(rel: str) -> None:
        if rel not in seen:
            seen.add(rel)
            out.append(rel)

    if req_lower in lower_map:
        add(lower_map[req_lower])
    for rel in all_files:
        if rel.lower().endswith("/" + req_lower):
            add(rel)
    for prefix in _FRONTEND_ROOTS:
        cand = (prefix + requested).lower()
        if cand in lower_map:
            add(lower_map[cand])
    return out[:limit]


def _find_files_by_query(root: Path, query: str, limit: int = 20) -> list[str]:
    """Fuzzy-ish file search for find_file: exact basename, then suffix-path,
    then substring. Case-insensitive throughout."""
    query = _normalize_workspace_path(query).lower()
    if not query:
        return []
    exact_name: list[str] = []
    suffix: list[str] = []
    substring: list[str] = []
    for rel in _walk_workspace_files(root):
        rl = rel.lower()
        name = PurePosixPath(rl).name
        if name == query:
            exact_name.append(rel)
        elif rl == query or rl.endswith("/" + query):
            suffix.append(rel)
        elif query in rl:
            substring.append(rel)
    ordered: list[str] = []
    for group in (exact_name, suffix, substring):
        for rel in group:
            if rel not in ordered:
                ordered.append(rel)
    return ordered[:limit]


def _format_path_candidates(candidates: list[str]) -> str:
    return ", ".join(candidates)


def resolve_reported_path(workspace_root: Path, reported: str) -> str | None:
    """Map a reported path (from a compile error / traceback / model diff) to the
    real workspace-relative path, or None when it cannot be resolved unambiguously.

    Deterministic and conservative:
      1. If the path already points at a real file, return it (normalized).
      2. Otherwise take the *strong* candidates (case-insensitive exact, suffix,
         or frontend-root match). Return the single best one only when it is
         unambiguous - either there is exactly one, or the top candidate is a
         strictly better rank than the rest. Never guess between two equally
         plausible files.
    """
    normalized = _normalize_workspace_path(reported)
    if not normalized:
        return None
    root = Path(workspace_root)
    direct = (root / normalized)
    try:
        if direct.is_file():
            return normalized
    except OSError:
        return None
    candidates = _strong_path_candidates(root, normalized)
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    # More than one strong candidate: only accept the top one if it is a clearly
    # better match (shorter path / exact suffix) than the runner-up. The list is
    # already ordered exact -> suffix -> frontend-root, so prefer an exact-suffix
    # winner; otherwise stay ambiguous and refuse to guess.
    best, second = candidates[0], candidates[1]
    best_is_exact_suffix = best.lower().endswith("/" + normalized.lower())
    second_is_exact_suffix = second.lower().endswith("/" + normalized.lower())
    if best_is_exact_suffix and not second_is_exact_suffix:
        return best
    return None


def remap_diff_paths(
    workspace_root: Path, diff_text: str
) -> tuple[str, list[tuple[str, str]]]:
    """Rewrite unified-diff file headers whose path does not exist to the real
    workspace path when it can be resolved unambiguously.

    Handles ``--- a/<p>``, ``+++ b/<p>``, ``diff --git a/<p> b/<p>``, and
    ``rename from/to`` lines. Returns the rewritten diff and the list of
    ``(reported, resolved)`` remaps applied (deduped, in first-seen order) so the
    caller can tell the user which paths were corrected.

    A ``/dev/null`` side (file creation/deletion) is left untouched.
    """
    if not diff_text:
        return diff_text, []
    root = Path(workspace_root)
    remaps: dict[str, str] = {}

    def resolve(reported: str) -> str | None:
        reported = reported.strip()
        if not reported or reported == "/dev/null":
            return None
        if reported in remaps:
            return remaps[reported]
        resolved = resolve_reported_path(root, reported)
        if resolved and resolved != _normalize_workspace_path(reported):
            remaps[reported] = resolved
            return resolved
        return None

    out_lines: list[str] = []
    for line in diff_text.splitlines():
        rewritten = _remap_header_line(line, resolve)
        out_lines.append(rewritten)
    new_text = "\n".join(out_lines)
    if diff_text.endswith("\n") and not new_text.endswith("\n"):
        new_text += "\n"
    # Preserve first-seen order.
    ordered = list(remaps.items())
    return new_text, ordered


def _remap_header_line(line: str, resolve) -> str:
    if line.startswith(("--- ", "+++ ")):
        sign = line[:4]  # "--- " or "+++ "
        # Split off a trailing tab-delimited timestamp if present.
        path_part, sep, trailer = line[4:].partition("\t")
        had_ab = path_part.startswith(("a/", "b/"))
        resolved = resolve(_strip_ab_prefix(path_part))
        if resolved is None:
            return line
        prefix = ("a/" if sign.startswith("---") else "b/") if had_ab else ""
        return f"{sign}{prefix}{resolved}{sep}{trailer}"
    if line.startswith("diff --git "):
        return _remap_git_line(line, resolve)
    for rename_marker in ("rename from ", "rename to ", "copy from ", "copy to "):
        if line.startswith(rename_marker):
            resolved = resolve(line[len(rename_marker):].strip())
            return f"{rename_marker}{resolved}" if resolved is not None else line
    return line


def _remap_git_line(line: str, resolve) -> str:
    # Format: ``diff --git a/<p1> b/<p2>``
    parts = line.split(" ")
    if len(parts) != 4:
        return line
    a_resolved = resolve(_strip_ab_prefix(parts[2]))
    b_resolved = resolve(_strip_ab_prefix(parts[3]))
    a_out = f"a/{a_resolved}" if a_resolved is not None else parts[2]
    b_out = f"b/{b_resolved}" if b_resolved is not None else parts[3]
    return f"diff --git {a_out} {b_out}"


def _strip_ab_prefix(path: str) -> str:
    path = path.strip()
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return path
