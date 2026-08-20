"""Ask Telegram what it thinks of this bot, and fix the one thing that silently breaks it.

SHAMSU receives updates by **long polling** - `getUpdates` in a loop, see
`transport.py`. There is no webhook handler and no tunnel, and none is planned.

That makes one remote setting lethal in a way nothing surfaces: if a webhook
URL is registered against the bot token - by another tool, an earlier
experiment, a half-finished tutorial - Telegram refuses `getUpdates` with
**409 Conflict** for as long as it stands. The poll loop swallows that
exception and retries forever (`service.py`), so the bot reports CONNECTED,
the phone sends messages, and nothing whatsoever arrives. Nothing in the
product has ever said why.

`probe()` detects it. `delete_webhook()` fixes it. Both are called only from an
explicit button press, never on page load, because both cost a round trip to
`api.telegram.org` and the settings drawer must open instantly.

**The token is in the URL**, which means it is in the string representation of
every httpx exception. Everything leaving this module goes through
:func:`_scrub` first. A "Telegram is unreachable" message that leaks the bot
token into a browser, a log file and a bug report would be a far worse failure
than the outage it was describing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

TELEGRAM_API_BASE = "https://api.telegram.org"

#: Short on purpose. This runs inside a request handler while someone watches a
#: spinner; a Telegram that needs longer than this is "unreachable" for the
#: purpose of answering "is my bot alive?".
PROBE_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class BotProbe:
    """What Telegram says about this token, right now."""

    ok: bool
    bot_username: str = ""
    bot_name: str = ""
    webhook_url: str = ""
    pending_updates: int = 0
    webhook_last_error: str = ""
    error: str = ""

    @property
    def webhook_blocks_polling(self) -> bool:
        """A registered webhook means `getUpdates` returns 409 and nothing arrives."""
        return bool(self.webhook_url)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "bot_username": self.bot_username,
            "bot_name": self.bot_name,
            "webhook_url": self.webhook_url,
            "pending_updates": self.pending_updates,
            "webhook_last_error": self.webhook_last_error,
            "webhook_blocks_polling": self.webhook_blocks_polling,
            "error": self.error,
        }


def probe(token: str, *, base_url: str = TELEGRAM_API_BASE, timeout: float = PROBE_TIMEOUT_SECONDS) -> BotProbe:
    """`getMe` + `getWebhookInfo` in one call, as a single verdict.

    Both or neither: knowing the token is valid while not knowing a webhook is
    eating every update is exactly the half-answer that made this necessary.
    """
    if not str(token or "").strip():
        return BotProbe(False, error="no bot token configured")
    try:
        with httpx.Client(timeout=timeout) as client:
            me = _call(client, token, "getMe", base_url)
            hook = _call(client, token, "getWebhookInfo", base_url)
    except _ApiError as exc:
        return BotProbe(False, error=_scrub(str(exc), token))
    except Exception as exc:  # noqa: BLE001 - any transport failure is "unreachable"
        return BotProbe(False, error=_scrub(f"{type(exc).__name__}: {exc}", token))
    return BotProbe(
        ok=True,
        bot_username=str(me.get("username") or ""),
        bot_name=str(me.get("first_name") or ""),
        webhook_url=str(hook.get("url") or ""),
        pending_updates=int(hook.get("pending_update_count") or 0),
        webhook_last_error=_scrub(str(hook.get("last_error_message") or ""), token),
    )


def delete_webhook(
    token: str,
    *,
    base_url: str = TELEGRAM_API_BASE,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    drop_pending: bool = False,
) -> tuple[bool, str]:
    """Unregister the webhook so long polling can work again.

    `drop_pending` defaults to False: updates queued while the webhook stood are
    real messages someone sent, and discarding them by default would turn a fix
    into a small data loss.
    """
    if not str(token or "").strip():
        return False, "no bot token configured"
    try:
        with httpx.Client(timeout=timeout) as client:
            _call(
                client,
                token,
                "deleteWebhook",
                base_url,
                payload={"drop_pending_updates": bool(drop_pending)},
            )
    except _ApiError as exc:
        return False, _scrub(str(exc), token)
    except Exception as exc:  # noqa: BLE001
        return False, _scrub(f"{type(exc).__name__}: {exc}", token)
    return True, "webhook deleted - long polling can receive updates again"


class _ApiError(RuntimeError):
    """Telegram answered, and said no."""


def _call(
    client: httpx.Client,
    token: str,
    method: str,
    base_url: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{base_url.rstrip('/')}/bot{token}/{method}"
    response = client.post(url, json=payload or {})
    try:
        body = response.json()
    except ValueError:
        raise _ApiError(f"{method}: HTTP {response.status_code}, unreadable reply") from None
    if not body.get("ok"):
        description = str(body.get("description") or f"HTTP {response.status_code}")
        raise _ApiError(f"{method}: {description}")
    result = body.get("result")
    return result if isinstance(result, dict) else {}


def _scrub(text: str, token: str) -> str:
    """Remove the bot token from anything on its way out of this module."""
    cleaned = str(text or "")
    secret = str(token or "").strip()
    if secret:
        cleaned = cleaned.replace(secret, "<token>")
        # httpx sometimes reports only the path, and a token's leading numeric
        # id is enough to identify the bot on its own.
        head = secret.split(":", 1)[0]
        if head and head.isdigit():
            cleaned = cleaned.replace(f"/bot{head}", "/bot<token>")
    return cleaned
