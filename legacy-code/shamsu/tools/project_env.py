"""Deterministic project-local Python environment resolution."""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

_PYTHON_MARKERS = (
    "pyproject.toml",
    "requirements.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
)
_SHELL_PREFIX = r"(?P<prefix>^|(?:&&|\|\||[;&|])\s*)"
_PIP_INSTALL_RE = re.compile(
    _SHELL_PREFIX + r"(?:(?:python3?|python)\s+-m\s+pip|pip3?)\s+install\b",
    re.IGNORECASE,
)
_PIP_RE = re.compile(_SHELL_PREFIX + r"pip3?(?=\s|$)", re.IGNORECASE)
_PYTHON_PIP_RE = re.compile(
    _SHELL_PREFIX + r"(?:python3?|python)\s+-m\s+pip(?=\s|$)",
    re.IGNORECASE,
)
_PYTHON_RE = re.compile(_SHELL_PREFIX + r"python3?(?:\.exe)?(?=\s|$)", re.IGNORECASE)
_PYTHON3_RE = re.compile(_SHELL_PREFIX + r"python3(?:\.exe)?(?=\s|$)", re.IGNORECASE)
_INVALID_INSTALL_VERSION_RE = re.compile(
    r"(?P<head>(?:^|(?:&&|\|\||[;&|])\s*)"
    r"(?:(?:python3?|python)\s+-m\s+pip|pip3?)\s+install)"
    r"\s+--version(?=\s+\S)",
    re.IGNORECASE,
)
_STATE_FILE = Path(".shamsu") / "project-environment.json"


@dataclass(frozen=True)
class CommandResolution:
    requested_command: str
    command: str
    project_root: str
    environment_kind: str
    interpreter: str = ""
    bootstraps_environment: bool = False
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.command != self.requested_command

    def to_dict(self) -> dict[str, object]:
        return {**asdict(self), "changed": self.changed}


