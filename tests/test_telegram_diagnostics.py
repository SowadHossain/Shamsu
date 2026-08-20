"""The webhook trap, and the one-click way out of it.

SHAMSU long-polls. A webhook registered against the same bot token makes
`getUpdates` return 409 for as long as it stands, and the poll loop retries
that forever without saying anything - so the bot reports connected and
receives nothing. These tests pin the detection and the fix.

Every test points `base_url` at a local fake. `conftest._no_live_telegram`
refuses `api.telegram.org` outright, because `deleteWebhook` MUTATES a real
bot and a suite that can do that to a developer's phone is not hermetic.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from shamsu.integrations.telegram import diagnostics

TOKEN = "123456:AAH-test-token-value"


class FakeTelegram:
    """A stand-in Bot API. Records what was called, answers what it is told to."""

    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.1 with an accurate Content-Length, so the connection stays
            # open between `getMe` and `getWebhookInfo`. The stdlib default is
            # HTTP/1.0, which closes after every reply - and httpx, which keeps
            # its connection pooled, would intermittently write the second
            # request into a socket the server had just dropped.
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802 - stdlib's spelling
                # The body MUST be consumed. Left in the socket it becomes the
                # first bytes of the next request on the same keep-alive
                # connection, which the stdlib parser answers with 501.
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                method = self.path.rsplit("/", 1)[-1]
                fake.calls.append(method)
                body = json.dumps(
                    fake.responses.get(method, {"ok": False, "description": "unknown method"})
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "FakeTelegram":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def healthy(**hook) -> dict[str, dict]:
    return {
        "getMe": {"ok": True, "result": {"username": "shamsu_bot", "first_name": "SHAMSU"}},
        "getWebhookInfo": {"ok": True, "result": {"url": "", "pending_update_count": 0, **hook}},
        "deleteWebhook": {"ok": True, "result": True},
    }


def test_probe_reports_a_working_bot() -> None:
    with FakeTelegram(healthy()) as fake:
        result = diagnostics.probe(TOKEN, base_url=fake.base_url)
    assert result.ok
    assert result.bot_username == "shamsu_bot"
    assert not result.webhook_blocks_polling


def test_probe_asks_both_questions_in_one_call() -> None:
    """Knowing the token is valid while not knowing a webhook is eating every
    update is exactly the half-answer this exists to prevent."""
    with FakeTelegram(healthy()) as fake:
        diagnostics.probe(TOKEN, base_url=fake.base_url)
        assert fake.calls == ["getMe", "getWebhookInfo"]


def test_a_registered_webhook_is_reported_as_blocking_polling() -> None:
    responses = healthy(url="https://someone-elses-tunnel.example/hook", pending_update_count=12)
    with FakeTelegram(responses) as fake:
        result = diagnostics.probe(TOKEN, base_url=fake.base_url)
    assert result.ok  # the token is fine; the bot is not receiving anything
    assert result.webhook_blocks_polling
    assert result.pending_updates == 12


def test_delete_webhook_keeps_the_updates_that_piled_up() -> None:
    """Queued updates are real messages somebody sent. Dropping them by default
    would turn a fix into a small data loss."""
    import inspect

    with FakeTelegram(healthy(url="https://x.example/hook")) as fake:
        ok, message = diagnostics.delete_webhook(TOKEN, base_url=fake.base_url)
    assert ok
    assert "long polling" in message
    assert inspect.signature(diagnostics.delete_webhook).parameters["drop_pending"].default is False


def test_a_refusal_from_telegram_is_reported_not_raised() -> None:
    responses = {"getMe": {"ok": False, "description": "Unauthorized"}}
    with FakeTelegram(responses) as fake:
        result = diagnostics.probe(TOKEN, base_url=fake.base_url)
    assert not result.ok
    assert "Unauthorized" in result.error


def test_no_token_is_answered_without_a_request() -> None:
    assert diagnostics.probe("").error == "no bot token configured"
    assert diagnostics.delete_webhook("")[0] is False


def test_the_token_never_leaves_this_module_in_an_error() -> None:
    """The token is IN THE URL, so it is in every httpx exception string. A
    "Telegram is unreachable" message that leaks the bot token into a browser,
    a log and then a bug report is worse than the outage it describes."""
    # Nothing is listening on this port, so httpx raises with the full URL.
    result = diagnostics.probe(TOKEN, base_url="http://127.0.0.1:1", timeout=1.0)
    assert not result.ok
    assert TOKEN not in result.error
    assert "123456" not in result.error


def test_the_real_api_is_refused_by_the_suite_guard() -> None:
    """Proof the guard is wired, not merely present: `probe` swallows every
    transport failure, so a live call would otherwise look like a clean miss."""
    from tests.conftest import BLOCKED_CALLS

    before = len(BLOCKED_CALLS)
    result = diagnostics.probe(TOKEN)
    assert not result.ok
    assert len(BLOCKED_CALLS) > before


@pytest.mark.parametrize("method", ["probe", "delete_webhook"])
def test_base_url_is_a_keyword_seam(method: str) -> None:
    """Both entry points must be redirectable, or the suite cannot test them."""
    import inspect

    signature = inspect.signature(getattr(diagnostics, method))
    assert signature.parameters["base_url"].kind is inspect.Parameter.KEYWORD_ONLY
