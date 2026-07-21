"""Dev-server detection and detached launch helpers."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.session.manager import SessionLogger
from shamsu.tools.executor import WORKSPACE_EXIT_CODE, CommandRunner


DEV_COMMAND_PATTERNS = (
    r"\bnpm(?:\s+--workspace\s+\S+)?\s+run\s+dev\b",
    r"\bnpm\s+run\s+dev\b",
    r"\bpnpm\s+dev\b",
    r"\byarn\s+dev\b",
    r"\bvite\b",
    r"\bnext\s+dev\b",
    r"\bnodemon\b",
    r"\bpython\s+manage\.py\s+runserver\b",
    r"\bdjango-admin\s+runserver\b",
    r"\buvicorn\b",
    r"\bflask\s+run\b",
)


@dataclass(frozen=True)
class DevServerResult:
    launched: bool
    command: str
    cwd: str
    pid: int | None = None
    url: str = ""
    message: str = ""
    duplicate: bool = False
    exit_code: int = 0


class DevServerManager:
    def __init__(
        self,
        workspace_root: Path,
        approval_func=ask_approval,
        session_logger: SessionLogger | None = None,
        approval_manager: ApprovalManager | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.approval_manager = approval_manager or ApprovalManager(approval_func, session_logger)
        self.session_logger = session_logger
        self.state_path = self.sandbox.validate(Path(".shamsu") / "dev-servers.json")
        self.command_runner = CommandRunner(
            self.workspace_root,
            approval_func=approval_func,
            session_logger=session_logger,
            approval_manager=self.approval_manager,
        )

    def start(self, command: str, cwd: str | Path = ".") -> DevServerResult:
        command = command.strip() or infer_dev_command(self.workspace_root)
        try:
            validated_cwd = self.sandbox.validate(cwd)
        except (SecurityError, ValueError) as exc:
            return DevServerResult(False, command, str(cwd), message=str(exc), exit_code=WORKSPACE_EXIT_CODE)
        if not validated_cwd.is_dir():
            return DevServerResult(
                False,
                command,
                str(validated_cwd),
                message=f"Working directory does not exist: {validated_cwd}",
                exit_code=WORKSPACE_EXIT_CODE,
            )
        if not is_dev_server_command(command):
            return DevServerResult(False, command, str(validated_cwd), message="Not a dev-server command.")

        approved, exit_code, message, approved_cwd = self.command_runner.validate_and_approve(
            command,
            validated_cwd,
            description=f"Launch dev server: {command}",
        )
        if not approved or approved_cwd is None:
            return DevServerResult(
                False,
                command,
                str(validated_cwd),
                message=message,
                exit_code=exit_code,
            )
        validated_cwd = approved_cwd

        duplicate = self._find_duplicate(command, validated_cwd)
        if duplicate:
            return duplicate

        process = _launch_detached(command, validated_cwd)
        result = DevServerResult(
            launched=True,
            command=command,
            cwd=str(validated_cwd),
            pid=process.pid,
            url=infer_dev_url(command),
            message="Dev server launched in a new terminal window.",
        )
        self._record(result)
        if self.session_logger:
            self.session_logger.log(
                "dev_server.started",
                asdict(result),
                f"Dev server launched: {command}",
                workflow_id="dev-server",
            )
        return result

    def status(self) -> list[DevServerResult]:
        return [item for item in self._read_state() if item.pid is None or _pid_alive(item.pid)]

    def _find_duplicate(self, command: str, cwd: Path) -> DevServerResult | None:
        for item in self.status():
            if item.command == command and Path(item.cwd).resolve() == cwd.resolve():
                return DevServerResult(
                    launched=False,
                    command=command,
                    cwd=str(cwd),
                    pid=item.pid,
                    url=item.url,
                    message="A matching dev server appears to already be running.",
                    duplicate=True,
                )
        return None

    def _read_state(self) -> list[DevServerResult]:
        if not self.state_path.exists():
            return []
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [DevServerResult(**item) for item in raw if isinstance(item, dict)]

    def _record(self, result: DevServerResult) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        items = [item for item in self.status() if not (item.command == result.command and item.cwd == result.cwd)]
        items.append(result)
        self.state_path.write_text(json.dumps([asdict(item) for item in items], indent=2), encoding="utf-8")


def is_dev_server_command(command: str) -> bool:
    normalized = " ".join(command.strip().lower().split())
    return any(re.search(pattern, normalized) for pattern in DEV_COMMAND_PATTERNS)


def extract_dev_command_from_sentence(user_input: str) -> str | None:
    """Pull the actual shell command out of a natural-language sentence.

    E.g. "can you run npm run dev in a new terminal window" -> "npm run dev"
         "run npm --workspace client run dev please" -> "npm --workspace client run dev"

    Returns None when no recognized dev-server command is found in the text.
    """
    normalized = " ".join(user_input.strip().lower().split())
    for pattern in DEV_COMMAND_PATTERNS:
        m = re.search(pattern, normalized)
        if m:
            return m.group(0).strip()
    return None


def infer_dev_command(workspace_root: Path) -> str:
    if (workspace_root / "client" / "package.json").exists():
        return "npm --workspace client run dev"
    if (workspace_root / "package.json").exists():
        return "npm run dev"
    if (workspace_root / "manage.py").exists():
        return "python manage.py runserver"
    return "npm run dev"


def infer_dev_url(command: str) -> str:
    lowered = command.lower()
    if "next" in lowered:
        return "http://localhost:3000/"
    if "flask" in lowered:
        return "http://127.0.0.1:5000/"
    if "django" in lowered or "manage.py runserver" in lowered or "uvicorn" in lowered:
        return "http://127.0.0.1:8000/"
    return "http://localhost:5173/"


def _launch_detached(command: str, cwd: Path) -> subprocess.Popen:
    if sys.platform == "win32":
        # CREATE_NEW_CONSOLE opens a new visible window for this process.
        # cmd /k runs the command and keeps the window open after exit so
        # crash output stays readable.  The returned PID (cmd.exe) stays
        # alive while the server is running, so duplicate-detection works.
        return subprocess.Popen(
            ["cmd.exe", "/k", command],
            cwd=cwd,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    terminal = _terminal_command(command, cwd)
    if terminal:
        return subprocess.Popen(terminal, cwd=cwd, start_new_session=True)
    return subprocess.Popen(command, shell=True, cwd=cwd, start_new_session=True)


def _terminal_command(command: str, cwd: Path) -> list[str] | None:
    if sys.platform == "darwin":
        return ["osascript", "-e", f'tell app "Terminal" to do script "cd {cwd} && {command}"']
    for term in ("x-terminal-emulator", "gnome-terminal", "konsole", "xfce4-terminal"):
        from shutil import which

        if which(term):
            return [term, "--", "sh", "-lc", command]
    return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
