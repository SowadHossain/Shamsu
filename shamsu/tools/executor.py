"""
Internal command execution helpers for workspace-bound SHAMSU tools.
"""
from __future__ import annotations

import atexit
import contextlib
import re
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.interfaces import ICommandRunner
from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.audit import AuditLogger
from shamsu.safety.commands import classify_command, redact
from shamsu.safety.sandbox import Sandbox, SecurityError
from shamsu.session.manager import SessionLogger
from shamsu.tools.project_env import CommandResolution, ProjectEnvironmentResolver
from shamsu.runtime.timeouts import TimeoutConfig
from shamsu.types import ApprovalRequest, CommandRisk, TestRunResult

BLOCKED_EXIT_CODE = 126
DENIED_EXIT_CODE = 125
TIMEOUT_EXIT_CODE = 124
WORKSPACE_EXIT_CODE = 127


#: POSIX-isms a model reaches for by reflex, and what `cmd.exe` does with them
#: instead. Every one of these was measured, not guessed - live 2026-08-24 in
#: `demo-3/asteroid`:
#:
#:   `mkdir -p src assets public`  -> created a directory named `-p`, which is
#:                                    still sitting in that workspace
#:   `curl ... | head -50`         -> "'head' is not recognized as an internal
#:                                    or external command"
#:
#: Only rewrites what is unambiguous. A model writing `mkdir -p` means "make
#: this path, parents included, and do not fail if it exists", which is
#: precisely `mkdir` on cmd - the flag is the only thing in the way.
_POSIX_FIXES: tuple[tuple[str, "str | Callable[[re.Match], str]"], ...] = (
    # `mkdir -p a b c` -> `mkdir a\c`; cmd's mkdir already makes parents, but
    # it reads `/b` in a forward-slash path as a SWITCH, so the separators have
    # to go too or the rewrite trades one failure for another.
    (
        r"(?i)(?P<prefix>^|(?:&&|\|\||[;&|])\s*)mkdir\s+-p\s+(?P<paths>[^&|;<>]+)",
        lambda m: f"{m.group('prefix')}mkdir {m.group('paths').replace('/', chr(92))}",
    ),
    # NOT `rm -rf`. Translating a destructive command is the one place this
    # must not help: `rm -rf /` is on the blocklist by its POSIX spelling, and
    # rewriting it - even downstream of the classifier - moves a refused command
    # one edit closer to running. A model that wants a tree gone can be told the
    # Windows spelling and blocked on it like anyone else.
    # `head -N` / `tail -N` at the end of a pipe.
    (r"(?i)\|\s*head\s+-n?\s*(?P<count>\d+)\s*$", r'| powershell -NoProfile -Command "$input | Select-Object -First \g<count>"'),
    (r"(?i)\|\s*tail\s+-n?\s*(?P<count>\d+)\s*$", r'| powershell -NoProfile -Command "$input | Select-Object -Last \g<count>"'),
    # `which x` -> where x
    (r"(?i)(?P<prefix>^|(?:&&|\|\||[;&|])\s*)which\s+", r"\g<prefix>where "),
)


def _strip_posix_background(command: str) -> tuple[str, bool]:
    """Peel a trailing `&` off, and say whether one was there.

    On cmd.exe `&` is a command SEPARATOR, not backgrounding, so
    `npm run dev -- --host 0.0.0.0 &` runs the server in the foreground exactly
    as if the `&` were absent - and then times out. The model asked for a
    background process; give it one rather than the opposite.
    """
    stripped = (command or "").rstrip()
    if stripped.endswith("&") and not stripped.endswith("&&"):
        return stripped[:-1].rstrip(), True
    return command, False


