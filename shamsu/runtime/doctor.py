"""
Read-only environment diagnostics for SHAMSU.

`doctor` never installs, deletes, or repairs anything - it only reports.
It exists so "something feels broken" no longer defaults to wiping the
venv and reinstalling; run doctor first and see what it actually finds.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from shamsu.abstract.service import AbstractService
from shamsu.action_ledger import store as action_ledger_store
from shamsu.diagnostics import doctor as diagnostics_doctor
from shamsu.memory.service import MemoryService
from shamsu.runtime.models import allowed_model_names, is_allowed_model
from shamsu.runtime.ollama import RuntimeStatus, collect_status, repo_root_from_runtime
from shamsu.runtime.state_upgrade import STATE_SCHEMA_VERSION, read_state_schema_version
from shamsu.tools.browser import BrowserTool
from shamsu.tools.web import WebConfig, WebServiceManager

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


def check_cookbook(status: RuntimeStatus | None = None) -> DoctorCheck:
    status = status if status is not None else collect_status()
    if not status.installed_models:
        return DoctorCheck(
            "model_cookbook",
            True,
            "No installed Ollama models were reported; cookbook enforcement will apply on pulls.",
        )
    off_cookbook = [model for model in status.installed_models if not is_allowed_model(model)]
    if not off_cookbook:
        return DoctorCheck(
            "model_cookbook",
            True,
            "Installed SHAMSU models fit the 8GB cookbook.",
        )
    return DoctorCheck(
        "model_cookbook",
        False,
        f"Installed model(s) outside SHAMSU's 8GB cookbook: {', '.join(off_cookbook)}",
        f"SHAMSU will only pull allowed models: {', '.join(allowed_model_names())}. "
        "Remove larger models manually if they are causing memory pressure.",
    )


def check_graphiti_memory(workspace: Path, service: MemoryService | None = None) -> DoctorCheck:
    status = (service or MemoryService(workspace)).status()
    if status.normal_mode_allowed:
        return DoctorCheck(
            "graphiti_memory",
            True,
            f"Graphiti memory is healthy. {status.health.message}",
        )
    return DoctorCheck(
        "graphiti_memory",
        False,
        status.health.message,
        "Run `/memory setup` or `/memory repair`.",
    )

def check_codebase_memory(workspace: Path, service: AbstractService | None = None) -> DoctorCheck:
    status = (service or AbstractService(workspace)).status()
    if status.health.ok and not status.index.stale:
        return DoctorCheck(
            "codebase_memory",
            True,
            f"Codebase-Memory MCP is healthy. {status.index.message}",
        )
    if not status.health.ok:
        return DoctorCheck(
            "codebase_memory",
            False,
            status.health.message,
            "Run `/abstract setup` (or `python -m shamsu.abstract.cli setup`).",
        )
    return DoctorCheck(
        "codebase_memory",
        False,
        status.index.message,
        "Run `/abstract refresh` to rebuild the workspace code-memory index.",
    )



def check_diagnostics(workspace: Path) -> DoctorCheck:
    try:
        status = diagnostics_doctor.check(workspace)
    except Exception as exc:
        return DoctorCheck(
            "diagnostics",
            False,
            f"Diagnostic helper status failed: {exc}",
            "Run `/diagnostics repair`.",
        )
    llmlingua = status.get("llmlingua", {})
    detail = (
        f"Diagnostics config ready. reviewdog/errorformat={status.get('reviewdog_errorformat', {}).get('external_binary')}; "
        f"Drain3-style compactor={status.get('drain3', {}).get('external_package')}; "
        f"LLMLingua enabled={llmlingua.get('enabled', False)}."
    )
    return DoctorCheck("diagnostics", True, detail)


def check_workspace_state(workspace: Path) -> DoctorCheck:
    """Validate persisted run/index/memory state without repairing it."""
    workspace = workspace.resolve()
    problems: list[str] = []

    json_paths = (
        workspace / ".shamsu" / "abstract" / "last-index.json",
        workspace / ".shamsu" / "abstract" / "status.json",
        workspace / ".shamsu" / "memory" / "status.json",
        workspace / ".shamsu" / "diagnostics" / "last-error-packet.json",
    )
    payloads: dict[Path, dict] = {}
    for path in json_paths:
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path.relative_to(workspace)} is invalid JSON: {exc}")
            continue
        if not isinstance(value, dict):
            problems.append(f"{path.relative_to(workspace)} is not a JSON object")
            continue
        payloads[path] = value

    index_path = workspace / ".shamsu" / "abstract" / "last-index.json"
    index = payloads.get(index_path, {})
    if index:
        generation = int(index.get("workspace_generation", 0) or 0)
        indexed_generation = int(index.get("indexed_generation", 0) or 0)
        if indexed_generation > generation:
            problems.append("abstract index generation is ahead of the workspace generation")
        if index.get("forced_stale") and generation == indexed_generation:
            problems.append("abstract index is forced stale but its generations are equal")

    memory_db = workspace / ".shamsu" / "memory" / "memory.db"
    if memory_db.exists():
        try:
            connection = sqlite3.connect(f"file:{memory_db.as_posix()}?mode=ro", uri=True)
            try:
                check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            finally:
                connection.close()
            if check.lower() != "ok":
                problems.append(f"memory database integrity check failed: {check}")
        except sqlite3.DatabaseError as exc:
            problems.append(f"memory database is unreadable: {exc}")

    for item in action_ledger_store.list_runs(workspace, limit=20):
        validation = action_ledger_store.validate_run(workspace, item.run_id)
        if not validation["ok"]:
            problems.append(
                f"run {item.run_id} is corrupt: {'; '.join(validation['errors'][:3])}"
            )

    if problems:
        return DoctorCheck(
            "workspace_state",
            False,
            " | ".join(problems),
            "Inspect the named artifacts with `/run validate <id>`; rebuild only the affected index or state store.",
        )
    return DoctorCheck(
        "workspace_state",
        True,
        "Run artifacts, index metadata, and local memory state are internally consistent.",
    )


def check_state_schema(workspace: Path) -> DoctorCheck:
    version = read_state_schema_version(workspace)
    if version is None:
        return DoctorCheck(
            "state_schema",
            True,
            f"No state marker exists yet; schema {STATE_SCHEMA_VERSION} will be initialized on first run.",
        )
    if version == STATE_SCHEMA_VERSION:
        return DoctorCheck("state_schema", True, f"Workspace state schema {version} is current.")
    if version > STATE_SCHEMA_VERSION:
        return DoctorCheck(
            "state_schema",
            False,
            f"Workspace state schema {version} is newer than this SHAMSU supports.",
            "Upgrade SHAMSU before using this workspace.",
        )
    return DoctorCheck(
        "state_schema",
        False,
        f"Workspace state schema {version} needs upgrade to {STATE_SCHEMA_VERSION}.",
        "Start SHAMSU once to run the idempotent workspace-state upgrade.",
    )


def check_web_capability(workspace: Path) -> DoctorCheck:
    config = WebConfig()
    service = WebServiceManager(workspace, config.searxng_url).status()
    fallback_ready = config.enabled and config.provider in {"auto", "duckduckgo"}
    ok = config.enabled and (service.running or fallback_ready)
    detail = (
        f"enabled={config.enabled}; provider={config.provider}; "
        f"SearXNG={service.state}; DuckDuckGo fallback={'ready' if fallback_ready else 'disabled'}."
    )
    return DoctorCheck(
        "web_capability",
        ok,
        detail,
        "Enable web search or configure a provider with `/web status` and `/web setup`." if not ok else "",
    )


def check_browser_capability(workspace: Path) -> DoctorCheck:
    status = BrowserTool(workspace).status()
    return DoctorCheck(
        "browser_capability",
        status.available,
        status.message,
        "Run `python -m playwright install chromium`." if not status.available else "",
    )


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
    codebase_memory_service: AbstractService | None = None,
    graphiti_memory_service: MemoryService | None = None,
) -> DoctorReport:
    resolved_repo_root = (repo_root or repo_root_from_runtime()).resolve()
    resolved_workspace = (workspace or resolved_repo_root).resolve()
    checks = (
        check_editable_install(resolved_repo_root),
        check_ollama(ollama_status),
        check_cookbook(ollama_status),
        check_nested_workspaces(resolved_workspace),
        check_ancestor_workspace(resolved_workspace),
        check_path_manifest(bin_dir),
        check_graphiti_memory(resolved_workspace, graphiti_memory_service),
        check_codebase_memory(resolved_workspace, codebase_memory_service),
        check_diagnostics(resolved_workspace),
        check_web_capability(resolved_workspace),
        check_browser_capability(resolved_workspace),
        check_state_schema(resolved_workspace),
        check_workspace_state(resolved_workspace),
    )
    return DoctorReport(checks=checks)


def run_first_run_checks(
    workspace: Path,
    ollama_status: RuntimeStatus | None = None,
    codebase_memory_service: AbstractService | None = None,
    graphiti_memory_service: MemoryService | None = None,
) -> DoctorReport:
    """Collect the six capabilities needed for a productive first session."""
    status = ollama_status if ollama_status is not None else collect_status()
    checks = (
        check_ollama(status),
        check_cookbook(status),
        check_codebase_memory(workspace, codebase_memory_service),
        check_graphiti_memory(workspace, graphiti_memory_service),
        check_web_capability(workspace),
        check_browser_capability(workspace),
    )
    return DoctorReport(checks=checks)


def write_first_run_report(workspace: Path, report: DoctorReport) -> Path:
    path = Path(workspace).resolve() / ".shamsu" / "first-run-report.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(check) for check in report.checks], indent=2), encoding="utf-8")
    return path


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
