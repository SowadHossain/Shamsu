"""The portal's HTTP surface: stdlib only, loopback only.

Zero new dependencies, matching the precedent the Telegram integration set -
raw `httpx` over the Bot API, no library, and it worked out. The surface here
is a handful of GETs and one event stream; FastAPI would buy validation this
does not need and cost ~15 MB plus an ASGI server on a tool that advertises low
RAM.

Prompts, the queue and approvals all go through `shamsu.control`, so the
browser is a peer of the CLI and the phone rather than a spectator: a prompt
sent here runs if the thread is free and queues if it is not, and an approval
raised anywhere can be answered here. Watching was always free - `activity.jsonl`
is on disk - and the control store is what made the rest of it safe.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from shamsu.runtime import workspaces as registry
from shamsu.webui import api, sse

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
TOKEN_HEADER = "X-Shamsu-Token"
TOKEN_QUERY = "t"

STATIC_DIR = Path(__file__).parent / "static"

#: Bodies larger than this are refused before being read. A prompt is text.
MAX_BODY_BYTES = 256 * 1024


class WebPortal:
    """A loopback web view over the sessions and turn streams on this machine."""

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        port: int = DEFAULT_PORT,
        runner: Any = None,
    ) -> None:
        # Optional on purpose. The portal is a view over EVERY workspace this
        # install has used, not a window onto one of them - binding it to the
        # directory it happened to be started in was the reason it showed a
        # single project and, if you started it somewhere fresh, nothing at all.
        self.workspace = Path(workspace).resolve() if workspace else None
        self.host = HOST
        self.requested_port = int(port)
        # 32 bytes, minted per start. The URL is the credential, so it must not
        # be guessable and must not outlive the process that printed it.
        self.token = secrets.token_urlsafe(32)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        # The port actually bound, remembered across `stop()`. With `port=0`
        # the OS picks one, and reverting to the request afterwards would make
        # `base_url` report `:0` - a URL that was never real.
        self._bound_port = 0
        # Built lazily so importing or constructing the portal does not touch
        # the control database; a test that never sends a prompt should not
        # create files under the install home.
        self._runner = runner
        self._runner_lock = threading.Lock()
        if self.workspace is not None:
            registry.remember_workspace(self.workspace)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> str:
        if self._server is not None:
            return self.base_url
        handler = _build_handler(self)
        self._server = ThreadingHTTPServer((self.host, self.requested_port), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="shamsu-webui",
            daemon=True,
        )
        self._thread.start()
        self._bound_port = int(self._server.server_address[1])
        return self.base_url

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)

    @property
    def port(self) -> int:
        if self._server is not None:
            return int(self._server.server_address[1])
        return self._bound_port or self.requested_port

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def url(self) -> str:
        """What to actually open: the shell plus the token it needs."""
        return f"{self.base_url}/?{TOKEN_QUERY}={self.token}"

    # -- what the handler asks -------------------------------------------

    @property
    def runner(self) -> Any:
        with self._runner_lock:
            if self._runner is None:
                from shamsu.control.runner import QueuedRunner

                self._runner = QueuedRunner(surface="web")
            return self._runner

    def workspaces(self) -> list[Path]:
        """Every workspace this install has used, current one first if there is one."""
        known = registry.known_workspaces()
        if self.workspace is not None and self.workspace not in known:
            known.insert(0, self.workspace)
        return known


def _build_handler(portal: WebPortal):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SHAMSU-webui"
        # The default logger writes every request to stderr, straight over the
        # REPL's live status line.
        def log_message(self, *_args) -> None:  # noqa: D102
            return

        # -- helpers -----------------------------------------------------

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # No CORS headers at all, deliberately: nothing off this origin has
            # any business reading it.
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: dict, status: int = 200) -> None:
            self._send(status, api.dumps(payload), "application/json; charset=utf-8")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status=status)

        def _origin_is_local(self) -> bool:
            """DNS-rebinding defence.

            A browser on another origin can be made to resolve a name to
            127.0.0.1 and then talk to this server with the victim's network
            position. An absent Origin is fine - that is a direct fetch, not a
            page - but a foreign one never is.
            """
            origin = self.headers.get("Origin")
            if not origin:
                return True
            allowed = {portal.base_url, f"http://localhost:{portal.port}"}
            return origin in allowed

        def _authorized(self, query: dict) -> bool:
            supplied = self.headers.get(TOKEN_HEADER) or ""
            if not supplied:
                supplied = (query.get(TOKEN_QUERY) or [""])[0]
            return secrets.compare_digest(supplied, portal.token)

        # -- routing -----------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib's spelling
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)

            if not self._origin_is_local():
                self._error(403, "cross-origin requests are refused")
                return
            # The shell itself carries no data and must load before the browser
            # has anywhere to put a token.
            if path in ("/", "/index.html"):
                self._serve_static("app.html", "text/html; charset=utf-8")
                return
            if path in ("/app.css", "/app.js"):
                self._serve_static(
                    path.lstrip("/"),
                    "text/css; charset=utf-8"
                    if path.endswith(".css")
                    else "text/javascript; charset=utf-8",
                )
                return
            if not path.startswith("/api/"):
                self._error(404, "not found")
                return
            if not self._authorized(query):
                self._error(401, "a valid token is required")
                return
            try:
                self._route_api(path, query)
            except api.NotFound as exc:
                self._error(404, str(exc))
            except BrokenPipeError:
                # The browser navigated away mid-stream. Not an error.
                return
            except Exception as exc:  # noqa: BLE001 - never take the portal down
                self._error(500, f"{type(exc).__name__}: {exc}")

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            query = parse_qs(parsed.query)
            if not self._origin_is_local():
                self._error(403, "cross-origin requests are refused")
                return
            if not path.startswith("/api/"):
                self._error(404, "not found")
                return
            if not self._authorized(query):
                self._error(401, "a valid token is required")
                return
            body = self._read_body()
            if body is None:
                return
            try:
                self._route_post(path, body)
            except api.NotFound as exc:
                self._error(404, str(exc))
            except ValueError as exc:
                self._error(400, str(exc))
            except Exception as exc:  # noqa: BLE001 - never take the portal down
                self._error(500, f"{type(exc).__name__}: {exc}")

        def _read_body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._error(400, "bad Content-Length")
                return None
            if length > MAX_BODY_BYTES:
                self._error(413, "body too large")
                return None
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except (ValueError, UnicodeDecodeError):
                self._error(400, "body must be JSON")
                return None
            if not isinstance(payload, dict):
                self._error(400, "body must be a JSON object")
                return None
            return payload

        def _route_post(self, path: str, body: dict) -> None:
            parts = [part for part in path.split("/") if part][1:]

            # Approvals are addressed by id alone, deliberately: you answer the
            # question you were shown, and the surface showing it need not know
            # which project raised it.
            if len(parts) == 2 and parts[0] == "approvals":
                decision = str(body.get("decision") or "").strip().lower()
                if decision not in ("allow", "deny"):
                    raise ValueError("decision must be allow or deny")
                resolved = portal.runner.broker.resolve(
                    parts[1], decision == "allow", "web"
                )
                self._json({"resolved": resolved, "decision": decision})
                return

            if parts == ["settings"]:
                self._json(_apply_settings(body))
                return

            if parts == ["telegram"]:
                self._json(_configure_telegram(body))
                return

            # Add a workspace by path. The only route that takes a path from a
            # caller, and it is checked rather than trusted - see api.add_workspace.
            if parts == ["workspaces"]:
                try:
                    summary = api.add_workspace(str(body.get("path") or ""))
                except api.BadPath as exc:
                    raise ValueError(str(exc)) from exc
                self._json({"workspace": summary}, status=201)
                return

            # New thread in an existing workspace.
            if len(parts) == 3 and parts[0] == "workspaces" and parts[2] == "sessions":
                workspace = api.workspace_for_id(parts[1], portal.workspaces())
                session = api.create_session(workspace, str(body.get("title") or ""))
                self._json({"session": session}, status=201)
                return

            if len(parts) == 5 and parts[0] == "workspaces" and parts[2] == "sessions":
                workspace = api.workspace_for_id(parts[1], portal.workspaces())
                session_id, leaf = parts[3], parts[4]
                if not _looks_like_id(session_id):
                    raise ValueError("malformed session id")
                if leaf == "prompt":
                    text = str(body.get("text") or "")
                    outcome = portal.runner.submit(workspace, session_id, text)
                    if not outcome.accepted:
                        raise ValueError(outcome.reason or "prompt refused")
                    self._json(
                        {
                            "accepted": True,
                            "queued": outcome.queued,
                            "queue_id": outcome.queue_id,
                            "reason": outcome.reason,
                        },
                        status=202,
                    )
                    return
                if leaf == "cancel":
                    queue_id = int(body.get("queue_id") or 0)
                    cancelled = portal.runner.store.cancel_queued(queue_id)
                    self._json({"cancelled": cancelled})
                    return
            self._error(404, "not found")

        def _route_api(self, path: str, query: dict) -> None:
            parts = [part for part in path.split("/") if part][1:]  # drop "api"
            if parts == ["health"]:
                self._json(api.health_payload(portal.workspace))
                return
            if parts == ["approvals"]:
                self._json(api.approvals_payload(portal.runner.store))
                return
            if parts == ["settings"]:
                self._json(api.settings_payload())
                return
            if parts == ["commands"]:
                self._json(api.commands_payload())
                return
            if parts == ["workspaces"]:
                self._json(api.workspaces_payload(portal.workspaces()))
                return
            if len(parts) == 3 and parts[0] == "workspaces" and parts[2] == "sessions":
                workspace = api.workspace_for_id(parts[1], portal.workspaces())
                self._json(api.sessions_payload(workspace))
                return
            # A session is addressed THROUGH its workspace. The first version
            # resolved every session against the portal's own workspace, so
            # opening a thread in any other project looked up an id that was
            # not there - the portal listed workspaces it could not then read.
            if len(parts) == 5 and parts[0] == "workspaces" and parts[2] == "sessions":
                workspace = api.workspace_for_id(parts[1], portal.workspaces())
                session_id, leaf = parts[3], parts[4]
                if not _looks_like_id(session_id):
                    self._error(400, "malformed session id")
                    return
                if leaf == "messages":
                    after = _int_arg(query, "after", 0)
                    self._json(api.session_messages(workspace, session_id, after))
                    return
                if leaf == "activity":
                    self._json(
                        api.session_activity(
                            workspace,
                            session_id,
                            since_seq=_int_arg(query, "since", -1),
                            turn_id=(query.get("turn") or [""])[0],
                        )
                    )
                    return
                if leaf == "stream":
                    self._stream(workspace, session_id)
                    return
                if leaf == "queue":
                    self._json(
                        api.queue_payload(portal.runner.store, workspace, session_id)
                    )
                    return
            self._error(404, "not found")

        # -- SSE ---------------------------------------------------------

        def _stream(self, workspace: Path, session_id: str) -> None:
            path = api.activity_file(workspace, session_id)
            since = _last_event_id(self.headers.get("Last-Event-ID"))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                for chunk in sse.tail_events(
                    path,
                    since_seq=since,
                    should_stop=lambda: portal._server is None,
                ):
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ValueError):
                return

        # -- static ------------------------------------------------------

        def _serve_static(self, name: str, content_type: str) -> None:
            path = STATIC_DIR / name
            if not path.exists():
                self._error(404, "not found")
                return
            self._send(200, path.read_bytes(), content_type)

    return Handler


def _apply_settings(body: dict) -> dict:
    """Change an install-wide preference. Validated here, not in the browser.

    A window below 4096 cannot hold the system prompt and a tool schema, so it
    would not produce a smaller model - it would produce one that fails on its
    first call.
    """
    from shamsu.runtime.settings import update_settings

    changes: dict = {}
    if "chat_max_ctx" in body:
        raw = body.get("chat_max_ctx")
        if raw in (None, "", "default"):
            changes["chat_max_ctx"] = None
        else:
            try:
                window = int(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("chat_max_ctx must be a number") from exc
            if window < 4096:
                raise ValueError("chat_max_ctx must be at least 4096")
            changes["chat_max_ctx"] = window
    if not changes:
        raise ValueError("nothing to change")
    update_settings(**changes)
    return api.settings_payload()


def _configure_telegram(body: dict) -> dict:
    """Save a bot token, install-wide. Never echoed back, at any length."""
    from shamsu.integrations.telegram.service import configure_telegram_bot_token

    token = str(body.get("token") or "").strip()
    if not token:
        raise ValueError("give a bot token")
    try:
        configure_telegram_bot_token(Path.cwd(), token)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return {"telegram": api.telegram_status()}


def _looks_like_id(value: str) -> bool:
    """Session ids are generated, so anything path-shaped is not one.

    Belt and braces: `SessionManager.resolve` would refuse an unknown id
    anyway, but rejecting the shape means a traversal attempt never reaches
    code that touches the filesystem at all.
    """
    return bool(value) and "/" not in value and "\\" not in value and ".." not in value


def _int_arg(query: dict, name: str, default: int) -> int:
    raw = (query.get(name) or [""])[0]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _last_event_id(raw: str | None) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return -1