def _platform_command(command: str) -> str:
    """Use the running interpreter for `python3` command segments on Windows,
    and translate the POSIX-isms cmd.exe silently misreads."""
    if os.name != "nt" or not command:
        return command
    for pattern, replacement in _POSIX_FIXES:
        command = re.sub(pattern, replacement, command)
    executable = f'"{sys.executable}"'
    return re.sub(
        r"(?i)(?P<prefix>^|(?:&&|\|\||[;&|])\s*)python3(?:\.exe)?(?=\s|$)",
        lambda match: f"{match.group('prefix')}{executable}",
        command,
    )



def _kill_process_tree(process: "subprocess.Popen") -> None:
    """Kill the command AND everything it started.

    `subprocess.run(..., capture_output=True, timeout=N)` cannot enforce N when
    the command leaves a survivor. It kills the direct child - the shell - then
    calls `communicate()` again to drain the pipes; a grandchild that inherited
    those handles keeps them open, so `communicate()` waits for an EOF that
    never comes and `TimeoutExpired` is never raised.

    Live 2026-08-18: `cd frontend && python -m http.server 8000 &` hung a turn
    for 28 minutes against a 120s timeout, with no tool result written at all.
    """
    if sys.platform == "win32":
        # taskkill walks the tree; the shell's children are not in our group.
        with contextlib.suppress(Exception):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    else:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        process.kill()


