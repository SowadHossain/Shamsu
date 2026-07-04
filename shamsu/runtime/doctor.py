"""
Read-only environment diagnostics for SHAMSU.

`doctor` never installs, deletes, or repairs anything — it only reports.
It exists so "something feels broken" no longer defaults to wiping the
venv and reinstalling; run doctor first and see what it actually finds.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shamsu.runtime.ollama import RuntimeStatus, collect_status, repo_root_from_runtime

IGNORED_DIR_NAMES = {".venv", ".git", "node_modules", "__pycache__"}


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str
    suggestion: str = ""


@dataclass(frozen=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...] = field(default_factory=tuple)

    @property
    def all_ok(self) -> bool:
        return all(check.ok for check in self.checks)


def check_editable_install(repo_root: Path) -> DoctorCheck:
    repo_root = repo_root.resolve()
    try:
        import shamsu

        package_path = Path(shamsu.__file__).resolve()
    except Exception as exc:  # pragma: no cover - defensive
        return DoctorCheck(
            "editable_install",
            False,
            f"Could not import shamsu: {exc}",
            "Reinstall with `.\\scripts\\install.ps1` or `scripts/install.sh`.",
        )
    if package_path.is_relative_to(repo_root):
        return DoctorCheck(
            "editable_install", True, f"shamsu package imports from inside {repo_root}."
        )
    return DoctorCheck(
        "editable_install",
        False,
        f"shamsu imports from {package_path.parent}, not the expected repo ({repo_root}).",
        "Reinstall with `.\\scripts\\install.ps1` or `scripts/install.sh` to reinstate the editable install.",
    )


def check_ollama(status: RuntimeStatus | None = None) -> DoctorCheck:
    status = status if status is not None else collect_status()
    if status.ready:
        return DoctorCheck(
            "ollama", True, "Ollama is installed, running, and all required models are present."
        )
    detail = status.message or "Ollama runtime is not ready."
    return DoctorCheck("ollama", False, detail, "Run `models repair` (or `/models repair` in the REPL).")


def _global_launcher_dir() -> Path:
    """`~/.shamsu` holds the user-local launcher + PATH manifest, not a project workspace."""
    return (Path.home() / ".shamsu").resolve()


def find_nested_workspaces(scan_root: Path) -> list[Path]:
    """Find `.shamsu` folders under `scan_root` other than `scan_root/.shamsu` itself."""
    scan_root = scan_root.resolve()
    primary = scan_root / ".shamsu"
    global_dir = _global_launcher_dir()
    found: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(scan_root):
        dirnames[:] = [d for d in dirnames if d not in IGNORED_DIR_NAMES]
        if ".shamsu" in dirnames:
            candidate = (Path(dirpath) / ".shamsu").resolve()
            if candidate != primary and candidate != global_dir:
                found.append(candidate)
            dirnames.remove(".shamsu")
    return sorted(found)


def check_nested_workspaces(scan_root: Path) -> DoctorCheck:
    nested = find_nested_workspaces(scan_root)
    if not nested:
        return DoctorCheck(
            "nested_workspaces", True, f"No stray nested .shamsu folders found under {scan_root}."
        )
    listed = ", ".join(str(p) for p in nested)
    return DoctorCheck(
        "nested_workspaces",
        False,
        f"Found extra .shamsu folder(s) besides the main one: {listed}",
        "These come from running shamsu with a different working directory inside this tree. "
        "Remove them manually, or rerun uninstall (it now cleans these up too).",
    )


def find_ancestor_workspace(path: Path, max_levels: int = 8) -> Path | None:
    """Search upward from `path` for the nearest ancestor with its own `.shamsu/`.

    Skips `~/.shamsu`, which is the global launcher/PATH manifest directory,
    not a project workspace, even though it shares the same folder name.
    """
    current = path.resolve()
    global_dir = _global_launcher_dir()
    for _ in range(max_levels):
        parent = current.parent
        if parent == current:
            return None
        candidate = parent / ".shamsu"
        if candidate.is_dir() and candidate.resolve() != global_dir:
            return parent
        current = parent
    return None


def check_ancestor_workspace(workspace: Path) -> DoctorCheck:
    workspace = workspace.resolve()
    ancestor = find_ancestor_workspace(workspace)
    if ancestor is None:
        return DoctorCheck(
            "ancestor_workspace", True, "No parent directory has a separate SHAMSU workspace."
        )
    return DoctorCheck(
        "ancestor_workspace",
        False,
        f"A parent directory already has a SHAMSU workspace at {ancestor}, separate from {workspace}.",
        f"Pass --workspace {ancestor} if you meant to use that existing workspace instead of creating a new one here.",
    )


def check_path_manifest(bin_dir: Path | None = None) -> DoctorCheck:
    if sys.platform != "win32":
        return DoctorCheck(
            "path_manifest",
            True,
            "PATH is not auto-managed on this platform; add the launcher directory to PATH yourself if desired.",
        )
    bin_dir = bin_dir or (Path.home() / ".shamsu" / "bin")
    manifest_path = bin_dir.parent / "path.json"
    if not manifest_path.exists():
        return DoctorCheck(
            "path_manifest",
            True,
            "No SHAMSU PATH manifest found (command-line launcher install was skipped or not yet run).",
        )
    try:
        # PowerShell's `Set-Content -Encoding UTF8` writes a BOM; utf-8-sig
        # strips it if present and reads plain UTF-8 fine either way.
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        return DoctorCheck("path_manifest", False, f"Could not read PATH manifest: {exc}")

    entry = manifest.get("path_entry", "")
    current_path = os.environ.get("PATH", "")
    normalized_entries = {
        os.path.normcase(os.path.normpath(part))
        for part in current_path.split(os.pathsep)
        if part
    }
    if entry and os.path.normcase(os.path.normpath(entry)) in normalized_entries:
        return DoctorCheck("path_manifest", True, f"SHAMSU launcher directory is on PATH: {entry}")
    return DoctorCheck(
        "path_manifest",
        False,
        f"SHAMSU launcher directory is registered but not visible in this shell's PATH: {entry}",
        "Open a new terminal so Windows picks up the updated PATH.",
    )


def run_doctor(
    workspace: Path | None = None,
    repo_root: Path | None = None,
    ollama_status: RuntimeStatus | None = None,
    bin_dir: Path | None = None,
) -> DoctorReport:
    resolved_repo_root = (repo_root or repo_root_from_runtime()).resolve()
    resolved_workspace = (workspace or resolved_repo_root).resolve()
    checks = (
        check_editable_install(resolved_repo_root),
        check_ollama(ollama_status),
        check_nested_workspaces(resolved_workspace),
        check_ancestor_workspace(resolved_workspace),
        check_path_manifest(bin_dir),
    )
    return DoctorReport(checks=checks)


def format_report(report: DoctorReport) -> str:
    lines: list[str] = []
    for check in report.checks:
        marker = "OK  " if check.ok else "WARN"
        lines.append(f"[{marker}] {check.name}: {check.detail}")
        if not check.ok and check.suggestion:
            lines.append(f"       -> {check.suggestion}")
    lines.append("")
    lines.append(
        "Everything looks fine." if report.all_ok else "Some checks need attention (see WARN lines above)."
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m shamsu.runtime.doctor")
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument(
        "--workspace",
        default=None,
        help="Workspace to check for nested/ancestor state. Defaults to the SHAMSU repo root.",
    )
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else None
    report = run_doctor(workspace=workspace)
    if args.as_json:
        print(json.dumps([asdict(c) for c in report.checks], indent=2))
    else:
        print(format_report(report))
    return 0 if report.all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
