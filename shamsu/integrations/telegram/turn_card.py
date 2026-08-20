"""The Telegram live turn card - a terminal pane, not a notification feed.

The bug this replaces: every surviving activity line became its own chat
message, so the delivery shape was wrong, so a throttle was added to protect
the API, so a 136-second model call and most of a 12-tool turn were simply
never delivered. Events were dropped to bound the API rate.

This inverts that. **One message is sent and then edited.** The edit rate is
bounded (default one every 1.5s) and lines ACCUMULATE between flushes, so the
API call count is bounded by the clock while the information is bounded by
nothing. Approvals, errors and the end of the turn bypass the interval, because
a bounded rate that delays an approval prompt is a worse bug than the one being
fixed.

Renderer contract: this object is called with `TurnEvent`s, synchronously, on
whatever thread the agent turn runs on. `send` must therefore be a BLOCKING
call that returns Telegram's `message_id` - the card cannot edit a message
whose id it never learned, and a fire-and-forget send cannot hand one back.
"""
from __future__ import annotations

import html
import re
import time
from typing import Callable

from shamsu.integrations.telegram.formatter import MAX_MESSAGE_CHARS
from shamsu.integrations.telegram.models import OutboundMessage
from shamsu.runtime.turn_stream import TurnEvent, body_kinds

#: Telegram allows roughly one message per second per chat and rate-limits
#: `editMessageText` similarly. 1.5s leaves headroom for a burst without the
#: card feeling stale on a phone.
DEFAULT_FLUSH_SECONDS = 1.5

#: `sendChatAction` expires after ~5s, so it is re-sent a little sooner. It is
#: free, and it is the idiom every Telegram user already reads as "working".
DEFAULT_TYPING_SECONDS = 4.0

CONTINUATION_HEADER = "... continued"

_RETRY_AFTER = re.compile(r"retry after (\d+)", re.IGNORECASE)


