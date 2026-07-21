"""
Thin wrapper around the local `git apply` binary, used as PatchEngine's
preferred patch validation/application tool per SHAMSU's "use existing
local tools first" rule.

`check()` (git apply --check) is a pure dry-run: it never touches disk. It
is used as an extra pre-flight gate alongside PatchEngine's own structural
diff parser. `apply()` performs the real write; `--3way` is only used as a
controlled fallback when a plain apply fails on context drift.

Workspaces that are not git repositories (a bare tmp_path in tests, or a
freshly scaffolded project) simply report `available=False` here and
PatchEngine falls back to its own tested unified-diff applier - this
wrapper is additive, never a hard dependency.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _no_window_flags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def git_binary() -> str | None:
    return shutil.which("git")


def is_git_repo(workspace_root: Path) -> bool:
    return (Path(workspace_root) / ".git").exists()


def available(workspace_root: Path) -> bool:
    return git_binary() is not None and is_git_repo(workspace_root)


def _run_git_apply(args: list[str], diff_text: str, workspace_root: Path) -> tuple[bool, str]:
    binary = git_binary()
    if binary is None:
        return False, "git binary not found on PATH."
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".patch", delete=False, encoding="utf-8", newline=""
    ) as handle:
        handle.write(diff_text)
        patch_path = handle.name
    try:
        completed = subprocess.run(
            [binary, "apply", *args, patch_path],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=_no_window_flags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git apply failed to run: {exc}"
    finally:
        try:
            Path(patch_path).unlink(missing_ok=True)
        except OSError:
            pass
    ok = completed.returncode == 0
    message = (completed.stderr or completed.stdout or "").strip()
    return ok, message


def check(diff_text: str, workspace_root: Path) -> tuple[bool, str]:
    """`git apply --check`: validates the patch applies cleanly without
    writing anything to disk."""
    if not available(workspace_root):
        return False, "git apply unavailable (no git binary or not a git repository)."
    return _run_git_apply(["--check", "-p1", "--whitespace=nowarn"], diff_text, workspace_root)


def apply(diff_text: str, workspace_root: Path, three_way: bool = False) -> tuple[bool, str]:
    """Apply the patch for real via `git apply` (optionally `--3way` as a
    controlled fallback for context drift)."""
    if not available(workspace_root):
        return False, "git apply unavailable (no git binary or not a git repository)."
    args = ["-p1", "--whitespace=nowarn"]
    if three_way:
        args.append("--3way")
    return _run_git_apply(args, diff_text, workspace_root)