def _run_command_bounded(
    command: str,
    cwd: "Path",
    timeout_seconds: float,
    creationflags: int,
) -> "subprocess.CompletedProcess":
    """Run *command*, and actually come back within *timeout_seconds*.

    Raises `subprocess.TimeoutExpired` on timeout, like `subprocess.run` was
    supposed to - but only after killing the whole tree, and never blocking on
    output a survivor is still holding.
    """
    popen_kwargs: dict = {
        "shell": True,
        "cwd": str(cwd),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "creationflags": creationflags,
    }
    if sys.platform != "win32":
        # Own process group, so the whole tree can be signalled at once.
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        try:
            # The tree is dead, so the pipes should close promptly. If something
            # STILL holds them, abandon the output rather than hang - a partial
            # result the model can act on beats a turn that never returns.
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            # Something survived the tree kill and still holds the pipes -
            # `start /b` detaches a child out of the shell's tree entirely.
            # ABANDON the output; do not close the pipes. `communicate` reads
            # them on daemon threads, and closing a file object whose reader is
            # blocked inside read() waits on that thread's lock, which is the
            # very hang being escaped (measured: 120s instead of 7.5s).
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(command, timeout_seconds, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


#: Commands whose whole job is to NOT exit. Running one in the foreground under
#: a timeout is not a check that failed - it is a check that could never have
#: succeeded, and the harness paid full price for it every time.
#:
#: Live 2026-08-24, `demo-3/asteroid`: 13 commands ran in a two-hour session.
#: EIGHT were `npm run dev`, each burning the full 120s and returning exit 124
#: with a stdout that says, in plain text, `VITE v5.4.21 ready in 421 ms` and
#: the URL it is serving on. The information the model needed was in hand at
#: 400ms and thrown away at 120s, sixteen minutes of wall clock in total. The
#: model even tried to background it - `npm run dev -- --host 0.0.0.0 &` - and
#: cmd.exe read the `&` as a separator and ran it in the foreground anyway.
_SERVER_SHAPED = re.compile(
    r"(?ix)"
    r"(?:^|&&|\|\||[;&|])\s*"
    r"(?:[\w./\\-]*\s+)*?"          # env prefixes, `cd x &&` already split off
    r"(?:"
    r"npm\s+(?:run\s+)?(?:dev|start|serve|watch)"
    r"|(?:yarn|pnpm|bun)\s+(?:run\s+)?(?:dev|start|serve|watch)"
    r"|vite(?:\s|$)|next\s+dev|nuxt\s+dev|ng\s+serve"
    r"|flask\s+run|uvicorn\s|gunicorn\s|hypercorn\s|daphne\s"
    r"|python[\w.]*\s+-m\s+http\.server|python[\w.]*\s+-m\s+flask"
    r"|manage\.py\s+runserver|rails\s+server|php\s+-S"
    r"|serve(?:\s|$)|http-server(?:\s|$)|live-server(?:\s|$)"
    r")"
)

#: A server announcing itself. Any of these means "it is up, stop waiting".
_SERVER_READY = re.compile(
    r"(?i)(https?://[^\s]+|\blistening\b|\bready in\b|\bserving\b|\bstarted server\b"
    r"|\bcompiled successfully\b|\brunning on\b)"
)

#: How long to wait for a detached server to say something before handing the
#: model what there is. Short: the point is to stop paying 120s for a line that
#: arrives in under one.
_DETACH_READY_SECONDS = 12.0

#: ...but a process that is still ALIVE this long after starting is a server
#: that is up, banner or no banner. `python -m http.server` writes its one
#: line to stderr through a block-buffered pipe and nothing reaches the log
#: until it flushes, which is never - so waiting for a line to appear paid
#: the full ceiling for a server that had been serving since 200ms.
_DETACH_ALIVE_SECONDS = 3.0

#: Everything this process started and has not reaped, so a session cannot
#: strand a server holding port 3000 after it exits.
_DETACHED: "dict[int, tuple[subprocess.Popen, str]]" = {}


def _forget_dead_detached() -> None:
    """Drop the ones that have already exited.

    The registry exists so nothing is stranded holding port 3000, not as a
    history of every server a long session ever started.
    """
    for pid, (process, _) in list(_DETACHED.items()):
        if process.poll() is not None:
            _DETACHED.pop(pid, None)


def _reap_detached() -> None:
    for process, _ in list(_DETACHED.values()):
        if process.poll() is None:
            _kill_process_tree(process)
    _DETACHED.clear()


atexit.register(_reap_detached)


def looks_like_a_server(command: str) -> bool:
    """Does this command intend to keep running?"""
    return bool(command) and bool(_SERVER_SHAPED.search(command))


def _run_command_detached(
    command: str,
    cwd: "Path",
    log_path: "Path",
    creationflags: int,
) -> tuple[int, str, str]:
    """Start *command* and leave it running, returning what it said on the way up.

    The contract with the caller is deliberately not `subprocess.run`'s: there
    is no exit code to report because the process has not exited, and that is
    the point. A server that is still up is the SUCCESS case.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8", errors="replace")
    popen_kwargs: dict = {
        "shell": True,
        "cwd": str(cwd),
        "stdout": handle,
        "stderr": subprocess.STDOUT,
        "text": True,
        "creationflags": creationflags,
    }
    if sys.platform != "win32":
        popen_kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen(command, **popen_kwargs)
    finally:
        # The child holds its own duplicate of this handle. Ours is finished
        # with the moment the process exists, and keeping it would leak one
        # write handle per detached server for the life of the session - a
        # server is started to be LEFT running, so nothing would ever close it.
        with contextlib.suppress(Exception):
            handle.close()
    _forget_dead_detached()
    _DETACHED[process.pid] = (process, str(log_path))

    deadline = time.monotonic() + _DETACH_READY_SECONDS
    alive_enough = time.monotonic() + _DETACH_ALIVE_SECONDS
    captured = ""
    while time.monotonic() < deadline:
        time.sleep(0.25)
        with contextlib.suppress(OSError):
            captured = log_path.read_text(encoding="utf-8", errors="replace")
        if process.poll() is not None:
            break
        if _SERVER_READY.search(captured):
            break
        if time.monotonic() >= alive_enough:
            break

    with contextlib.suppress(OSError):
        captured = log_path.read_text(encoding="utf-8", errors="replace")

    exited = process.poll()
    if exited is not None:
        # It stopped on its own, so it was not a server after all - report it
        # exactly as a foreground run would have.
        _DETACHED.pop(process.pid, None)
        return int(exited), redact(captured), "" if exited == 0 else redact(captured)

    note = (
        f"[still running: pid {process.pid}, output -> {log_path}]" + chr(10)
        + "This command does not exit, so it was started in the background and "
        "left up. It is serving NOW: check it with a request (curl, or a fetch) "
        "rather than starting it again. Starting it a second time will only take "
        "another port."
    )
    return 0, redact(captured) + chr(10) + note, ""


class CommandRunner(ICommandRunner):
    def __init__(
        self,
        workspace_root: Path,
        approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
        timeout_seconds: int | None = None,
        session_logger: SessionLogger | None = None,
        approval_manager: ApprovalManager | None = None,
        action_ledger: ActionLedger | None = None,
        environment_resolver: ProjectEnvironmentResolver | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)
        self.approval_func = approval_func
        self.approval_manager = approval_manager or ApprovalManager(approval_func, session_logger)
        self.timeout_seconds = int(timeout_seconds if timeout_seconds is not None else TimeoutConfig.from_env().tool_timeout)
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.audit_logger = AuditLogger(self.workspace_root)
        self.environment_resolver = environment_resolver or ProjectEnvironmentResolver(
            self.workspace_root
        )
        self.last_diagnostics_path = ""
        self.last_command_resolution: CommandResolution | None = None

    def run(self, command: str, cwd: Path) -> tuple[int, str, str]:
        requested_command = command
        self.last_diagnostics_path = ""
        self.last_command_resolution = None
        try:
            validated_cwd = self._validate_cwd(cwd)
        except (SecurityError, ValueError) as exc:
            ledger_cmd_id = (
                self.action_ledger.log_command_start(requested_command, cwd)
                if self.action_ledger
                else ""
            )
            if self.session_logger:
                self.session_logger.log(
                    "command.failed",
                    {"command": requested_command, "error": str(exc)},
                    "Command rejected before execution",
                    workflow_id="command",
                )
            self.audit_logger.log(
                "command_run",
                "error",
                details={"command": requested_command, "stderr": str(exc)},
            )
            if self.action_ledger:
                self.action_ledger.log_command_finish(
                    ledger_cmd_id,
                    requested_command,
                    cwd,
                    WORKSPACE_EXIT_CODE,
                    "",
                    str(exc),
                )
            return WORKSPACE_EXIT_CODE, "", str(exc)

        resolution = self.environment_resolver.resolve(requested_command, validated_cwd)
        self.last_command_resolution = resolution
        command = resolution.command
        resolution_payload = resolution.to_dict()
        if resolution.changed:
            if self.session_logger:
                self.session_logger.log(
                    "command.environment_resolved",
                    resolution_payload,
                    f"Resolved command through {resolution.environment_kind}.",
                    workflow_id="command",
                )
            if self.action_ledger:
                self.action_ledger.log_event(
                    "project_environment_resolved",
                    **resolution_payload,
                )
        if self.session_logger:
            self.session_logger.log(
                "command.started",
                {
                    "command": command,
                    "requested_command": requested_command,
                    "cwd": str(validated_cwd),
                    "environment": resolution.environment_kind,
                },
                f"Command started: {command}",
                workflow_id="command",
            )
        ledger_cmd_id = (
            self.action_ledger.log_command_start(command, validated_cwd)
            if self.action_ledger
            else ""
        )

        risk = classify_command(command)
        if risk == CommandRisk.BLOCKED:
            if self.session_logger:
                self.session_logger.log(
                    "command.blocked",
                    {"command": command, "risk": risk.value},
                    f"Blocked command: {command}",
                    workflow_id="command",
                )
            self.audit_logger.log("command_run", "blocked", details={"command": command, "risk": risk.value})
            if self.action_ledger:
                self.action_ledger.log_command_finish(
                    ledger_cmd_id, command, validated_cwd, BLOCKED_EXIT_CODE, "", f"Blocked command: {command}"
                )
            return BLOCKED_EXIT_CODE, "", f"Blocked command: {command}"

        if risk == CommandRisk.MEDIUM:
            request = ApprovalRequest(
                action_type="run_command",
                description=f"Run command: {command}",
                risk_level="medium",
                preview=command,
                working_dir=str(validated_cwd),
                reason="Command is medium risk or unknown.",
            )
            self.approval_manager.session_logger = self.session_logger
            if not self.approval_manager.ask(request):
                if self.session_logger:
                    self.session_logger.log(
                        "command.denied",
                        {"command": command, "cwd": str(validated_cwd)},
                        f"Command denied: {command}",
                        workflow_id="command",
                    )
                self.audit_logger.log(
                    "approval",
                    "denied",
                    details={"action_type": request.action_type, "preview": request.preview},
                )
                self.audit_logger.log("command_run", "denied", details={"command": command, "risk": risk.value})
                if self.action_ledger:
                    self.action_ledger.log_command_finish(
                        ledger_cmd_id, command, validated_cwd, DENIED_EXIT_CODE, "", f"Command denied by user: {command}"
                    )
                return DENIED_EXIT_CODE, "", f"Command denied by user: {command}"
            self.audit_logger.log(
                "approval",
                "approved",
                details={"action_type": request.action_type, "preview": request.preview},
            )

        # Translate the POSIX-isms cmd.exe misreads - but only HERE, downstream
        # of `classify_command` and the approval prompt. Translating first meant
        # `rm -rf /` reached the classifier as `rmdir /s /q /` and stopped
        # matching the blocklist, turning a command the harness refuses into one
        # it runs. The user approves what they were shown; the shell gets the
        # dialect it can parse. `_platform_command` was previously called from
        # nowhere at all, so the `python3` shim in it had never once run.
        command = _platform_command(command)
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

        # A server does not exit, so waiting for it to exit is not a check. Run
        # it detached, hand back what it printed on the way up, and leave it
        # serving - which is what the model wanted both times it wrote `&`.
        detached_command, asked_for_background = _strip_posix_background(command)
        if asked_for_background or looks_like_a_server(detached_command):
            log_path = (
                self.workspace_root
                / ".shamsu"
                / "processes"
                / f"{int(time.time())}-{abs(hash(detached_command)) % 10000:04d}.log"
            )
            exit_code, stdout, stderr = _run_command_detached(
                detached_command, validated_cwd, log_path, creationflags
            )
            if self.session_logger:
                self.session_logger.log(
                    "command.detached",
                    {"command": detached_command, "log": str(log_path), "exit_code": exit_code},
                    f"Started in the background: {detached_command}",
                    workflow_id="command",
                )
            self.audit_logger.log(
                "command_run",
                "success" if exit_code == 0 else "error",
                details={
                    "command": detached_command,
                    "detached": True,
                    "log": str(log_path),
                    "exit_code": exit_code,
                    "stdout": stdout,
                },
            )
            if self.action_ledger:
                self.action_ledger.log_command_finish(
                    ledger_cmd_id, detached_command, validated_cwd, exit_code, stdout, stderr
                )
            return exit_code, stdout, stderr

        try:
            completed = _run_command_bounded(
                command,
                validated_cwd,
                self.timeout_seconds,
                creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = redact(_as_text(exc.stdout))
            stderr = redact(_as_text(exc.stderr))
            message = f"Command timed out after {self.timeout_seconds} seconds: {command}"
            if stderr:
                message = f"{message}\n{stderr}"
            if self.session_logger:
                self.session_logger.log(
                    "command.failed",
                    {"command": command, "stdout": stdout, "stderr": message, "exit_code": TIMEOUT_EXIT_CODE},
                    f"Command timed out: {command}",
                    workflow_id="command",
                )
            self.audit_logger.log(
                "command_run",
                "timeout",
                details={"command": command, "stdout": stdout, "stderr": message},
            )
            if self.action_ledger:
                self.action_ledger.log_command_finish(
                    ledger_cmd_id, command, validated_cwd, TIMEOUT_EXIT_CODE, stdout, message
                )
            return TIMEOUT_EXIT_CODE, stdout, message

        result = (
            completed.returncode,
            redact(completed.stdout or ""),
            redact(completed.stderr or ""),
        )
        if result[0] == 0:
            state_path = self.environment_resolver.persist_resolution(resolution)
            if state_path is not None and self.action_ledger:
                try:
                    relative_state = state_path.relative_to(self.workspace_root).as_posix()
                except ValueError:
                    relative_state = str(state_path)
                self.action_ledger.log_event(
                    "project_environment_persisted",
                    path=relative_state,
                    environment_kind=resolution.environment_kind,
                    interpreter=resolution.interpreter,
                )
        if self.session_logger:
            self.session_logger.log(
                "command.finished",
                {
                    "command": command,
                    "exit_code": result[0],
                    "stdout": result[1],
                    "stderr": result[2],
                },
                f"Command finished with exit {result[0]}: {command}",
                workflow_id="command",
            )
        self.audit_logger.log(
            "command_run",
            "success" if result[0] == 0 else "error",
            details={"command": command, "exit_code": result[0], "stdout": result[1], "stderr": result[2]},
        )
        if self.action_ledger:
            self.action_ledger.log_command_finish(
                ledger_cmd_id, command, validated_cwd, result[0], result[1], result[2]
            )
        return result

    def validate_and_approve(
        self,
        command: str,
        cwd: Path,
        description: str | None = None,
    ) -> tuple[bool, int, str, Path | None]:
        """Validate command/cwd and ask approval when needed without executing.

        Used by detached dev-server launches: the command must pass the same
        safety gates as normal command execution, but SHAMSU must not wait for
        a long-running server process to exit.
        """
        try:
            validated_cwd = self._validate_cwd(cwd)
        except (SecurityError, ValueError) as exc:
            return False, WORKSPACE_EXIT_CODE, str(exc), None

        risk = classify_command(command)
        if risk == CommandRisk.BLOCKED:
            if self.session_logger:
                self.session_logger.log(
                    "command.blocked",
                    {"command": command, "risk": risk.value},
                    f"Blocked command: {command}",
                    workflow_id="command",
                )
            self.audit_logger.log("command_run", "blocked", details={"command": command, "risk": risk.value})
            return False, BLOCKED_EXIT_CODE, f"Blocked command: {command}", validated_cwd

        if risk == CommandRisk.MEDIUM:
            request = ApprovalRequest(
                action_type="run_command",
                description=description or f"Run command: {command}",
                risk_level="medium",
                preview=command,
                working_dir=str(validated_cwd),
                reason="Command is medium risk or unknown.",
            )
            self.approval_manager.session_logger = self.session_logger
            if not self.approval_manager.ask(request):
                if self.session_logger:
                    self.session_logger.log(
                        "command.denied",
                        {"command": command, "cwd": str(validated_cwd)},
                        f"Command denied: {command}",
                        workflow_id="command",
                    )
                self.audit_logger.log("command_run", "denied", details={"command": command, "risk": risk.value})
                return False, DENIED_EXIT_CODE, f"Command denied by user: {command}", validated_cwd
        return True, 0, "Command approved.", validated_cwd

    def run_tests(self, cwd: Path) -> TestRunResult:
        exit_code, stdout, stderr = self.run("python -m pytest tests/ -q", cwd)
        raw_output = "\n".join(part for part in (stdout, stderr) if part)
        passed = _summary_count(raw_output, "passed")
        failed = _summary_count(raw_output, "failed")
        if exit_code != 0 and failed == 0:
            failed = 1
        return TestRunResult(passed=passed, failed=failed, raw_output=raw_output)

    def _validate_cwd(self, cwd: Path) -> Path:
        validated = self.sandbox.validate(cwd)
        if not validated.is_dir():
            raise ValueError(f"Working directory does not exist: {validated}")
        return validated


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _summary_count(output: str, word: str) -> int:
    match = re.search(rf"(\d+)\s+{re.escape(word)}\b", output)
    return int(match.group(1)) if match else 0