class ProjectEnvironmentResolver:
    """Resolve bare Python/pip commands to the nearest project environment."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        platform_name: str | None = None,
        runtime_python: str | None = None,
        environ: Mapping[str, str] | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.platform_name = platform_name or os.name
        self.runtime_python = runtime_python or sys.executable
        self.environ = dict(os.environ if environ is None else environ)
        self.which = which

    def resolve(self, command: str, cwd: Path) -> CommandResolution:
        requested = command
        command = _normalize_install_command(command)
        project_root = self._project_root(Path(cwd).resolve())
        if not _contains_bare_python_command(command):
            return self._resolution(requested, command, project_root, "explicit", reason="No bare Python command.")

        existing = self._existing_environment(project_root)
        if existing is not None:
            rewritten = _rewrite_for_interpreter(command, self._quote(existing))
            return self._resolution(
                requested,
                rewritten,
                project_root,
                "project-venv",
                interpreter=str(existing),
                reason="Resolved bare Python commands to an existing project-local environment.",
            )

        manager = self._project_manager(project_root)
        installs_packages = bool(_PIP_INSTALL_RE.search(command))
        if manager == "poetry":
            rewritten = _rewrite_for_poetry(command)
            return self._resolution(
                requested,
                rewritten,
                project_root,
                "poetry",
                interpreter="poetry run python",
                reason="Resolved bare Python commands through the project's Poetry environment.",
            )
        if manager == "uv":
            if installs_packages:
                venv_dir = project_root / ".venv"
                interpreter = self._venv_python(venv_dir)
                rewritten = _rewrite_uv_install(command, self._quote(interpreter))
                bootstrap = f"uv venv {self._quote(venv_dir)}"
                rewritten = f"{bootstrap} && {rewritten}"
                return self._resolution(
                    requested,
                    rewritten,
                    project_root,
                    "uv",
                    interpreter=str(interpreter),
                    bootstraps=True,
                    reason="Bootstraps and installs into the uv project's local .venv.",
                )
            rewritten = _rewrite_for_uv(command)
            return self._resolution(
                requested,
                rewritten,
                project_root,
                "uv",
                interpreter="uv run python",
                reason="Resolved bare Python commands through the project's uv environment.",
            )

        if installs_packages:
            venv_dir = project_root / ".venv"
            interpreter = self._venv_python(venv_dir)
            bootstrap = f"{self._quote(self.runtime_python)} -m venv {self._quote(venv_dir)}"
            rewritten = _rewrite_for_interpreter(command, self._quote(interpreter))
            rewritten = f"{bootstrap} && {rewritten}"
            return self._resolution(
                requested,
                rewritten,
                project_root,
                "bootstrap-venv",
                interpreter=str(interpreter),
                bootstraps=True,
                reason="Bootstraps .venv so package installation cannot reach the ambient interpreter.",
            )

        rewritten = command
        if self.platform_name == "nt":
            rewritten = _PYTHON3_RE.sub(
                lambda match: f"{match.group('prefix')}{self._quote(self.runtime_python)}",
                command,
            )
        return self._resolution(
            requested,
            rewritten,
            project_root,
            "ambient",
            interpreter=self.runtime_python if rewritten != command else "",
            reason="No project environment is required for this non-install command.",
        )

    def persist_resolution(self, resolution: CommandResolution) -> Path | None:
        """Persist a successful project environment choice for later runs."""
        if resolution.environment_kind in {"ambient", "explicit"} or not resolution.interpreter:
            return None
        project_root = Path(resolution.project_root).resolve()
        try:
            project_root.relative_to(self.workspace_root)
        except ValueError:
            return None
        state_path = project_root / _STATE_FILE
        payload = {
            "schema_version": 1,
            "project_root": ".",
            "environment_kind": resolution.environment_kind,
            "interpreter": resolution.interpreter,
        }
        try:
            if state_path.is_file():
                existing = json.loads(state_path.read_text(encoding="utf-8"))
                if existing == payload:
                    return state_path
            state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, state_path)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        return state_path

    def _project_root(self, cwd: Path) -> Path:
        current = cwd
        while True:
            if any((current / marker).exists() for marker in _PYTHON_MARKERS):
                return current
            if current == self.workspace_root:
                return cwd
            parent = current.parent
            try:
                parent.relative_to(self.workspace_root)
            except ValueError:
                return cwd
            current = parent

    def _existing_environment(self, project_root: Path) -> Path | None:
        local = self._venv_python(project_root / ".venv")
        if local.is_file():
            return local
        persisted = self._persisted_interpreter(project_root)
        if persisted is not None:
            return persisted
        active_raw = self.environ.get("VIRTUAL_ENV", "").strip()
        if not active_raw:
            return None
        active = Path(active_raw).resolve()
        try:
            active.relative_to(project_root)
        except ValueError:
            return None
        interpreter = self._venv_python(active)
        return interpreter if interpreter.is_file() else None

    def _persisted_interpreter(self, project_root: Path) -> Path | None:
        state_path = project_root / _STATE_FILE
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            raw = str(payload.get("interpreter") or "").strip()
            interpreter = Path(raw).resolve()
            interpreter.relative_to(project_root)
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, ValueError):
            return None
        return interpreter if interpreter.is_file() else None

    def _project_manager(self, project_root: Path) -> str:
        if (project_root / "uv.lock").is_file() and self.which("uv"):
            return "uv"
        if (project_root / "poetry.lock").is_file() and self.which("poetry"):
            return "poetry"
        pyproject = _read_pyproject(project_root / "pyproject.toml")
        tool = pyproject.get("tool") if isinstance(pyproject, dict) else None
        if isinstance(tool, dict):
            if isinstance(tool.get("uv"), dict) and self.which("uv"):
                return "uv"
            if isinstance(tool.get("poetry"), dict) and self.which("poetry"):
                return "poetry"
        return ""

    def _venv_python(self, venv_dir: Path) -> Path:
        if self.platform_name == "nt":
            return venv_dir / "Scripts" / "python.exe"
        return venv_dir / "bin" / "python"

    def _quote(self, value: Path | str) -> str:
        text = str(value)
        if self.platform_name == "nt":
            return subprocess.list2cmdline([text])
        return shlex.quote(text)

    @staticmethod
    def _resolution(
        requested: str,
        command: str,
        project_root: Path,
        kind: str,
        *,
        interpreter: str = "",
        bootstraps: bool = False,
        reason: str,
    ) -> CommandResolution:
        return CommandResolution(
            requested_command=requested,
            command=command,
            project_root=str(project_root),
            environment_kind=kind,
            interpreter=interpreter,
            bootstraps_environment=bootstraps,
            reason=reason,
        )


def _contains_bare_python_command(command: str) -> bool:
    return bool(_PYTHON_RE.search(command) or _PIP_RE.search(command))


def _normalize_install_command(command: str) -> str:
    """Repair the invalid ``pip install --version PACKAGE`` model form.

    ``--version`` belongs to ``pip`` itself, not ``pip install``. Small models
    sometimes insert it when the request contains an exact ``name==version``
    specifier, causing pip to print its own version or fail without installing
    anything. Removing it is unambiguous when a package argument follows.
    """
    return _INVALID_INSTALL_VERSION_RE.sub(lambda match: match.group("head"), command)


def _rewrite_for_interpreter(command: str, interpreter: str) -> str:
    rewritten = _PYTHON_PIP_RE.sub(
        lambda match: f"{match.group('prefix')}{interpreter} -m pip",
        command,
    )
    rewritten = _PIP_RE.sub(
        lambda match: f"{match.group('prefix')}{interpreter} -m pip",
        rewritten,
    )
    return _rewrite_python(rewritten, interpreter)


def _rewrite_python(command: str, replacement: str) -> str:
    return _PYTHON_RE.sub(
        lambda match: f"{match.group('prefix')}{replacement}",
        command,
    )


def _rewrite_for_poetry(command: str) -> str:
    rewritten = _PYTHON_PIP_RE.sub(
        lambda match: f"{match.group('prefix')}poetry run python -m pip",
        command,
    )
    rewritten = _PIP_RE.sub(
        lambda match: f"{match.group('prefix')}poetry run python -m pip",
        rewritten,
    )
    return _rewrite_python(rewritten, "poetry run python")


def _rewrite_for_uv(command: str) -> str:
    rewritten = _PYTHON_PIP_RE.sub(
        lambda match: f"{match.group('prefix')}uv run python -m pip",
        command,
    )
    rewritten = _PIP_RE.sub(
        lambda match: f"{match.group('prefix')}uv pip",
        rewritten,
    )
    return _rewrite_python(rewritten, "uv run python")


def _rewrite_uv_install(command: str, interpreter: str) -> str:
    rewritten = _PIP_INSTALL_RE.sub(
        lambda match: f"{match.group('prefix')}uv pip install --python {interpreter}",
        command,
    )
    return _rewrite_for_interpreter(rewritten, interpreter)


def _read_pyproject(path: Path) -> dict:
    try:
        with path.open("rb") as handle:
            parsed = tomllib.load(handle)
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, tomllib.TOMLDecodeError):
        return {}
