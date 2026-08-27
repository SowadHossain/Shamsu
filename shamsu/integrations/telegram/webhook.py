"""Telegram webhook receiver and Cloudflare Tunnel launcher."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

from shamsu.integrations.telegram.models import TelegramUpdate
from shamsu.integrations.telegram.transport import normalize_update

UpdateProcessor = Callable[[TelegramUpdate], Awaitable[None]]

CLOUDFLARED_ENV_VAR = "SHAMSU_CLOUDFLARED"
TUNNEL_TIMEOUT_ENV_VAR = "SHAMSU_TELEGRAM_WEBHOOK_TUNNEL_TIMEOUT_SECONDS"
TUNNEL_READY_TIMEOUT_ENV_VAR = "SHAMSU_TELEGRAM_WEBHOOK_READY_TIMEOUT_SECONDS"
TRYCLOUDFLARE_URL = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")
MAX_UPDATE_BYTES = 2_000_000


class TelegramWebhookError(RuntimeError):
    """Webhook startup or request handling failed."""


class CloudflaredQuickTunnel:
    """Own a `cloudflared tunnel --url ...` process."""

    def __init__(self, local_url: str, *, executable: str | None = None, timeout: float | None = None) -> None:
        self.local_url = local_url.rstrip("/")
        self.executable = executable or os.environ.get(CLOUDFLARED_ENV_VAR, "").strip() or "cloudflared"
        self.timeout = timeout if timeout is not None else _tunnel_timeout()
        self.public_url = ""
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._lines: list[str] = []

    def start(self) -> str:
        path = shutil.which(self.executable) if self.executable == "cloudflared" else self.executable
        if not path:
            raise TelegramWebhookError(
                "cloudflared was not found. Install it or set SHAMSU_CLOUDFLARED to its path."
            )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            [path, "tunnel", "--url", self.local_url],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        self._reader = threading.Thread(target=self._read_output, name="shamsu-cloudflared", daemon=True)
        self._reader.start()
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self._ready.is_set() and self._connected.is_set():
                return self.public_url
            self._ready.wait(timeout=0.1)
            if self._process.poll() is not None:
                break
        tail = "\n".join(self._lines[-8:]).strip()
        self.stop()
        detail = f"\n\ncloudflared output:\n{tail}" if tail else ""
        raise TelegramWebhookError(f"cloudflared did not publish a tunnel URL in time.{detail}")

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _read_output(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            clean = line.rstrip()
            if clean:
                self._lines.append(clean)
            match = TRYCLOUDFLARE_URL.search(clean)
            if match:
                self.public_url = match.group(0).rstrip("/")
                self._ready.set()
            if "Registered tunnel connection" in clean:
                self._connected.set()


class TelegramWebhookServer:
    """Receive Telegram updates on localhost and hand them to the service loop."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        path: str,
        secret_token: str,
        loop: asyncio.AbstractEventLoop,
        process_update: UpdateProcessor,
        process_timeout: float = 25.0,
    ) -> None:
        self.host = host
        self.path = path if path.startswith("/") else f"/{path}"
        self.secret_token = secret_token
        self.loop = loop
        self.process_update = process_update
        self.process_timeout = process_timeout
        handler = self._handler()
        self._server = ThreadingHTTPServer((host, int(port)), handler)
        self.port = int(self._server.server_port)
        self._thread: threading.Thread | None = None

    @property
    def local_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def webhook_url_path(self) -> str:
        return self.path

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="shamsu-telegram-webhook",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/healthz":
                    self._reply(HTTPStatus.OK, b"ok")
                    return
                self._reply(HTTPStatus.NOT_FOUND, b"not found")

            def do_POST(self) -> None:
                if self.path != owner.path:
                    self._reply(HTTPStatus.NOT_FOUND, b"not found")
                    return
                if owner.secret_token:
                    header = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
                    if header != owner.secret_token:
                        self._reply(HTTPStatus.FORBIDDEN, b"forbidden")
                        return
                length = _content_length(self.headers.get("Content-Length", ""))
                if length < 0 or length > MAX_UPDATE_BYTES:
                    self._reply(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"too large")
                    return
                try:
                    raw = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._reply(HTTPStatus.BAD_REQUEST, b"bad json")
                    return
                if not isinstance(raw, dict):
                    self._reply(HTTPStatus.BAD_REQUEST, b"bad update")
                    return
                update = normalize_update(raw)
                if update is None:
                    self._reply(HTTPStatus.OK, b"ignored")
                    return
                future = asyncio.run_coroutine_threadsafe(owner.process_update(update), owner.loop)
                try:
                    future.result(timeout=owner.process_timeout)
                except concurrent.futures.TimeoutError:
                    self._reply(HTTPStatus.OK, b"accepted")
                    return
                except Exception:  # noqa: BLE001 - the HTTP reply is the error boundary
                    self._reply(HTTPStatus.INTERNAL_SERVER_ERROR, b"failed")
                    return
                self._reply(HTTPStatus.OK, b"ok")

            def log_message(self, format: str, *args: Any) -> None:
                return None

            def _reply(self, status: HTTPStatus, body: bytes) -> None:
                self.send_response(int(status))
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler


def wait_for_public_webhook(public_root: str, *, timeout: float | None = None) -> None:
    """Wait until the Cloudflare URL actually reaches the local server."""
    root = public_root.rstrip("/")
    deadline = time.monotonic() + (timeout if timeout is not None else _ready_timeout())
    last_error = ""
    while time.monotonic() < deadline:
        try:
            response = httpx.get(f"{root}/healthz", timeout=5, follow_redirects=True)
            if response.status_code == HTTPStatus.OK:
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.5)
    suffix = f" Last check: {last_error}" if last_error else ""
    raise TelegramWebhookError(
        f"Cloudflare tunnel URL was published but is not reachable yet: {root}.{suffix}"
    )


def _content_length(raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


def _tunnel_timeout() -> float:
    raw = os.environ.get(TUNNEL_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return 30.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 30.0


def _ready_timeout() -> float:
    raw = os.environ.get(TUNNEL_READY_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return 90.0
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 90.0
