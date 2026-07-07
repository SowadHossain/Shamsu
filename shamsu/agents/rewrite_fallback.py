"""Full-file rewrite fallback for malformed small-model diffs."""
from __future__ import annotations

import difflib
from pathlib import Path

from shamsu.interfaces import IContextBuilder, ILLMManager, IPatchEngine

FULL_REWRITE_INSTRUCTIONS = (
    "Your previous unified diff could not be applied because of a formatting error. "
    "Do NOT output a diff or patch. Output the ENTIRE new content of the file below, "
    "from the first line to the last, with the requested change applied. No markdown "
    "code fences, no explanation, no diff markers (no ---, +++, @@, leading +/-). "
    "Output only the raw file content."
)


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
