"""OAuth and OS-keyring support for remote MCP servers."""
from __future__ import annotations

import asyncio
import queue
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

import keyring
from keyring.errors import KeyringError
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl


class KeyringTokenStorage(TokenStorage):
    """Persist OAuth material in the operating system credential store."""

    def __init__(self, server_name: str) -> None:
        self.service = "shamsu-mcp"
        self.tokens_key = f"{server_name}:tokens"
        self.client_key = f"{server_name}:client"

    async def get_tokens(self) -> OAuthToken | None:
        return await asyncio.to_thread(self._get_model, self.tokens_key, OAuthToken)

    async def set_tokens(self, tokens: OAuthToken) -> None:
        await asyncio.to_thread(self._set_model, self.tokens_key, tokens)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return await asyncio.to_thread(
            self._get_model, self.client_key, OAuthClientInformationFull
        )

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await asyncio.to_thread(self._set_model, self.client_key, client_info)

    def clear(self) -> None:
        for key in (self.tokens_key, self.client_key):
            try:
                keyring.delete_password(self.service, key)
            except (KeyringError, TypeError):
                pass

    def _get_model(self, key: str, model: type[Any]) -> Any | None:
        try:
            raw = keyring.get_password(self.service, key)
        except KeyringError:
            return None
        if not raw:
            return None
        try:
            return model.model_validate_json(raw)
        except (ValueError, TypeError):
            return None

    def _set_model(self, key: str, value: Any) -> None:
        payload = value.model_dump_json(exclude_none=True)
        try:
            keyring.set_password(self.service, key, payload)
        except KeyringError as exc:
            raise RuntimeError(
                "No usable OS credential store is available for MCP OAuth tokens."
            ) from exc


class OAuthCallbackServer:
    def __init__(self, port: int = 0) -> None:
        self.values: queue.Queue[tuple[str, str | None] | Exception] = queue.Queue(maxsize=1)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
                params = parse_qs(urlparse(self.path).query)
                if "error" in params:
                    value: tuple[str, str | None] | Exception = RuntimeError(
                        f"MCP OAuth failed: {params['error'][0]}"
                    )
                    status = 400
                elif "code" not in params:
                    value = RuntimeError("MCP OAuth callback did not contain an authorization code.")
                    status = 400
                else:
                    value = (params["code"][0], params.get("state", [None])[0])
                    status = 200
                try:
                    owner.values.put_nowait(value)
                except queue.Full:
                    pass
                body = (
                    b"MCP authorization complete. You can close this window."
                    if status == 200
                    else b"MCP authorization failed. Return to SHAMSU for details."
                )
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/callback"

    async def wait(self) -> tuple[str, str | None]:
        deadline = time.monotonic() + 300.0
        while True:
            try:
                value = self.values.get_nowait()
                break
            except queue.Empty:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Timed out waiting for the MCP OAuth browser callback.")
                await asyncio.sleep(0.1)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2.0)


def build_oauth_provider(
    server_name: str,
    server_url: str,
    callback: OAuthCallbackServer,
    scopes: str | None = None,
) -> OAuthClientProvider:
    async def redirect_handler(auth_url: str) -> None:
        if not webbrowser.open(auth_url):
            raise RuntimeError(f"Open this MCP authorization URL in a browser: {auth_url}")

    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=OAuthClientMetadata(
            client_name="SHAMSU",
            redirect_uris=[AnyUrl(callback.redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope=scopes,
        ),
        storage=KeyringTokenStorage(server_name),
        redirect_handler=redirect_handler,
        callback_handler=callback.wait,
    )