class TelegramTurnCard:
    """One turn, rendered into one editable Telegram message (or a few)."""

    def __init__(
        self,
        *,
        chat_id: int,
        send: Callable[[OutboundMessage], int],
        prompt: str,
        label: str = "remote-telegram",
        typing: Callable[[], None] | None = None,
        verbosity: str = "normal",
        flush_interval: float = DEFAULT_FLUSH_SECONDS,
        typing_interval: float = DEFAULT_TYPING_SECONDS,
        max_chars: int = MAX_MESSAGE_CHARS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.chat_id = int(chat_id)
        self.send = send
        self.prompt = prompt or ""
        self.label = label or "remote-telegram"
        self.typing = typing
        self.verbosity = verbosity
        self.flush_interval = float(flush_interval)
        self.typing_interval = float(typing_interval)
        self.max_chars = int(max_chars)
        self.clock = clock
        # Room kept back for the footer. The overflow decision is taken while
        # appending a line, when the footer is usually empty; without a reserve
        # a card that fitted exactly would burst the moment `turn.end` wrote a
        # verdict under it.
        self._footer_reserve = min(160, max(24, self.max_chars // 4))

        self._body = body_kinds(verbosity)
        #: Every body line of the whole turn, across continuation cards. This
        #: is the list the parity test compares against the CLI's.
        self.all_lines: list[str] = []
        self.sealed = False
        self.send_failures = 0
        self._consecutive_failures = 0
        self.message_ids: list[int] = []

        self._card_lines: list[str] = []
        self._header = f"shamsu ({self.label})> {self.prompt}".rstrip()
        self._footer = ""
        self._message_id: int | None = None
        self._last_text = ""
        self._last_flush = -1e9
        self._last_typing = -1e9
        self._retry_until = -1e9

    # -- renderer --------------------------------------------------------

    def __call__(self, event: TurnEvent) -> None:
        if self.sealed:
            return
        kind = event.kind
        if kind == "turn.start":
            self._flush(force=True)
            return
        if kind == "status":
            self._footer = event.text
            self._tick_typing()
            self._flush()
            return
        if kind == "turn.end":
            self._footer = event.text
            self._flush(force=True)
            self.sealed = True
            return
        if kind == "error":
            if event.text:
                self._append(event.text)
            self._flush(force=True)
            return
        if kind == "approval":
            if event.text:
                self._append(event.text)
            self._flush(force=True)
            return
        if kind in self._body and event.text:
            self._append(event.text)
            self._flush()

    # -- body ------------------------------------------------------------

    def _append(self, line: str) -> None:
        """Add one line. Never drops one, and never silently loses it to overflow."""
        line = self._fit(line)
        self.all_lines.append(line)
        # `self._card_lines` guards the degenerate case: a card must always
        # accept at least one line, or a line too big for any card would
        # bounce from continuation to continuation forever.
        if self._card_lines and not self._fits(self._card_lines + [line]):
            # Seal what is there and open a continuation, so a long turn reads
            # as a sequence of terminal panes rather than one truncated blob.
            self._seal_for_overflow()
            self._card_lines = [line]
            self._flush(force=True)
            return
        self._card_lines.append(line)

    def _fit(self, line: str) -> str:
        """A single line longer than a whole card is clipped, not discarded.

        The full, untruncated record is always in `activity.jsonl`; what is
        lost here is only what would not have rendered anyway.
        """
        budget = max(32, self.max_chars - self._chrome_length() - self._footer_reserve - 16)
        if len(line) <= budget:
            return line
        return line[: budget - 3] + "..."

    def _chrome_length(self) -> int:
        return len(self._render_text([], header=self._header, footer=""))

    def _fits(self, lines: list[str]) -> bool:
        rendered = self._render_text(lines, header=self._header, footer="")
        return len(rendered) + self._footer_reserve <= self.max_chars

    def _seal_for_overflow(self) -> None:
        text = self._render_text(self._card_lines, header=self._header, footer="")
        self._deliver(text)
        self._message_id = None
        self._last_text = ""
        self._header = CONTINUATION_HEADER

    # -- rendering -------------------------------------------------------

    def _render_text(self, lines: list[str], *, header: str, footer: str) -> str:
        parts = [f"<b>{html.escape(header)}</b>"]
        if lines:
            parts.append("<pre>" + html.escape("\n".join(lines)) + "</pre>")
        if footer:
            parts.append(html.escape(footer))
        return "\n".join(parts)

    def _render(self) -> str:
        return self._render_text(
            self._card_lines, header=self._header, footer=self._footer
        )

    # -- delivery --------------------------------------------------------

    def _flush(self, *, force: bool = False) -> None:
        now = self.clock()
        if now < self._retry_until and not force:
            return
        if not force and (now - self._last_flush) < self.flush_interval:
            return
        text = self._render()
        if text == self._last_text:
            return
        self._last_flush = now
        self._deliver(text)

    def _deliver(self, text: str) -> None:
        message = OutboundMessage(
            chat_id=self.chat_id,
            text=text,
            parse_mode="HTML",
            edit_message_id=self._message_id,
        )
        try:
            message_id = int(self.send(message) or 0)
        except Exception as exc:  # noqa: BLE001 - the API saying no loses no line
            self.send_failures += 1
            self._consecutive_failures += 1
            # Back off, and back off HARDER each time. This call blocks the
            # agent's thread, so a transport that is simply unreachable must
            # not be re-tried every 1.5 seconds for the length of a ten-minute
            # turn.
            self._retry_until = self.clock() + max(
                _retry_after(str(exc)), min(60.0, 2.0 ** self._consecutive_failures)
            )
            # `_last_text` is deliberately NOT updated: the lines are still
            # buffered and the next flush re-sends all of them.
            return
        self._consecutive_failures = 0
        self._last_text = text
        if self._message_id is None and message_id:
            self._message_id = message_id
            self.message_ids.append(message_id)

    def _tick_typing(self) -> None:
        if self.typing is None or self.sealed:
            return
        now = self.clock()
        if now - self._last_typing < self.typing_interval:
            return
        self._last_typing = now
        try:
            self.typing()
        except Exception:  # noqa: BLE001 - a courtesy signal, never a failure
            pass


def _retry_after(error: str) -> float:
    """How long Telegram asked us to wait, if it said so. 1s otherwise."""
    match = _RETRY_AFTER.search(error or "")
    if not match:
        return 1.0
    try:
        return min(60.0, float(match.group(1)))
    except ValueError:
        return 1.0
