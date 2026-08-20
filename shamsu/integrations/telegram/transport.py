"""Telegram Bot API transport abstractions."""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import httpx

from shamsu.integrations.telegram.models import (
    OutboundMessage,
    TelegramCallbackQuery,
    TelegramChat,
    TelegramFile,
    TelegramMessage,
    TelegramUpdate,
    TelegramUser,
)
from shamsu.safety.commands import redact


class TelegramTransport(ABC):
    @abstractmethod
    async def updates(self) -> AsyncIterator[TelegramUpdate]:
        ...

    @abstractmethod
    async def send(self, message: OutboundMessage) -> int:
        ...

    async def chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Show "typing..." in the chat. Concrete, not abstract, on purpose:
        it is a courtesy signal, and an implementation that cannot send one
        must not be forced to pretend it can."""
        return None

    async def close(self) -> None:
        return None


class TelegramBotApiTransport(TelegramTransport):
    def __init__(
        self,
        *,
        bot_token: str,
        offset_getter,
        offset_setter,
        timeout_seconds: int = 30,
        base_url: str = "https://api.telegram.org",
    ) -> None:
        self._token = bot_token
        self._offset_getter = offset_getter
        self._offset_setter = offset_setter
        self._timeout_seconds = timeout_seconds
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=timeout_seconds + 10)
        self._closed = False

    async def updates(self) -> AsyncIterator[TelegramUpdate]:
        while not self._closed:
            offset = int(self._offset_getter() or 0)
            try:
                payload = await self._api(
                    "getUpdates",
                    {
                        "timeout": self._timeout_seconds,
                        "offset": offset,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
            except Exception:
                await asyncio.sleep(2)
                continue
            for raw in payload:
                update = normalize_update(raw)
                if update is None:
                    continue
                self._offset_setter(update.update_id + 1)
                yield update

    async def send(self, message: OutboundMessage) -> int:
        if message.document_path is not None:
            return await self._send_document(message)
        method = "editMessageText" if message.edit_message_id is not None else "sendMessage"
        payload: dict[str, Any] = {
            "chat_id": message.chat_id,
            "text": redact(message.text),
            "disable_web_page_preview": True,
        }
        if message.edit_message_id is not None:
            payload["message_id"] = message.edit_message_id
        if message.parse_mode:
            payload["parse_mode"] = message.parse_mode
        if message.reply_markup is not None:
            payload["reply_markup"] = message.reply_markup.to_telegram()
        result = await self._api(method, payload)
        return int((result or {}).get("message_id") or message.edit_message_id or 0)

    async def chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self._api("sendChatAction", {"chat_id": chat_id, "action": action})

    async def _send_document(self, message: OutboundMessage) -> int:
        path = message.document_path
        assert path is not None
        data = {"chat_id": str(message.chat_id), "caption": redact(message.text[:1000])}
        files = {"document": (path.name, path.read_bytes())}
        result = await self._api("sendDocument", data, files=files)
        return int((result or {}).get("message_id") or 0)

    async def _api(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        files: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base_url}/bot{self._token}/{method}"
        if files:
            response = await self._client.post(url, data=payload, files=files)
        else:
            response = await self._client.post(url, json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API failed: {body.get('description', 'unknown')}")
        return body.get("result")

    async def close(self) -> None:
        self._closed = True
        await self._client.aclose()


class FakeTelegramTransport(TelegramTransport):
    def __init__(self) -> None:
        self.inbound: asyncio.Queue[TelegramUpdate] = asyncio.Queue()
        self.sent: list[OutboundMessage] = []
        self.actions: list[tuple[int, str]] = []
        self.closed = False

    async def updates(self) -> AsyncIterator[TelegramUpdate]:
        while not self.closed:
            update = await self.inbound.get()
            yield update

    async def send(self, message: OutboundMessage) -> int:
        self.sent.append(message)
        # An EDIT keeps the id it was given; only a fresh send mints one. A
        # fake that renumbered every call would have hidden the live card's
        # whole point - that it edits one message instead of sending many.
        if message.edit_message_id is not None:
            return int(message.edit_message_id)
        return len(self.sent)

    async def chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append((int(chat_id), action))

    async def close(self) -> None:
        self.closed = True


def normalize_update(raw: dict[str, Any]) -> TelegramUpdate | None:
    update_id = int(raw.get("update_id", 0))
    if "message" in raw:
        return TelegramUpdate(update_id=update_id, message=_normalize_message(raw["message"]))
    if "callback_query" in raw:
        callback = raw["callback_query"]
        message = _normalize_message(callback.get("message", {}))
        user = _normalize_user(callback.get("from", {}))
        return TelegramUpdate(
            update_id=update_id,
            callback_query=TelegramCallbackQuery(
                callback_query_id=str(callback.get("id", "")),
                user=user,
                message=message,
                data=str(callback.get("data", "")),
            ),
        )
    return None


def _normalize_message(raw: dict[str, Any]) -> TelegramMessage:
    document = raw.get("document") or None
    return TelegramMessage(
        message_id=int(raw.get("message_id", 0)),
        user=_normalize_user(raw.get("from", {})),
        chat=_normalize_chat(raw.get("chat", {})),
        text=str(raw.get("text") or raw.get("caption") or ""),
        document=_normalize_file(document) if document else None,
    )


def _normalize_user(raw: dict[str, Any]) -> TelegramUser:
    return TelegramUser(
        user_id=int(raw.get("id", 0)),
        first_name=str(raw.get("first_name") or ""),
        last_name=str(raw.get("last_name") or ""),
        username=str(raw.get("username") or ""),
    )


def _normalize_chat(raw: dict[str, Any]) -> TelegramChat:
    return TelegramChat(
        chat_id=int(raw.get("id", 0)),
        chat_type=str(raw.get("type") or "private"),
        title=str(raw.get("title") or ""),
    )


def _normalize_file(raw: dict[str, Any]) -> TelegramFile:
    return TelegramFile(
        file_id=str(raw.get("file_id") or ""),
        file_name=str(raw.get("file_name") or "attachment"),
        mime_type=str(raw.get("mime_type") or ""),
        file_size=int(raw.get("file_size") or 0),
    )
