"""Local HTTP server for the SHAMSU browser UI."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from shamsu.runtime.doctor import run_doctor
from shamsu.runtime.models import active_tier, model_for_role
from shamsu.runtime.ollama import collect_status

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_workspace() -> Path:
    return Path(os.environ.get("SHAMSU_WORKSPACE") or repo_root()).resolve()


def clean_terminal_output(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.splitlines()]
    prompt_indices = [index for index, line in enumerate(lines) if "shamsu>" in line]
    if prompt_indices:
        lines = lines[prompt_indices[0] + 1 :]
    trimmed: list[str] = []
    skip_prefixes = (
        "SHAMSU v",
        "Workspace:",
        "Model:",
        "Runtime:",
        "Graphiti memory:",
        "Code memory:",
        "Session:",
        "Trace:",
        "Type a prompt",
        "shamsu>",
        "Goodbye.",
    )
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if trimmed and trimmed[-1]:
                trimmed.append("")
            continue
        if any(stripped.startswith(prefix) for prefix in skip_prefixes):
            continue
        trimmed.append(line)
    return "\n".join(trimmed).strip()


def run_prompt(prompt: str, workspace: Path, timeout: int) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    command = [sys.executable, "-m", "shamsu.cli.repl", "--workspace", str(workspace)]
    try:
        completed = subprocess.run(
            command,
            input=f"{prompt}\nexit\n",
            cwd=repo_root(),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        partial = clean_terminal_output((exc.stdout or "") + "\n" + (exc.stderr or ""))
        message = partial or f"Prompt timed out after {timeout}s. Try a smaller request or use the terminal for interactive approvals."
        return {"ok": False, "answer": message, "exit_code": None, "timeout": True}

    output = clean_terminal_output(completed.stdout + "\n" + completed.stderr)
    if not output:
        output = "The backend completed without returning visible output."
    return {
        "ok": completed.returncode == 0,
        "answer": output,
        "exit_code": completed.returncode,
        "timeout": False,
    }


class ShamsuWebHandler(SimpleHTTPRequestHandler):
    server_version = "ShamsuWebUI/0.1"

    def __init__(self, *args: Any, directory: str | None = None, **kwargs: Any) -> None:
        super().__init__(*args, directory=directory or str(repo_root() / "webui"), **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    @property
    def workspace(self) -> Path:
        return self.server.workspace  # type: ignore[attr-defined]

    @property
    def prompt_timeout(self) -> int:
        return self.server.prompt_timeout  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send_json(self._status_payload())
            return
        if path.startswith("/api/"):
            self._send_json({"ok": False, "error": "Unknown API route."}, HTTPStatus.NOT_FOUND)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/prompt":
            self._send_json({"ok": False, "error": "Unknown API route."}, HTTPStatus.NOT_FOUND)
            return
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            self._send_json({"ok": False, "error": "Prompt is required."}, HTTPStatus.BAD_REQUEST)
            return
        self._send_json(run_prompt(prompt, self.workspace, self.prompt_timeout))

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid JSON body.") from exc
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object.")
        return parsed

    def _status_payload(self) -> dict[str, Any]:
        try:
            runtime = collect_status()
            doctor = run_doctor(workspace=self.workspace)
            return {
                "ok": True,
                "workspace": str(self.workspace),
                "runtime": {
                    "ready": runtime.ready,
                    "message": runtime.message,
                    "endpoint": getattr(runtime, "base_url", ""),
                    "server_running": runtime.server_running,
                },
                "model": model_for_role("chat"),
                "tier": active_tier().value,
                "checks": [asdict(check) for check in doctor.checks],
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc), "workspace": str(self.workspace)}

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ShamsuWebServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[ShamsuWebHandler],
        workspace: Path,
        prompt_timeout: int,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.workspace = workspace
        self.prompt_timeout = prompt_timeout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m shamsu.web.server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5174)
    parser.add_argument("--workspace", default=None)
    parser.add_argument("--prompt-timeout", type=int, default=300)
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else default_workspace()
    web_root = repo_root() / "webui"
    if not web_root.exists():
        raise SystemExit(f"webui folder not found: {web_root}")

    server = ShamsuWebServer((args.host, args.port), ShamsuWebHandler, workspace, args.prompt_timeout)
    print(f"Starting SHAMSU Web UI at http://{args.host}:{args.port}")
    print(f"Workspace: {workspace}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping SHAMSU Web UI.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
