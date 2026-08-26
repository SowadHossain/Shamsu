"""Full-file rewrite fallback for malformed small-model diffs."""
from __future__ import annotations

import difflib
import re
from pathlib import Path

from shamsu.interfaces import IContextBuilder, ILLMManager, IPatchEngine
from shamsu.types import SearchResult

# Filename-ish token: has an extension of 1-6 word chars. Used to spot files a
# user named in free text ("fix multiply in calc.py") so we can ground on them.
_FILE_TOKEN_RE = re.compile(r"[\w./\\-]+\.\w{1,6}")


def mentioned_workspace_files(workspace_root: Path, text: str, limit: int = 5) -> list[str]:
    """Return workspace-relative paths for files explicitly named in *text* that
    actually exist in the workspace. Grounds edits on the file the user pointed
    at even when there's no traceback and the code search misses it - the exact
    'fix X in calc.py' case a local model otherwise hallucinates. Uses the
    ignore-aware FileWalker for basename matches so it never walks node_modules
    or .git."""
    workspace_root = Path(workspace_root).resolve()
    tokens = [
        t.strip().replace("\\", "/").strip("/") for t in _FILE_TOKEN_RE.findall(text or "")
    ]
    tokens = [t for t in tokens if t]
    if not tokens:
        return []
    found: list[str] = []
    walker_files: list[Path] | None = None
    for token in tokens:
        # 1) Direct path relative to the workspace root.
        candidate = (workspace_root / token).resolve()
        rel: str | None = None
        try:
            if candidate.is_file():
                rel = candidate.relative_to(workspace_root).as_posix()
        except (OSError, ValueError):
            rel = None
        # 2) Basename match via the ignore-aware walker (lazily; only if needed).
        if rel is None:
            base = token.rsplit("/", 1)[-1].lower()
            if walker_files is None:
                # `indexer.walker.FileWalker` used to wrap this one call and
                # nothing else, so the wrapper went and the call stayed.
                from shamsu.indexer.policy import walk_workspace_files
                try:
                    walker_files = walk_workspace_files(
                        workspace_root.resolve(), indexable_only=True
                    )
                except Exception:
                    walker_files = []
            for match in walker_files:
                if match.name.lower() == base:
                    try:
                        rel = match.relative_to(workspace_root).as_posix()
                    except ValueError:
                        continue
                    break
        if rel and rel not in found:
            found.append(rel)
        if len(found) >= limit:
            break
    return found

FULL_REWRITE_INSTRUCTIONS = (
    "Your previous unified diff could not be applied because of a formatting error. "
    "Do NOT output a diff or patch. Output the ENTIRE new content of the file below, "
    "from the first line to the last, with the requested change applied. No markdown "
    "code fences, no explanation, no diff markers (no ---, +++, @@, leading +/-). "
    "Output only the raw file content."
)

# Cap per-file grounding so a huge file doesn't blow the prompt budget; above
# this the search snippets still cover the relevant regions and the model works
# from those. ~24k chars ~= 6k tokens, comfortably inside an 8B ctx window.
MAX_GROUNDING_BYTES = 24_000


def full_file_results(workspace_root: Path, paths: list[str]) -> list[SearchResult]:
    """Read the real, current content of the target files as high-priority
    SearchResults so the model edits against ground truth instead of guessing
    from partial/stale index snippets. This is what stops a local model from
    inventing lines (e.g. an @@ header past EOF) that were never in the file.
    Files above MAX_GROUNDING_BYTES are skipped (snippets still apply)."""
    workspace_root = Path(workspace_root).resolve()
    seen: set[str] = set()
    results: list[SearchResult] = []
    for path in paths:
        if not path or path in seen:
            continue
        seen.add(path)
        target = (workspace_root / path).resolve()
        try:
            target.relative_to(workspace_root)
            content = target.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue
        if len(content.encode("utf-8")) > MAX_GROUNDING_BYTES:
            continue
        results.append(
            SearchResult(
                file_path=path,
                language=target.suffix.lstrip(".") or "text",
                line_start=1,
                line_end=max(1, len(content.splitlines())),
                content=content,
                score=20.0,   # outranks search snippets so it leads the pack
            )
        )
    return results


def lenient_diff_target_paths(diff_text: str) -> list[str]:
    """Extract intended target paths from diff headers even when hunks are bad."""
    paths: list[str] = []
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            header = line[4:]
        elif line.startswith("--- "):
            header = line[4:]
        else:
            continue
        header = header.split("\t")[0].strip()
        for prefix in ("a/", "b/"):
            if header.startswith(prefix):
                header = header[len(prefix):]
        if header and header != "/dev/null" and header not in paths:
            paths.append(header)
    return paths


def strip_code_fences(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines)
    return text


def _safe_target(workspace_root: Path, rel: str) -> Path | None:
    try:
        candidate = (workspace_root / rel).resolve()
        candidate.relative_to(workspace_root)
    except (OSError, ValueError):
        return None
    return candidate


async def rewrite_files_fully(
    *,
    llm: ILLMManager,
    context_builder: IContextBuilder,
    patch_engine: IPatchEngine,
    workspace_root: Path,
    request: str,
    target_paths: list[str],
    specialist: str,
    max_files: int = 3,
    plan_text: str = "",
) -> list[str]:
    """Ask for full file content and apply it through PatchEngine.

    This is a format-correction retry on the plan the first attempt already
    had, not a new planning opportunity - `plan_text` (if given) is folded
    into the prompt as-is rather than calling the planner model again.
    """
    changed: list[str] = []
    for rel in target_paths[:max_files]:
        abs_path = _safe_target(workspace_root, rel)
        if abs_path is None:
            continue
        current = ""
        if abs_path.exists() and abs_path.is_file():
            current = abs_path.read_text(encoding="utf-8", errors="replace")
        prompt = (
            f"{FULL_REWRITE_INSTRUCTIONS}\n\n"
            f"File: {rel}\n\n"
            + (f"Plan from planner model:\n{plan_text}\n\n" if plan_text.strip() else "")
            + f"Task:\n{request}\n\n"
            f"Current content of {rel} (empty if this is a new file):\n{current}"
        )
        pack = context_builder.pack(
            results=[],
            request=prompt,
            task_id="full-rewrite",
            step_id=2,
            specialist=specialist,
        )
        response = await llm.run_specialist(specialist, pack)
        new_content = strip_code_fences(response.raw)
        if not new_content.strip():
            continue
        if not new_content.endswith("\n"):
            new_content += "\n"
        rewrite_diff = _full_file_diff(rel, current, new_content, exists=abs_path.exists())
        ok, _error = patch_engine.validate_diff(rewrite_diff)
        if not ok:
            continue
        if patch_engine.apply(rewrite_diff, workspace_root):
            changed.append(rel)
    return changed


def _full_file_diff(rel: str, old_content: str, new_content: str, *, exists: bool) -> str:
    old_lines = old_content.splitlines(keepends=True) if exists else []
    new_lines = new_content.splitlines(keepends=True)
    old_name = f"a/{rel}" if exists else "/dev/null"
    new_name = f"b/{rel}"
    return "".join(
        difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile=old_name,
            tofile=new_name,
            lineterm="\n",
        )
    )
