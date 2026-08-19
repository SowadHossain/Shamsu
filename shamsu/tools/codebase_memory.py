"""
Thin adapter around the real upstream Codebase-Memory MCP tool
(https://github.com/DeusData/codebase-memory-mcp).

This module does NOT parse code, build a graph, or infer facts on its own.
Every method here shells out to the real `codebase-memory-mcp` binary's
documented CLI mode (`codebase-memory-mcp cli <tool> '<json-args>'`) and
returns exactly what the tool reports. If the binary is missing or a call
fails, methods return an honest `ok: False` result — never a fabricated
graph fact.

Local-only: remote/non-local `SHAMSU_CODEBASE_MEMORY_URI` values are rejected.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from shamsu.abstract.types import AdapterResult, CodebaseMemoryHealth
from shamsu.indexer.policy import ensure_cbm_ignore

CLI_TIMEOUT_SECONDS = 60
VERSION_TIMEOUT_SECONDS = 5

_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "::1", "[::1]"}


def default_tool_dir() -> Path:
    """SHAMSU-managed external tool cache: ~/.shamsu/tools/codebase-memory-mcp/."""
    return Path.home() / ".shamsu" / "tools" / "codebase-memory-mcp"


def _binary_filename() -> str:
    return "codebase-memory-mcp.exe" if sys.platform == "win32" else "codebase-memory-mcp"


def is_local_uri(uri: str) -> bool:
    """True if `uri` refers to localhost/127.0.0.1/::1 or a plain local file path."""
    uri = uri.strip()
    if not uri:
        return True
    parsed = urlparse(uri)
    if not parsed.scheme:
        # Bare path or host:port with no scheme - treat as a local path unless
        # it looks like a host:port pair with a non-local host.
        return True
    if parsed.scheme == "file":
        return True
    hostname = (parsed.hostname or "").lower()
    return hostname in _LOCAL_HOSTS


def _no_window_flags() -> int:
    if sys.platform != "win32":
        return 0
    return subprocess.CREATE_NO_WINDOW


# Directory names that mean "this tree is not a project". Matched on the PATH,
# not on a single name, so a real project simply called `tmp` is untouched.
_DISPOSABLE_MARKERS = (
    "/eval_",
    "/baseline_artifacts/",
    "/prd_eval",
    "/universal-prd-eval",
    "/phase2_prd_eval_artifacts/",
)


def disposable_workspace(workspace: Path) -> str:
    """Why this directory is throwaway, or ``""`` if it is a real project.

    Two independent signals, both taken from what actually filled the store:

    * it lives under the system temp directory - true of 129 of the 243 indexed
      projects, all of them eval scratchpads from a single day in July;
    * its path names it as eval output - true of 30 more, which sit INSIDE this
      repository and are indexed as separate projects alongside it.

    Returns a reason rather than a bool so the refusal can say which one.
    """
    try:
        resolved = Path(workspace).resolve()
    except OSError:
        return ""
    lowered = resolved.as_posix().lower()
    for root in (tempfile.gettempdir(), os.environ.get("TMPDIR", "")):
        if not root:
            continue
        try:
            root_posix = Path(root).resolve().as_posix().lower()
        except OSError:
            continue
        if lowered == root_posix or lowered.startswith(root_posix.rstrip("/") + "/"):
            return "inside the system temporary directory"
    for marker in _DISPOSABLE_MARKERS:
        if marker in lowered:
            return f"an evaluation artifact directory ({marker.strip('/')})"
    return ""


class CodebaseMemoryAdapter:
    """Adapter around the real, locally-installed Codebase-Memory MCP binary."""

    # Tools that operate on a repo_path directly and must NOT have a "project"
    # arg injected (index_repository) or that enumerate all projects and take
    # no project-scoping arg at all (list_projects).
    _UNSCOPED_TOOLS = frozenset({"index_repository", "list_projects"})

    def __init__(self, tool_dir: Path | None = None) -> None:
        self.tool_dir = (tool_dir or default_tool_dir()).resolve()
        self._project_cache: dict[str, str] = {}

    # -- binary/URI resolution -------------------------------------------------

    def resolve_binary(self) -> Path | None:
        """Resolve the local binary honouring env overrides, then the managed tool dir."""
        explicit_cmd = os.environ.get("SHAMSU_CODEBASE_MEMORY_CMD", "").strip()
        if explicit_cmd:
            candidate = Path(explicit_cmd)
            return candidate if candidate.exists() else None

        explicit_path = os.environ.get("SHAMSU_CODEBASE_MEMORY_PATH", "").strip()
        if explicit_path:
            base = Path(explicit_path)
            candidate = base if base.is_file() else base / _binary_filename()
            return candidate if candidate.exists() else None

        candidate = self.tool_dir / _binary_filename()
        if candidate.exists():
            return candidate

        found = shutil.which("codebase-memory-mcp")
        return Path(found).resolve() if found else None

    def _rejected_remote_uri(self) -> str:
        """Non-empty message if SHAMSU_CODEBASE_MEMORY_URI is set to a non-local URI."""
        uri = os.environ.get("SHAMSU_CODEBASE_MEMORY_URI", "").strip()
        if uri and not is_local_uri(uri):
            return (
                f"Rejected non-local Codebase-Memory MCP URI: {uri}. "
                "Only localhost/127.0.0.1/::1 or local file paths are allowed."
            )
        return ""

    # -- health -----------------------------------------------------------------

    def is_available(self, workspace: Path) -> bool:
        return self.healthcheck(workspace).ok

    def healthcheck(self, workspace: Path) -> CodebaseMemoryHealth:
        remote_rejection = self._rejected_remote_uri()
        if remote_rejection:
            return CodebaseMemoryHealth(available=False, message=remote_rejection)

        binary = self.resolve_binary()
        if binary is None:
            return CodebaseMemoryHealth(
                available=False,
                message=(
                    "Codebase-Memory MCP binary was not found. "
                    f"Expected it at {self.tool_dir / _binary_filename()}, via "
                    "SHAMSU_CODEBASE_MEMORY_CMD/SHAMSU_CODEBASE_MEMORY_PATH, or on PATH."
                ),
            )
        try:
            completed = subprocess.run(
                [str(binary), "--version"],
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_SECONDS,
                creationflags=_no_window_flags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CodebaseMemoryHealth(
                available=False,
                binary_path=str(binary),
                message=f"Codebase-Memory MCP binary could not be run: {exc}",
            )
        if completed.returncode != 0:
            return CodebaseMemoryHealth(
                available=False,
                binary_path=str(binary),
                message=f"Codebase-Memory MCP binary exited with {completed.returncode}.",
            )
        version = (completed.stdout or completed.stderr or "").strip()
        return CodebaseMemoryHealth(
            available=True, binary_path=str(binary), version=version, message="Codebase-Memory MCP is ready."
        )

    def status(self, workspace: Path) -> dict[str, Any]:
        health = self.healthcheck(workspace)
        result: dict[str, Any] = {
            "available": health.available,
            "binary_path": health.binary_path,
            "version": health.version,
            "message": health.message,
        }
        if health.ok:
            result["index_status"] = self.run_cli(workspace, "index_status", {}).to_dict()
        return result

    # -- setup/repair -------------------------------------------------------------

    def setup(self, workspace: Path) -> dict[str, Any]:
        """Install the real upstream binary into SHAMSU's managed tool cache.

        Downloads the platform-matching release asset from the upstream GitHub
        releases (as documented by the project's own install scripts) and
        extracts it locally. Never vendors source into SHAMSU, never installs
        globally/with elevated privileges, never fakes success.
        """
        try:
            return self._download_and_install()
        except Exception as exc:  # pragma: no cover - network/environment dependent
            return {
                "ok": False,
                "error": f"Setup failed: {exc}",
                "manual_steps": (
                    "Install codebase-memory-mcp yourself (see "
                    "https://github.com/DeusData/codebase-memory-mcp#quick-start) and set "
                    "SHAMSU_CODEBASE_MEMORY_CMD to the binary path, or place it at "
                    f"{self.tool_dir / _binary_filename()}."
                ),
            }

    def _download_and_install(self) -> dict[str, Any]:
        import tempfile
        import urllib.request
        import zipfile

        asset_name = self._release_asset_name()
        if asset_name is None:
            return {
                "ok": False,
                "error": f"Unsupported platform for automated setup: {platform.system()}/{platform.machine()}.",
            }
        api_url = "https://api.github.com/repos/DeusData/codebase-memory-mcp/releases/latest"
        with urllib.request.urlopen(api_url, timeout=15) as response:
            release = json.loads(response.read().decode("utf-8"))
        asset = next((a for a in release.get("assets", []) if a.get("name") == asset_name), None)
        if asset is None:
            return {"ok": False, "error": f"Release asset '{asset_name}' not found in latest release."}

        self.tool_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / asset_name
            urllib.request.urlretrieve(asset["browser_download_url"], archive_path)
            if asset_name.endswith(".zip"):
                with zipfile.ZipFile(archive_path) as archive:
                    archive.extractall(self.tool_dir)
            else:
                import tarfile

                with tarfile.open(archive_path) as archive:
                    archive.extractall(self.tool_dir)

        binary = self.tool_dir / _binary_filename()
        if not binary.exists():
            return {"ok": False, "error": f"Extracted archive but binary not found at {binary}."}
        if sys.platform != "win32":
            binary.chmod(binary.stat().st_mode | 0o111)
        return {"ok": True, "path": str(binary)}

    @staticmethod
    def _release_asset_name() -> str | None:
        system = platform.system().lower()
        machine = platform.machine().lower()
        arch = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
        if system == "windows":
            return "codebase-memory-mcp-windows-amd64.zip"
        if system == "darwin":
            return f"codebase-memory-mcp-darwin-{arch}.tar.gz"
        if system == "linux":
            return f"codebase-memory-mcp-linux-{arch}.tar.gz"
        return None

    def repair(self, workspace: Path) -> dict[str, Any]:
        health = self.healthcheck(workspace)
        if not health.ok:
            setup_result = self.setup(workspace)
            health = self.healthcheck(workspace)
            if not health.ok:
                return {
                    "ok": False,
                    "message": health.message,
                    "setup_result": setup_result,
                }
        return {"ok": True, "message": health.message, "binary_path": health.binary_path}

    def start_server(self, workspace: Path) -> dict[str, Any]:
        """SHAMSU queries via CLI mode (stateless per-call), not a persistent MCP
        server, so this only verifies the binary responds - it never claims a
        server process was started."""
        health = self.healthcheck(workspace)
        return {"ok": health.ok, "mode": "cli", "message": health.message}

    # -- indexing -----------------------------------------------------------------

    def index_workspace(self, workspace: Path, force: bool = False) -> dict[str, Any]:
        throwaway = "" if force else disposable_workspace(workspace)
        if throwaway:
            # The store is GLOBAL and keyed by a mangled absolute path, so every
            # throwaway directory anything ever ran in accumulates forever and
            # nothing cleans up. Measured 2026-08-19: 243 indexed projects, of
            # which 129 lived under a temp directory and 30 more were
            # eval-artifact folders inside this repo, indexed as separate
            # projects beside it. Among the 129 were `pytest-of-.../pytest-320/
            # test_workspace_qa_workflow_fal0` - SHAMSU's own test suite had
            # been filling the store for weeks - and four workspaces created
            # while testing these very fixes.
            #
            # A graph that outlives the directory it describes can never answer
            # anything, so building it was pure cost.
            #
            # The guard sits HERE, at the only place that writes to the global
            # store, rather than in `AbstractService`: every test that exercises
            # indexing does so through a fake adapter and never touches the real
            # store, so guarding the service would have made nine tests opt out
            # of a hazard they never posed.
            #
            # `force` is the escape - somebody who explicitly asks to index a
            # scratch directory has a reason.
            return {
                "ok": False,
                "skipped": "disposable_workspace",
                "error": (
                    f"Not indexing {workspace}: it is {throwaway}, so the index would "
                    "outlive the directory it describes and stay in the global store "
                    "forever. Pass force=True if that is really what you want."
                ),
            }
        ignore_policy = ensure_cbm_ignore(workspace)
        if not ignore_policy.get("ok"):
            return {
                "ok": False,
                "error": f"Could not install Codebase-Memory exclusions: {ignore_policy.get('error', 'unknown error')}",
                "ignore_policy": ignore_policy,
            }
        result = self.run_cli(workspace, "index_repository", {"repo_path": str(workspace)}).to_dict()
        return {**result, "ignore_policy": ignore_policy}

    def refresh_workspace(self, workspace: Path) -> dict[str, Any]:
        # index_repository re-syncs an already-indexed project incrementally
        # per upstream docs; there is no separate "refresh" tool.
        refreshed = self.index_workspace(workspace)
        if not refreshed.get("ok"):
            return refreshed
        polluted_paths = self._internal_index_paths(workspace)
        if not polluted_paths:
            return refreshed

        deleted = self.delete_workspace_index(workspace)
        if not deleted.get("ok"):
            return {
                "ok": False,
                "error": "Code memory still contains internal SHAMSU paths and could not be rebuilt.",
                "polluted_paths": polluted_paths,
                "delete_result": deleted,
            }
        rebuilt = self.index_workspace(workspace)
        return {
            **rebuilt,
            "rebuilt_for_policy": True,
            "removed_internal_paths": polluted_paths,
        }

    def delete_workspace_index(self, workspace: Path) -> dict[str, Any]:
        key = str(Path(workspace).resolve())
        result = self.run_cli(workspace, "delete_project", {}).to_dict()
        if result.get("ok"):
            self._project_cache.pop(key, None)
        return result

    def _internal_index_paths(self, workspace: Path) -> list[str]:
        query = (
            "MATCH (f:File) WHERE f.file_path =~ '^\\\\.shamsu/.*' "
            "RETURN f.file_path AS file_path LIMIT 20"
        )
        result = self.run_cli(workspace, "query_graph", {"query": query}).to_dict()
        if not result.get("ok"):
            return []
        paths: list[str] = []
        columns = result.get("columns") or []
        rows = result.get("rows") or []
        index = columns.index("file_path") if "file_path" in columns else 0
        for row in rows:
            value = row[index] if isinstance(row, list) and len(row) > index else row
            if value:
                paths.append(str(value))
        return paths

    # -- queries --------------------------------------------------------------

    def query(self, workspace: Path, query: str, limit: int = 20) -> dict[str, Any]:
        return self.run_cli(workspace, "search_graph", {"name_pattern": query, "limit": limit}).to_dict()

    def get_exports(self, workspace: Path, path: str) -> dict[str, Any]:
        # File nodes expose the relative path as `file_path` (`path` doesn't
        # exist on them - verified against a real index). `NOT n:Module`
        # excludes the file's own module-wrapper node so only the symbols it
        # actually defines come back.
        cypher = (
            f"MATCH (f:File)-[:DEFINES]->(n) WHERE f.file_path =~ '.*{_cypher_escape(path)}' "
            "AND NOT n:Module RETURN n.name AS name, labels(n) AS labels LIMIT 100"
        )
        return self.run_cli(workspace, "query_graph", {"query": cypher}).to_dict()

    def get_imports(self, workspace: Path, path: str) -> dict[str, Any]:
        cypher = (
            f"MATCH (f:File)-[:IMPORTS]->(m) WHERE f.file_path =~ '.*{_cypher_escape(path)}' "
            "RETURN m.name AS name LIMIT 100"
        )
        return self.run_cli(workspace, "query_graph", {"query": cypher}).to_dict()

    def get_symbols(self, workspace: Path, query_or_path: str) -> dict[str, Any]:
        if "/" in query_or_path or "\\" in query_or_path:
            return self.run_cli(workspace, "search_graph", {"file_pattern": query_or_path, "limit": 100}).to_dict()
        return self.run_cli(workspace, "search_graph", {"name_pattern": query_or_path, "limit": 100}).to_dict()

    def get_references(self, workspace: Path, symbol_or_path: str) -> dict[str, Any]:
        return self.run_cli(
            workspace, "trace_path", {"function_name": symbol_or_path, "direction": "inbound"}
        ).to_dict()

    def get_impact(self, workspace: Path, path_or_symbol: str) -> dict[str, Any]:
        # detect_changes maps the current git diff to affected symbols with risk
        # classification - the real "edit impact" tool this project ships.
        return self.run_cli(workspace, "detect_changes", {}).to_dict()

    def search_code(self, workspace: Path, pattern: str, limit: int = 20, ignore_case: bool = True) -> dict[str, Any]:
        """Grep-like text search over indexed files - backs SHAMSU's general
        workspace search (shamsu/retriever/search.py), replacing the old
        SQLite FTS5 index."""
        return self.run_cli(
            workspace, "search_code", {"pattern": pattern, "limit": limit, "ignore_case": ignore_case}
        ).to_dict()

    def get_code_snippet(self, workspace: Path, qualified_name: str) -> dict[str, Any]:
        if not qualified_name:
            return {"ok": False, "error": "Missing qualified_name."}
        return self.run_cli(workspace, "get_code_snippet", {"qualified_name": qualified_name}).to_dict()

    def get_module_contract(self, workspace: Path, path: str) -> dict[str, Any]:
        exports = self.get_exports(workspace, path)
        imports = self.get_imports(workspace, path)
        return {"ok": exports.get("ok", False) and imports.get("ok", False), "exports": exports, "imports": imports}

    def list_projects(self, workspace: Path) -> dict[str, Any]:
        return self.run_cli(workspace, "list_projects", {}).to_dict()

    def index_status_raw(self, workspace: Path) -> dict[str, Any]:
        return self.run_cli(workspace, "index_status", {}).to_dict()

    # -- project resolution -----------------------------------------------------

    def _resolve_project(self, workspace: Path) -> str | None:
        """Every query tool (search_graph, trace_path, query_graph, index_status,
        detect_changes, ...) needs an explicit "project" name - unlike
        index_repository, they do not accept/derive it from repo_path. Resolve
        it from list_projects by matching root_path against the workspace."""
        key = str(workspace.resolve())
        cached = self._project_cache.get(key)
        if cached:
            return cached
        result = self._invoke(workspace, "list_projects", {})
        if not result.ok:
            return None
        target = key.replace("\\", "/").rstrip("/").lower()
        for project in result.data.get("projects", []):
            root = str(project.get("root_path", "")).replace("\\", "/").rstrip("/").lower()
            if root == target:
                name = project.get("name", "")
                if name:
                    self._project_cache[key] = name
                return name or None
        return None

    # -- CLI plumbing -----------------------------------------------------------

    def run_cli(self, workspace: Path, tool: str, args: dict[str, Any]) -> AdapterResult:
        if tool not in self._UNSCOPED_TOOLS and "project" not in args:
            project = self._resolve_project(workspace)
            if project is None:
                return AdapterResult(
                    ok=False,
                    error=(
                        "No indexed Codebase-Memory MCP project found for this workspace. "
                        "Run /abstract build first."
                    ),
                )
            args = {**args, "project": project}
        return self._invoke(workspace, tool, args)

    def _invoke(self, workspace: Path, tool: str, args: dict[str, Any]) -> AdapterResult:
        remote_rejection = self._rejected_remote_uri()
        if remote_rejection:
            return AdapterResult(ok=False, error=remote_rejection)

        binary = self.resolve_binary()
        if binary is None:
            return AdapterResult(ok=False, error="Codebase-Memory MCP binary is not available.")

        # `cli <tool> '<json>'` is the base documented form and already prints
        # JSON to stdout. `--raw` is only for pretty-print suppression when
        # piping through `jq` and some builds parse it as a positional tool
        # name (not a flag) here, so it's deliberately omitted.
        command = [str(binary), "cli", tool, json.dumps(args)]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                cwd=str(workspace),
                timeout=CLI_TIMEOUT_SECONDS,
                creationflags=_no_window_flags(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return AdapterResult(ok=False, error=f"Failed to run '{tool}': {exc}")

        if completed.returncode != 0:
            return AdapterResult(ok=False, error=(completed.stderr or completed.stdout or "non-zero exit").strip())

        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            return AdapterResult(ok=False, error=f"Non-JSON output from '{tool}'.")

        if isinstance(payload, dict):
            return AdapterResult(ok=True, data=payload)
        return AdapterResult(ok=True, data={"result": payload})


def _cypher_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
